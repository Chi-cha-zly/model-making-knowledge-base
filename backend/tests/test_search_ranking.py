"""The catalog search must use the same ranking as conversation retrieval."""
from fastapi.testclient import TestClient
from app.main import create_app


def test_catalog_search_understands_natural_questions_and_step_content(tmp_path):
    with TestClient(create_app(f"sqlite:///{tmp_path / 'catalog.db'}", tmp_path / 'files')) as client:
        source = client.post('/api/v1/sources', json={'title': '检索集成测试出处'}).json()
        target = client.post('/api/v1/techniques', json={
            'title': '剪切后的表面处理', 'summary': '先逐步清理，再检查表面。',
            'steps': ['用砂纸分级打磨水口，检查残留。'], 'source_ids': [source['id']],
        }).json()
        response = client.get('/api/v1/search', params={'q': '新手应该怎样打磨水口？'})
        assert response.status_code == 200
        assert response.json()['techniques'][0]['id'] == target['id']
        client.patch(f"/api/v1/sources/{source['id']}", json={'status': 'withdrawn'})
        assert client.get('/api/v1/search', params={'q': '新手应该怎样打磨水口？'}).json() == {'techniques': [], 'products': [], 'ranking': []}


def test_catalog_search_limits_unbounded_queries(tmp_path):
    with TestClient(create_app(f"sqlite:///{tmp_path / 'catalog.db'}", tmp_path / 'files')) as client:
        assert client.get('/api/v1/search', params={'q': 'x' * 2001}).status_code == 422
        assert client.get('/api/v1/search', params={'q': '   '}).json() == {'techniques': [], 'products': [], 'ranking': []}


def test_catalog_search_keeps_global_rank_across_record_types(tmp_path):
    with TestClient(create_app(f"sqlite:///{tmp_path / 'catalog.db'}", tmp_path / 'files')) as client:
        source = client.post('/api/v1/sources', json={'title': '编号检索出处'}).json()
        technique = client.post('/api/v1/techniques', json={
            'title': '涂装步骤', 'summary': '这里提到 XF-2 的一个用例', 'source_ids': [source['id']],
        }).json()
        product = client.post('/api/v1/products', json={
            'name': '示例白色', 'sku': 'XF-2', 'source_ids': [source['id']],
        }).json()
        response = client.get('/api/v1/search', params={'q': 'XF-2'}).json()
        assert response['ranking'][0] == f"product:{product['id']}"
        assert f"technique:{technique['id']}" in response['ranking']
