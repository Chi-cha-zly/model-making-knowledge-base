from pg_test_support import test_database_url
"""Settings tests use isolated fake credentials and never contact an upstream."""
import json
import stat
from fastapi.testclient import TestClient
import pytest
from app.main import create_app

FAKE = {'AI_BASE_URL':'https://example.invalid/v1','AI_MODEL':'fake','AI_API_KEY':'env-test-secret','LANGSMITH_TRACING':'false'}
PAYLOAD = dict(name='Local', base_url='http://127.0.0.1:9999/v1', model='fake', compare_model='', reasoning_effort='auto', max_output_tokens=256, api_key='saved-test-secret')

def make(tmp_path):
    return create_app(test_database_url(f"{tmp_path/'db.sqlite'}"), tmp_path/'uploads', settings_path=tmp_path/'private.json', runtime_settings=FAKE)

def test_crud_private_persistence_and_key_ownership(tmp_path):
    app=make(tmp_path)
    with TestClient(app) as c:
        assert c.get('/api/v1/settings').status_code == 200
        view=c.post('/api/v1/settings/profiles',json=PAYLOAD).json()
        p=view['profiles'][-1]; id=p['id']
        assert p['has_api_key'] and 'saved-test-secret' not in json.dumps(view)
        assert stat.S_IMODE((tmp_path/'private.json').stat().st_mode)==0o600
        changed={**PAYLOAD,'base_url':'https://other.invalid/v1'}; changed.pop('api_key')
        assert c.put(f'/api/v1/settings/profiles/{id}',json=changed).status_code==422
        changed['api_key']='replacement-secret'
        assert c.put(f'/api/v1/settings/profiles/{id}',json=changed).status_code==200
        assert c.post(f'/api/v1/settings/profiles/{id}/activate',json={}).status_code==200
        assert c.request('DELETE',f'/api/v1/settings/profiles/{id}',json={}).status_code==409
        assert app.state.chat.provider.reasoning_effort is None
        assert c.get('/api/v1/integrations/status').json()['reasoning_effort']=='auto'
    with TestClient(make(tmp_path)) as c:
        view=c.get('/api/v1/settings').json()
        assert view['active_profile_id']==id
        assert c.post('/api/v1/settings/profiles/env/activate',json={}).status_code==200
        assert c.request('DELETE',f'/api/v1/settings/profiles/{id}',json={}).status_code==200

def test_validation_and_origin_never_echo_key(tmp_path):
    with TestClient(make(tmp_path)) as c:
        bad={**PAYLOAD,'max_output_tokens':3}
        response=c.post('/api/v1/settings/profiles',json=bad)
        assert response.status_code==422 and 'saved-test-secret' not in response.text
        assert c.post('/api/v1/settings/profiles',json=PAYLOAD,headers={'Origin':'https://foreign.invalid'}).status_code==403
        assert c.post('/api/v1/settings/profiles',content='{}',headers={'Content-Type':'text/plain'}).status_code==415

def test_prompt_and_unfinished_tasks_guard(tmp_path):
    app=make(tmp_path)
    with TestClient(app) as c:
        assert c.put('/api/v1/settings/prompt',json={'text':'Use simple words.'}).status_code==200
        assert 'Use simple words.' in app.state.chat.system_prompt
        assert app.state.experiments.system_prompt==app.state.chat.system_prompt
        class Pending:
            def done(self): return False
        app.state.chat.tasks[999]=Pending()
        assert c.put('/api/v1/settings/prompt',json={'text':''}).status_code==409
        app.state.chat.tasks.clear()

def test_invalid_private_file_is_preserved(tmp_path):
    (tmp_path/'private.json').write_text('{broken')
    with pytest.raises(ValueError,match='private settings'):
        make(tmp_path)
    assert (tmp_path/'private.json').read_text()=='{broken'

def test_connection_probe_is_sanitized_untraced_and_bounded(tmp_path,monkeypatch):
    from app.llm import LangChainProvider
    captured=[]
    async def fail(self,messages,**kw):
        captured.append((self.max_output_tokens,messages,kw))
        raise RuntimeError('saved-test-secret raw upstream response')
    monkeypatch.setattr(LangChainProvider,'complete',fail)
    with TestClient(make(tmp_path)) as c:
        id=c.post('/api/v1/settings/profiles',json=PAYLOAD).json()['profiles'][-1]['id']
        response=c.post(f'/api/v1/settings/profiles/{id}/test',json={})
        assert response.json()['ok'] is False
        assert 'saved-test-secret' not in response.text and 'raw upstream' not in response.text
        assert captured[0][0]==256 and captured[0][2]['trace'] is False

