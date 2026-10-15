"""The published pytest command must never inherit operator model configuration."""

from pathlib import Path


def test_default_configuration_isolated_before_app_and_provider_imports(tmp_path, monkeypatch):
    poisoned = {
        'AI_BASE_URL': 'https://poison.invalid/v1',
        'AI_MODEL': 'poison',
        'AI_API_KEY': 'synthetic-secret',
        'EMBEDDING_BASE_URL': 'https://poison.invalid/v1',
        'EMBEDDING_MODEL': 'text-embedding-v4',
        'EMBEDDING_API_KEY': 'synthetic-secret',
        'VISION_BASE_URL': 'https://poison.invalid/v1',
        'VISION_MODEL': 'poison',
        'VISION_API_KEY': 'synthetic-secret',
        'LANGSMITH_API_KEY': 'synthetic-secret',
        'LANGSMITH_TRACING': 'true',
        'RETRIEVAL_MODE': 'hybrid',
    }
    for key, value in poisoned.items():
        monkeypatch.setenv(key, value)
    original = Path.read_text
    reads = []

    def synthetic_env(path, *args, **kwargs):
        if path.name == '.env':
            reads.append(path)
            return '\n'.join(
                f'{key}={value}'
                for key, value in {**poisoned, 'DATA_DIR': str(tmp_path / 'module-images')}.items()
            )
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', synthetic_env)
    from app.llm import LangChainProvider
    import app.llm
    import app.runtime_config
    import app.services.runtime

    assert app.llm.read_settings is app.runtime_config.read_settings
    assert app.services.runtime.read_settings is app.runtime_config.read_settings
    from app.main import create_app
    from pg_test_support import test_database_url

    provider = LangChainProvider.from_env()
    assert not provider.configured
    assert not provider.embedding_configured
    app = create_app(test_database_url(), tmp_path / 'images')
    assert not app.state.chat.provider.configured
    assert not app.state.chat.provider.embedding_configured
    assert app.state.retrieval.mode == 'lexical'
    assert not reads, 'default model configuration must not read any .env'


def test_explicit_configuration_and_backup_limit_remain_testable(tmp_path, monkeypatch):
    from app.runtime_config import read_settings

    exists = Path.exists
    monkeypatch.setattr(Path, 'exists', lambda path: False if path.name == '.env' else exists(path))
    path = tmp_path / 'isolated.env'
    path.write_text('AI_MODEL="fixture"\n')
    monkeypatch.setenv('AI_API_KEY', 'synthetic-ambient')
    monkeypatch.setenv('BACKUP_MAX_BYTES', '64')
    explicit = read_settings(path, {'AI_MODEL': ' "override" '})
    assert explicit == {'AI_MODEL': 'override'}
    assert read_settings(environ={'AI_MODEL': 'injected'}) == {'AI_MODEL': 'injected'}
    assert read_settings()['BACKUP_MAX_BYTES'] == '64'
