from pg_test_support import test_database_url
"""Run with PG_TEST_DATABASE_URL against a migrated disposable PostgreSQL DB."""

import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def test_postgres_candidates_cover_tags_kind_and_fuzzy_identifiers(tmp_path):
    url = test_database_url()
    app = create_app(url, tmp_path / "uploads")
    with TestClient(app) as client:
        source = client.post("/api/v1/sources", json={"title": "Search test source"}).json()
        sid = source["id"]
        technique = client.post("/api/v1/techniques", json={"title": "Unrelated technique", "tags": ["水贴"], "source_ids": [sid]}).json()
        product = client.post("/api/v1/products", json={"name": "Unrelated product", "kind": "涂料", "brand": "Vallejo", "sku": "XF-12345", "source_ids": [sid]}).json()
        assert technique["id"] in [x["id"] for x in client.get("/api/v1/search", params={"q": "水贴"}).json()["techniques"]]
        for query in ("涂料", "Vallejp", "XF-12354"):
            assert product["id"] in [x["id"] for x in client.get("/api/v1/search", params={"q": query}).json()["products"]], query