def test_backup_excludes_private_settings_and_restore_preserves_them(tmp_path):
    from io import BytesIO
    from zipfile import ZipFile
    with TestClient(make(tmp_path)) as c:
        c.post('/api/v1/settings/profiles',json=PAYLOAD)
        c.put('/api/v1/settings/prompt',json={'text':'Saved preference'})
        response=c.get('/api/v1/backup')
        assert response.status_code==200
        with ZipFile(BytesIO(response.content)) as archive:
            for name in archive.namelist():
                assert 'saved-test-secret' not in archive.read(name).decode('utf-8')
                assert 'private' not in name
        before=(tmp_path/'private.json').read_bytes()
        assert c.post('/api/v1/backup',files={'file':('backup.zip',response.content,'application/zip')}).status_code==200
        assert (tmp_path/'private.json').read_bytes()==before

def test_clear_key_and_persist_failure_keep_old_state(tmp_path,monkeypatch):
    app=make(tmp_path)
    with TestClient(app) as c:
        id=c.post('/api/v1/settings/profiles',json=PAYLOAD).json()['profiles'][-1]['id']
        c.post(f'/api/v1/settings/profiles/{id}/activate',json={})
        old=app.state.chat.provider
        original=(tmp_path/'private.json').read_bytes()
        def failed_replace(*args): raise OSError('fake disk full')
        with monkeypatch.context() as patch:
            patch.setattr('app.user_settings.os.replace',failed_replace)
            with pytest.raises(OSError):
                c.put(f'/api/v1/settings/profiles/{id}',json={**PAYLOAD,'model':'changed'})
        assert app.state.chat.provider is old
        assert (tmp_path/'private.json').read_bytes()==original
        cleared=c.put(f'/api/v1/settings/profiles/{id}',json={**PAYLOAD,'api_key':''})
        assert cleared.status_code==200
        assert not cleared.json()['profiles'][-1]['has_api_key']
        assert app.state.chat.provider.api_key==''

def test_chat_prompt_sent_with_fixed_rules(tmp_path):
    import time
    from app.chat import SYSTEM
    class Capture:
        configured=True
        model='fake'
        def __init__(self): self.messages=[]
        async def stream(self,messages):
            self.messages=messages
            yield 'fake answer'
    provider=Capture()
    app=create_app(test_database_url(f"{tmp_path/'db.sqlite'}"),tmp_path/'uploads',provider=provider,settings_path=tmp_path/'private.json',runtime_settings=FAKE)
    with TestClient(app) as c:
        c.put('/api/v1/settings/prompt',json={'text':'Explain each term.'})
        cid=c.post('/api/v1/conversations',json={}).json()['id']
        rid=c.post(f'/api/v1/conversations/{cid}/runs',json={'content':'如何制作？','request_id':'fake'}).json()['id']
        for _ in range(100):
            if c.get(f'/api/v1/runs/{rid}').json()['status']=='completed': break
            time.sleep(.01)
        assert provider.messages[0]['content'].startswith(SYSTEM)
        assert 'Explain each term.' in provider.messages[0]['content']

@pytest.mark.anyio
async def test_experiment_prompt_frozen_and_legacy_backup_accepted(tmp_path):
    from app.experiments import ExperimentInput,export_experiments,validate_experiment_backup,fingerprint
    from test_experiments import service,Provider
    from sqlalchemy.orm import Session
    class Capture(Provider):
        def __init__(self): self.messages=[]
        async def complete(self,messages,**kw):
            self.messages.append(messages)
            return await super().complete(messages,**kw)
    provider=Capture(); s=service(tmp_path,provider)
    s.system_prompt='fixed system plus fake preference'
    row=await s.start(ExperimentInput(dataset_id='demo-v1',mode='answers',request_id='prompt'))
    s.system_prompt='later preference'
    await s.tasks[row['id']]
    assert provider.messages[0][0]['content']=='fixed system plus fake preference'
    with Session(s.engine) as db: backup=export_experiments(db)
    backup['version']=4
    validate_experiment_backup(backup)
    record=backup['experiments'][0]
    assert record['snapshot']['system_prompt']=='fixed system plus fake preference'
    del record['snapshot']['system_prompt']
    record['payload']['snapshot_hash']=fingerprint(record['snapshot'])
    backup['version']=4
    validate_experiment_backup(backup)

