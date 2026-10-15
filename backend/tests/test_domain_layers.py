"""Domain contracts keep HTTP, persistence and model orchestration separate."""

import ast
import importlib
from pathlib import Path

import pytest


@pytest.mark.parametrize('domain', ['chat', 'experiments', 'settings'])
def test_domain_layers_do_not_cross_transport_and_storage_boundaries(domain):
    root = Path(__file__).parents[1] / 'app'
    for layer, forbidden in (
        ('api', {'sqlalchemy'}),
        ('services', {'fastapi', 'sqlalchemy'}),
        ('repositories', {'fastapi', 'app.llm', 'langchain_openai'}),
    ):
        path = root / layer / f'{domain}.py'
        tree = ast.parse(path.read_text())
        imported = {
            node.module or '' for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not any(
            name == prefix or name.startswith(prefix + '.')
            for name in imported
            for prefix in forbidden
        )


def test_legacy_imports_keep_one_shared_service_and_settings_class():
    for old, new, name in (
        ('app.chat', 'app.services.chat', 'ChatService'),
        ('app.experiments', 'app.services.experiments', 'ExperimentService'),
        ('app.user_settings', 'app.repositories.settings', 'PrivateSettings'),
    ):
        assert getattr(importlib.import_module(old), name) is getattr(
            importlib.import_module(new), name
        )
    from app import user_settings
    from app.repositories import settings

    assert user_settings.os is settings.os


def test_experiment_domain_error_preserves_http_contract(tmp_path):
    from fastapi.testclient import TestClient
    from test_chat import setup

    with TestClient(setup(tmp_path)) as client:
        response = client.post(
            '/api/v1/experiments',
            json={'dataset_id': 'demo-v1', 'mode': 'retrieval', 'request_id': 'unconfigured'},
        )
        assert response.status_code == 503
        assert 'LangSmith' in response.json()['detail']
        assert client.get('/api/v1/experiments').json() == []
