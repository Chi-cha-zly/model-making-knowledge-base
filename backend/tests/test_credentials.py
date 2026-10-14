"""Synthetic secrets and isolated fake vault only; never touch the OS keychain."""

from copy import deepcopy
import json
from uuid import uuid4
import pytest
from app.user_settings import PrivateSettings
from app.credentials import SystemCredentials as NativeCredentials, CredentialError


class FakeCredentials:
    def __init__(self):
        self.values = {}
        self.fail_set_number = None
        self.writes = 0
        self.fail_delete = False
        self.bad_readback = False

    def get(self, reference):
        value = self.values.get(reference)
        return 'mismatched-value' if self.bad_readback and value else value

    def set(self, reference, secret):
        self.writes += 1
        if self.writes == self.fail_set_number:
            raise RuntimeError('synthetic-secret must never escape')
        self.values[reference] = secret

    def delete(self, reference):
        if self.fail_delete:
            raise RuntimeError('synthetic-secret cleanup failure')
        self.values.pop(reference, None)


def legacy(path, *, count=1, active=True):
    profiles = [
        dict(
            id=str(uuid4()),
            name=f'Profile {i}',
            base_url='https://example.invalid/v1',
            model='test-model',
            compare_model='',
            reasoning_effort='auto',
            max_output_tokens=256,
            api_key=f'synthetic-secret-{i}',
        )
        for i in range(count)
    ]
    data = dict(
        profiles=profiles,
        active_profile_id=profiles[0]['id'] if active else 'env',
        custom_prompt='',
    )
    path.write_text(json.dumps(data))
    return data


def test_legacy_load_is_lazy_and_migration_scrubs_manifest_only_after_verified_vault_write(
    tmp_path,
):
    path = tmp_path / 'private.json'
    old = legacy(path)
    before = path.read_bytes()
    vault = FakeCredentials()
    store = PrivateSettings(path, {'AI_API_KEY': 'env-synthetic'}, credentials=vault)
    assert vault.values == {} and path.read_bytes() == before
    store.initialize()
    saved = json.loads(path.read_text())
    assert saved['schema_version'] == 2
    assert 'synthetic-secret' not in path.read_text() and 'api_key' not in saved['profiles'][0]
    assert saved['active_profile_id'] == old['active_profile_id']
    assert store.values()['AI_API_KEY'] == 'synthetic-secret-0'
    assert 'credential_ref' not in json.dumps(store.view())
    assert 'synthetic-secret' not in json.dumps(store.view())
    restarted = PrivateSettings(path, {'AI_API_KEY': 'env-synthetic'}, credentials=vault)
    restarted.initialize()
    assert restarted.values()['AI_API_KEY'] == 'synthetic-secret-0'


@pytest.mark.parametrize('failure', ['second_write', 'readback', 'manifest'])
def test_failed_migration_preserves_original_file_and_does_not_fallback(
    tmp_path, monkeypatch, failure
):
    from app.credentials import CredentialError

    path = tmp_path / 'private.json'
    legacy(path, count=2)
    before = path.read_bytes()
    vault = FakeCredentials()
    store = PrivateSettings(path, {}, credentials=vault)
    if failure == 'second_write':
        vault.fail_set_number = 2
    if failure == 'readback':
        vault.bad_readback = True
    if failure == 'manifest':
        original = store._write_json

        def fail_manifest(target, data):
            if target == path:
                raise OSError('disk full')
            return original(target, data)

        monkeypatch.setattr(store, '_write_json', fail_manifest)
    with pytest.raises((CredentialError, OSError)) as error:
        store.initialize()
    assert 'synthetic-secret' not in str(error.value)
    assert path.read_bytes() == before and vault.values == {}
    with pytest.raises(CredentialError):
        store.values()


def test_failed_rotation_keeps_old_key_and_configuration_recoverable(tmp_path, monkeypatch):
    path = tmp_path / 'private.json'
    legacy(path)
    vault = FakeCredentials()
    store = PrivateSettings(path, {}, credentials=vault)
    store.initialize()
    before = path.read_bytes()
    original = store._write_json

    def fail_manifest(target, data):
        if target == path:
            raise OSError('disk full')
        original(target, data)

    monkeypatch.setattr(store, '_write_json', fail_manifest)
    candidate = deepcopy(store.data)
    candidate['profiles'][0]['api_key'] = 'replacement-synthetic'
    with pytest.raises(OSError):
        store.save(candidate)
    assert path.read_bytes() == before
    assert store.values()['AI_API_KEY'] == 'synthetic-secret-0'
    assert list(vault.values.values()) == ['synthetic-secret-0']