@pytest.fixture
def anyio_backend(): return 'asyncio'

@pytest.mark.parametrize('endpoint',['http://127.0.0.2:9999/v1','http://[::1]:9999/v1'])
def test_loopback_profiles_can_omit_key(tmp_path,endpoint):
    with TestClient(make(tmp_path)) as c:
        response=c.post('/api/v1/settings/profiles',json={**PAYLOAD,'base_url':endpoint,'api_key':''})
        assert response.status_code==200
        id=response.json()['profiles'][-1]['id']
        assert c.post(f'/api/v1/settings/profiles/{id}/activate',json={}).status_code==200
        assert c.get('/api/v1/agent/config').json()['configured'] is True

def test_omitted_or_null_key_keeps_saved_secret(tmp_path):
    with TestClient(make(tmp_path)) as c:
        id=c.post('/api/v1/settings/profiles',json=PAYLOAD).json()['profiles'][-1]['id']
        omitted={**PAYLOAD,'name':'Renamed'}; omitted.pop('api_key')
        for payload in (omitted,{**omitted,'api_key':None}):
            response=c.put(f'/api/v1/settings/profiles/{id}',json=payload)
            assert response.status_code==200 and response.json()['profiles'][-1]['has_api_key']
        assert json.loads((tmp_path/'private.json').read_text())['profiles'][0]['api_key']=='saved-test-secret'

def test_persisted_active_chat_blocks_settings(tmp_path):
    import asyncio
    from sqlalchemy.orm import Session
    from sqlalchemy import select
    from app.chat_models import Run
    class Block:
        configured=True
        model='fake'
        async def stream(self,messages):
            yield 'partial'
            await asyncio.Event().wait()
    app=create_app(test_database_url(f"{tmp_path/'db.sqlite'}"),tmp_path/'uploads',provider=Block(),settings_path=tmp_path/'private.json',runtime_settings=FAKE)
    with TestClient(app) as c:
        cid=c.post('/api/v1/conversations',json={}).json()['id']
        rid=c.post(f'/api/v1/conversations/{cid}/runs',json={'content':'fake','request_id':'busy'}).json()['id']
        with Session(app.state.chat.engine) as db:
            assert db.scalar(select(Run.status).where(Run.id==rid)) in ('queued','running')
        assert c.put('/api/v1/settings/prompt',json={'text':'blocked'}).status_code==409
        assert c.post('/api/v1/settings/profiles',json=PAYLOAD).status_code==409
        c.post(f'/api/v1/runs/{rid}/stop')
        assert c.put('/api/v1/settings/prompt',json={'text':'allowed'}).status_code==200

def test_trusted_dev_proxy_origin_with_backend_host(tmp_path):
    with TestClient(make(tmp_path)) as c:
        assert c.put('/api/v1/settings/prompt',json={'text':'proxy'},headers={'Origin':'http://127.0.0.1:5174','Host':'127.0.0.1:8011'}).status_code==200
        assert c.put('/api/v1/settings/prompt',json={'text':'bad'},headers={'Origin':'http://127.0.0.1:5999','Host':'127.0.0.1:8011'}).status_code==403

def test_injected_provider_preserved_with_saved_active_profile(tmp_path):
    class Injected:
        configured=True
        model='test-provider'
    with TestClient(make(tmp_path)) as c:
        id=c.post('/api/v1/settings/profiles',json=PAYLOAD).json()['profiles'][-1]['id']
        c.post(f'/api/v1/settings/profiles/{id}/activate',json={})
    provider=Injected()
    app=create_app(test_database_url(f"{tmp_path/'db.sqlite'}"),tmp_path/'uploads',provider=provider,settings_path=tmp_path/'private.json',runtime_settings=FAKE)
    assert app.state.chat.provider is provider
    assert app.state.experiments.provider is provider
