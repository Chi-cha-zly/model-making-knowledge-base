from pg_test_support import test_database_url
from fastapi.testclient import TestClient
from app.llm import LangChainProvider
from app.main import create_app


def test_default_app_uses_langchain_provider_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr('app.llm.read_settings', lambda: {
        'AI_BASE_URL':'https://example.org/v1', 'AI_MODEL':'controlled-model',
        'AI_API_KEY':'unit-test-only', 'LANGSMITH_TRACING':'false',
    })
    app=create_app(test_database_url(f"{tmp_path / 'default.db'}"), tmp_path / 'uploads')
    assert isinstance(app.state.chat.provider, LangChainProvider)
    with TestClient(app) as client:
        status=client.get('/api/v1/agent/config').json()
        assert status['configured'] is True and status['model']=='controlled-model'
        assert 'unit-test-only' not in client.get('/api/v1/integrations/status').text