def test_cleanup_failure_retries_after_commit_without_plaintext_journal(tmp_path):
    path = tmp_path / 'private.json'
    legacy(path)
    vault = FakeCredentials()
    store = PrivateSettings(path, {}, credentials=vault)
    store.initialize()
    vault.fail_delete = True
    candidate = deepcopy(store.data)
    candidate['profiles'][0]['api_key'] = 'replacement-synthetic'
    store.save(candidate)
    assert store.values()['AI_API_KEY'] == 'replacement-synthetic'
    assert store.view()['credential_cleanup_pending'] is True
    for file in tmp_path.iterdir():
        assert (
            'synthetic-secret' not in file.read_text()
            and 'replacement-synthetic' not in file.read_text()
        )
    vault.fail_delete = False
    restarted = PrivateSettings(path, {}, credentials=vault)
    restarted.initialize()
    assert list(vault.values.values()) == ['replacement-synthetic']
    assert restarted.view()['credential_cleanup_pending'] is False


def test_missing_key_fails_closed_and_env_profile_needs_no_vault(tmp_path):
    from app.credentials import CredentialError

    path = tmp_path / 'private.json'
    legacy(path)
    vault = FakeCredentials()
    store = PrivateSettings(path, {'AI_API_KEY': 'env-synthetic'}, credentials=vault)
    store.initialize()
    vault.values.clear()
    with pytest.raises(CredentialError):
        store.values()
    assert store.values(id='env')['AI_API_KEY'] == 'env-synthetic'


def test_stale_instance_cannot_overwrite_new_manifest_or_credentials(tmp_path):
    from app.credentials import CredentialError

    path = tmp_path / 'private.json'
    legacy(path)
    vault = FakeCredentials()
    first = PrivateSettings(path, {}, credentials=vault)
    first.initialize()
    stale = PrivateSettings(path, {}, credentials=vault)
    stale.initialize()
    changed = deepcopy(first.data)
    changed['profiles'][0]['api_key'] = 'new-current-secret'
    first.save(changed)
    before = path.read_bytes()
    other = deepcopy(stale.data)
    other['profiles'][0]['api_key'] = 'stale-secret'
    with pytest.raises(CredentialError):
        stale.save(other)
    assert path.read_bytes() == before and first.values()['AI_API_KEY'] == 'new-current-secret'
    assert 'stale-secret' not in vault.values.values()


def test_cleanup_failure_does_not_disable_committed_profile_on_restart(tmp_path):
    path = tmp_path / 'private.json'
    legacy(path)
    vault = FakeCredentials()
    store = PrivateSettings(path, {}, credentials=vault)
    store.initialize()
    vault.fail_delete = True
    candidate = deepcopy(store.data)
    candidate['profiles'][0]['api_key'] = 'replacement-synthetic'
    store.save(candidate)
    restarted = PrivateSettings(path, {}, credentials=vault)
    restarted.initialize()
    assert restarted.values()['AI_API_KEY'] == 'replacement-synthetic'
    assert restarted.view()['credential_cleanup_pending'] is True


def test_precommit_crash_recovers_only_staged_references_before_retry(tmp_path):
    from app.credentials import CredentialError

    path = tmp_path / 'private.json'
    legacy(path, count=2)
    before = path.read_bytes()
    vault = FakeCredentials()
    vault.fail_set_number = 2
    vault.fail_delete = True
    store = PrivateSettings(path, {}, credentials=vault)
    with pytest.raises(CredentialError):
        store.initialize()
    assert path.read_bytes() == before and len(vault.values) == 1
    vault.fail_set_number = None
    vault.fail_delete = False
    resumed = PrivateSettings(path, {}, credentials=vault)
    resumed.initialize()
    assert sorted(vault.values.values()) == ['synthetic-secret-0', 'synthetic-secret-1']
    assert not resumed.view()['credential_cleanup_pending']


def test_filesync_failure_before_publish_retains_original_and_cleans_staging(tmp_path, monkeypatch):
    path = tmp_path / 'private.json'
    legacy(path)
    before = path.read_bytes()
    vault = FakeCredentials()
    store = PrivateSettings(path, {}, credentials=vault)

    def fail_sync(*args):
        raise OSError('fake fsync failure')

    monkeypatch.setattr('app.user_settings.os.fsync', fail_sync)
    with pytest.raises(OSError):
        store.initialize()
    assert path.read_bytes() == before and vault.values == {}


def test_foreign_installation_journal_never_deletes_its_credentials(tmp_path):
    from app.credentials import CredentialError

    path = tmp_path / 'private.json'
    legacy(path)
    vault = FakeCredentials()
    store = PrivateSettings(path, {}, credentials=vault)
    store.initialize()
    foreign = str(uuid4())
    ref = f'{foreign}:{uuid4()}'
    vault.values[ref] = 'foreign-secret'
    store.journal_path.write_text(
        json.dumps(
            dict(
                version=1,
                settings_id=store.settings_id,
                namespace=foreign,
                created=[ref],
                obsolete=[],
            )
        )
    )
    with pytest.raises(CredentialError):
        store.save(deepcopy(store.data))
    assert vault.values[ref] == 'foreign-secret'


def test_reference_presence_is_distinct_from_verified_or_missing_secret(tmp_path):
    from app.credentials import CredentialError

    path = tmp_path / 'private.json'
    legacy(path)
    vault = FakeCredentials()
    store = PrivateSettings(path, {}, credentials=vault)
    store.initialize()
    restarted = PrivateSettings(path, {}, credentials=vault)
    assert restarted.view()['profiles'][-1]['credential_status'] == 'saved_unverified'
    restarted.values()
    assert restarted.view()['profiles'][-1]['credential_status'] == 'available'
    vault.values.clear()
    with pytest.raises(CredentialError):
        restarted.values()
    assert restarted.view()['profiles'][-1]['credential_status'] == 'missing'


def test_postrename_sync_failure_keeps_both_keys_and_reports_pending_until_recovery(
    tmp_path, monkeypatch
):
    path = tmp_path / 'private.json'
    legacy(path)
    vault = FakeCredentials()
    store = PrivateSettings(path, {}, credentials=vault)
    store.initialize()
    original = store._sync_directory

    def fail_after_manifest(directory):
        if 'replacement-synthetic' in vault.values.values():
            raise OSError('directory sync failure')
        original(directory)

    monkeypatch.setattr(store, '_sync_directory', fail_after_manifest)
    candidate = deepcopy(store.data)
    candidate['profiles'][0]['api_key'] = 'replacement-synthetic'
    store.save(candidate)
    assert store.values()['AI_API_KEY'] == 'replacement-synthetic'
    assert sorted(vault.values.values()) == ['replacement-synthetic', 'synthetic-secret-0']
    assert store.view()['credential_durability_pending'] is True
    assert 'synthetic' not in path.read_text()
    restarted = PrivateSettings(path, {}, credentials=vault)
    restarted.initialize()
    assert list(vault.values.values()) == ['replacement-synthetic']


# Capture the production adapter before autouse fixture replaces its factory.


def test_native_adapter_is_lazy_and_rejects_null_backend(monkeypatch):
    import keyring
    from keyring.backends.null import Keyring

    monkeypatch.setattr(
        keyring, 'get_keyring', lambda: pytest.fail('constructor must not select a backend')
    )
    native = NativeCredentials()
    monkeypatch.setattr(keyring, 'get_keyring', lambda: Keyring())
    with pytest.raises(CredentialError, match='明文'):
        native.set('fixture-ref', 'synthetic-secret')


def test_native_exception_never_exposes_backend_message_or_secret(monkeypatch):
    import keyring

    def fail(*args):
        raise RuntimeError('synthetic-secret upstream diagnostic')

    backend = type(
        'Keyring',
        (),
        {
            '__module__': 'keyring.backends.macOS',
            'priority': 1,
            'get_password': fail,
            'set_password': fail,
            'delete_password': fail,
        },
    )()
    monkeypatch.setattr(keyring, 'get_keyring', lambda: backend)
    native = NativeCredentials()
    for method, args in [
        ('get', ('ref',)),
        ('set', ('ref', 'synthetic-secret')),
        ('delete', ('ref',)),
    ]:
        with pytest.raises(CredentialError) as error:
            getattr(native, method)(*args)
        assert 'synthetic-secret' not in str(error.value)


def test_unconfigured_env_store_never_selects_native_backend(tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(
        keyring, 'get_keyring', lambda: pytest.fail('env profile must not access OS vault')
    )
    path = tmp_path / 'private.json'
    store = PrivateSettings(path, {'AI_API_KEY': 'env-only'}, credentials=NativeCredentials())
    store.initialize()
    assert store.values()['AI_API_KEY'] == 'env-only'
    assert not path.exists()


def test_successful_metadata_commit_clears_previous_durability_warning(tmp_path, monkeypatch):
    path = tmp_path / 'private.json'
    vault = FakeCredentials()
    store = PrivateSettings(path, {}, credentials=vault)
    sync = store._sync_directory
    monkeypatch.setattr(
        store, '_sync_directory', lambda _: (_ for _ in ()).throw(OSError('directory sync'))
    )
    changed = deepcopy(store.data)
    changed['custom_prompt'] = 'first'
    store.save(changed)
    assert store.view()['credential_durability_pending']
    monkeypatch.setattr(store, '_sync_directory', sync)
    changed = deepcopy(store.data)
    changed['custom_prompt'] = 'second'
    store.save(changed)
    assert not store.view()['credential_durability_pending']


def test_copied_legacy_recovery_journal_cannot_clean_another_settings_path(tmp_path):
    first_path = tmp_path / 'first.json'
    legacy(first_path, count=2)
    vault = FakeCredentials()
    vault.fail_set_number = 2
    vault.fail_delete = True
    first = PrivateSettings(first_path, {}, credentials=vault)
    with pytest.raises(CredentialError):
        first.initialize()
    staged = dict(vault.values)
    other_path = tmp_path / 'other.json'
    legacy(other_path)
    other = PrivateSettings(other_path, {}, credentials=vault)
    other.journal_path.write_bytes(first.journal_path.read_bytes())
    vault.fail_delete = False
    vault.fail_set_number = None
    with pytest.raises(CredentialError):
        other.initialize()
    assert vault.values == staged
