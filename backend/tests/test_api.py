from io import BytesIO
import json
import os
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.chat import evidence as chat_evidence
from sqlalchemy.orm import Session


@pytest.fixture
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'test.db'}", tmp_path / "uploads")
    with TestClient(app) as client:
        yield client


def source(client, title="说明书"):
    response = client.post("/api/v1/sources", json={"title": title, "url": "https://example.org/manual"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_source_and_technique_crud_keep_evidence(client):
    sid = source(client)
    response = client.post("/api/v1/techniques", json={"title": "修水口", "category": "素组", "summary": "先剪后磨", "steps": ["二次剪切", "打磨"], "source_ids": [sid], "tags": ["水口"]})
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["source_ids"] == [sid]
    assert item["status"] == "published"
    assert item["visibility"] == "private"
    assert client.get(f"/api/v1/techniques/{item['id']}").json()["steps"] == ["二次剪切", "打磨"]
    patched = client.patch(f"/api/v1/techniques/{item['id']}", json={"summary": "用砂纸打磨", "status": "withdrawn"})
    assert patched.status_code == 200
    assert patched.json()["summary"] == "用砂纸打磨"
    assert client.get("/api/v1/techniques").json()[0]["source_ids"] == [sid]


def test_records_require_real_sources(client):
    assert client.post("/api/v1/techniques", json={"title": "无出处", "source_ids": []}).status_code == 422
    assert client.post("/api/v1/products", json={"name": "错误", "source_ids": [999]}).status_code == 422


def test_product_sku_alias_uniqueness_and_search_order(client):
    sid = source(client)
    payload = {"brand": "田宫", "name": "基础白", "sku": "XF-2", "aliases": ["消光白"], "source_ids": [sid]}
    first = client.post("/api/v1/products", json=payload)
    assert first.status_code == 201, first.text
    assert client.post("/api/v1/products", json={**payload, "name": "另一个"}).status_code == 409
    assert client.post("/api/v1/products", json={**payload, "sku": "XF-3", "aliases": [" 消光白 "]}).status_code == 409
    assert client.get("/api/v1/search", params={"q": "xf-2"}).json()["products"][0]["id"] == first.json()["id"]
    assert client.get("/api/v1/search", params={"q": "消光白"}).json()["products"][0]["id"] == first.json()["id"]
    assert client.get("/api/v1/search", params={"q": "白"}).json()["products"][0]["id"] == first.json()["id"]
    assert client.patch(f"/api/v1/products/{first.json()['id']}", json={"status": "withdrawn"}).status_code == 200
    assert client.get("/api/v1/search", params={"q": "xf-2"}).json()["products"] == []


def test_product_patch_can_keep_alias_and_reject_duplicate_sku(client):
    sid = source(client)
    first = client.post("/api/v1/products", json={"name": "白色", "sku": "XF-2", "aliases": ["消光白"], "source_ids": [sid]}).json()
    second = client.post("/api/v1/products", json={"name": "黑色", "sku": "XF-1", "source_ids": [sid]}).json()
    assert client.patch(f"/api/v1/products/{first['id']}", json={"name": "新白色", "aliases": ["消光白"]}).status_code == 200
    assert client.patch(f"/api/v1/products/{second['id']}", json={"sku": " xf-2 "}).status_code == 409


def test_technique_search_hides_withdrawn_and_drafts(client):
    sid = source(client)
    for status in ("published", "withdrawn", "draft"):
        response = client.post("/api/v1/techniques", json={"title": "打磨", "status": "published" if status == "withdrawn" else status, "source_ids": [sid]})
        assert response.status_code == 201
        if status == "withdrawn":
            client.delete(f"/api/v1/techniques/{response.json()['id']}")
    assert len(client.get("/api/v1/search", params={"q": "打磨"}).json()["techniques"]) == 1


def test_withdrawn_source_removes_dependent_records_from_search(client):
    sid = source(client)
    client.post("/api/v1/techniques", json={"title": "无痕打磨", "source_ids": [sid]})
    client.post("/api/v1/products", json={"name": "砂纸", "source_ids": [sid]})
    assert len(client.get("/api/v1/search", params={"q": "打磨"}).json()["techniques"]) == 1
    assert client.delete(f"/api/v1/sources/{sid}").status_code == 200
    assert client.get("/api/v1/search", params={"q": "打磨"}).json()["techniques"] == []
    assert client.get("/api/v1/search", params={"q": "砂纸"}).json()["products"] == []


def test_markdown_import_preserves_source(client):
    response = client.post("/api/v1/imports/markdown", data={"source_title": "个人笔记", "source_url": "https://example.org/note"}, files={"file": ("notes.md", "# 喷涂\n\n先薄喷。".encode(), "text/markdown")})
    assert response.status_code == 201, response.text
    technique = response.json()["technique"]
    assert technique["title"] == "喷涂"
    sid = technique["source_ids"][0]
    assert client.get(f"/api/v1/sources/{sid}").json()["url"] == "https://example.org/note"


def test_image_upload_and_private_download(client):
    bad = client.post("/api/v1/attachments", files={"file": ("evil.svg", b"<svg/>", "image/svg+xml")})
    assert bad.status_code == 415
    huge = client.post("/api/v1/attachments", files={"file": ("large.png", b"\x89PNG\r\n\x1a\n" + b"x" * (5 * 1024 * 1024 + 1), "image/png")})
    assert huge.status_code == 413
    raw = b"\x89PNG\r\n\x1a\n" + b"tiny"
    response = client.post("/api/v1/attachments", files={"file": ("../../piece.png", raw, "image/png")})
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["filename"] == "piece.png"
    assert "path" not in item and "uploads" not in str(item)
    assert client.get(item["url"]).content == raw


def test_images_attach_to_records_and_survive_backup(client):
    sid = source(client)
    img = client.post("/api/v1/attachments", files={"file": ("x.png", b"\x89PNG\r\n\x1a\nxx", "image/png")}).json()
    technique = client.post("/api/v1/techniques", json={"title": "水口", "source_ids": [sid]}).json()
    patched = client.patch(f"/api/v1/techniques/{technique['id']}", json={"attachment_ids": [img["id"]]})
    assert patched.status_code == 200, patched.text
    assert patched.json()["attachment_ids"] == [img["id"]]
    product = client.post("/api/v1/products", json={"name": "砂纸", "source_ids": [sid], "attachment_ids": [img["id"]]})
    assert product.status_code == 201, product.text
    assert product.json()["attachment_ids"] == [img["id"]]
    archive = client.get("/api/v1/backup").content
    assert client.post("/api/v1/backup", files={"file": ("backup.zip", archive, "application/zip")}).status_code == 200
    assert client.get(f"/api/v1/techniques/{technique['id']}").json()["attachment_ids"] == [img["id"]]
    assert client.get(f"/api/v1/products/{product.json()['id']}").json()["attachment_ids"] == [img["id"]]


def test_unicode_image_filename_downloads_safely(client):
    image = client.post("/api/v1/attachments", files={"file": ("零件图.png", b"\x89PNG\r\n\x1a\nxx", "image/png")}).json()
    response = client.get(image["url"])
    assert response.status_code == 200
    assert response.content.startswith(b"\x89PNG")


def test_backup_restores_relations_and_attachments(client):
    sid = source(client)
    technique = client.post("/api/v1/techniques", json={"title": "旧技巧", "source_ids": [sid]}).json()
    img = client.post("/api/v1/attachments", files={"file": ("x.png", b"\x89PNG\r\n\x1a\nxx", "image/png")}).json()
    archive = client.get("/api/v1/backup")
    assert archive.status_code == 200
    client.patch(f"/api/v1/techniques/{technique['id']}", json={"title": "新技巧"})
    restored = client.post("/api/v1/backup", files={"file": ("backup.zip", archive.content, "application/zip")})
    assert restored.status_code == 200, restored.text
    assert client.get(f"/api/v1/techniques/{technique['id']}").json()["title"] == "旧技巧"
    assert client.get(f"/api/v1/sources/{sid}").status_code == 200
    assert client.get(img["url"]).content == b"\x89PNG\r\n\x1a\nxx"


def test_backup_rejects_zip_slip(client):
    from zipfile import ZipFile

    data = BytesIO()
    with ZipFile(data, "w") as zip_file:
        zip_file.writestr("../outside", "bad")
    response = client.post("/api/v1/backup", files={"file": ("bad.zip", data.getvalue(), "application/zip")})
    assert response.status_code == 400


def test_backup_rejects_duplicate_archive_entries(client):
    from zipfile import ZipFile

    client.post("/api/v1/attachments", files={"file": ("x.png", b"\x89PNG\r\n\x1a\nxx", "image/png")})
    original = client.get("/api/v1/backup").content
    data = BytesIO()
    with ZipFile(BytesIO(original)) as source_zip, ZipFile(data, "w") as zip_file:
        for name in source_zip.namelist():
            zip_file.writestr(name, source_zip.read(name))
        attachment_name = next(name for name in source_zip.namelist() if name.startswith("attachments/"))
        zip_file.writestr(attachment_name, source_zip.read(attachment_name))
    response = client.post("/api/v1/backup", files={"file": ("bad.zip", data.getvalue(), "application/zip")})
    assert response.status_code == 400


def test_backup_restore_preserves_source_retrieval_time(client):
    sid = source(client)
    before = client.get(f"/api/v1/sources/{sid}").json()["retrieved_at"]
    archive = client.get("/api/v1/backup").content
    response = client.post("/api/v1/backup", files={"file": ("backup.zip", archive, "application/zip")})
    assert response.status_code == 200, response.text
    assert client.get(f"/api/v1/sources/{sid}").json()["retrieved_at"] == before


def test_export_rejects_archive_too_large_to_restore(client, monkeypatch):
    source(client)
    client.app.state.backup_limit = 256
    response = client.get("/api/v1/backup")
    assert response.status_code == 413


def test_backup_limit_can_be_raised_for_large_library(tmp_path, monkeypatch):
    from app import main as main_module

    monkeypatch.setattr(main_module, "MAX_BACKUP", 256)
    monkeypatch.setenv("BACKUP_MAX_BYTES", "8192")
    app = create_app(f"sqlite:///{tmp_path / 'large.db'}", tmp_path / "images")
    with TestClient(app) as large_client:
        source(large_client)
        archive = large_client.get("/api/v1/backup")
        assert archive.status_code == 200, archive.text
        assert len(archive.content) > 256
        restored = large_client.post("/api/v1/backup", files={"file": ("backup.zip", archive.content, "application/zip")})
        assert restored.status_code == 200, restored.text


def test_restore_file_failure_keeps_previous_database_state(client, tmp_path, monkeypatch):
    sid = source(client)
    technique = client.post("/api/v1/techniques", json={"title": "备份标题", "source_ids": [sid]}).json()
    image = client.post("/api/v1/attachments", files={"file": ("x.png", b"\x89PNG\r\n\x1a\nxx", "image/png")}).json()
    archive = client.get("/api/v1/backup").content
    with ZipFile(BytesIO(archive)) as z:
        storage_name = json.loads(z.read("manifest.json"))["attachments"][0]["storage_name"]
    client.patch(f"/api/v1/techniques/{technique['id']}", json={"title": "当前标题"})
    original_replace = os.replace

    def fail_final_replace(src, dst):
        if Path(dst) == tmp_path / "uploads" / storage_name:
            raise OSError("simulated disk failure")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_final_replace)
    from fastapi.testclient import TestClient

    with TestClient(client.app, raise_server_exceptions=False) as failure_client:
        response = failure_client.post("/api/v1/backup", files={"file": ("backup.zip", archive, "application/zip")})
    assert response.status_code == 500
    assert client.get(f"/api/v1/techniques/{technique['id']}").json()["title"] == "当前标题"
    assert client.get(image["url"]).status_code == 200


def test_restore_second_image_failure_restores_first_image(client, tmp_path, monkeypatch):
    sid = source(client)
    technique = client.post("/api/v1/techniques", json={"title": "备份标题", "source_ids": [sid]}).json()
    images = [client.post("/api/v1/attachments", files={"file": (f"{i}.png", b"\x89PNG\r\n\x1a\n" + bytes([i]), "image/png")}).json() for i in (1, 2)]
    archive = client.get("/api/v1/backup").content
    with ZipFile(BytesIO(archive)) as z:
        names = [x["storage_name"] for x in json.loads(z.read("manifest.json"))["attachments"]]
    current_bytes = b"\x89PNG\r\n\x1a\ncurrent"
    (tmp_path / "uploads" / names[0]).write_bytes(current_bytes)
    client.patch(f"/api/v1/techniques/{technique['id']}", json={"title": "当前标题"})
    original_replace = os.replace
    original_copy = __import__("shutil").copy2
    replacements = []

    def fail_second_incoming(src, dst):
        if Path(src).parent.name == "incoming":
            replacements.append(Path(dst).name)
            if Path(dst).name == names[1]:
                raise OSError("simulated second write failure")
        return original_replace(src, dst)

    def fail_copy_rollback(src, dst, *args, **kwargs):
        if Path(src).parent.name == "previous":
            raise OSError("simulated full disk")
        return original_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_second_incoming)
    monkeypatch.setattr("shutil.copy2", fail_copy_rollback)
    with TestClient(client.app, raise_server_exceptions=False) as failure_client:
        response = failure_client.post("/api/v1/backup", files={"file": ("backup.zip", archive, "application/zip")})
    assert response.status_code == 500
    assert replacements == names
    assert client.get(f"/api/v1/techniques/{technique['id']}").json()["title"] == "当前标题"
    assert client.get(images[0]["url"]).content == current_bytes


def test_original_technique_creates_local_evidence_and_survives_restore(client):
    response = client.post('/api/v1/techniques', json={'title': '原创打磨', 'original': True})
    assert response.status_code == 201, response.text
    item = response.json()
    assert item['status'] == 'published'
    assert len(item['source_ids']) == 1
    evidence = client.get(f"/api/v1/sources/{item['source_ids'][0]}").json()
    assert evidence['source_type'] == 'original'
    assert evidence['title'] == '原创：原创打磨'
    assert evidence['author'] == '我'
    assert evidence['url'] is None
    assert evidence['status'] == 'published'
    with Session(client.app.state.chat.engine) as db:
        assert chat_evidence(db, '原创打磨')[0]['id'] == item['id']
    assert client.get('/api/v1/search?q=原创打磨').json()['techniques'][0]['id'] == item['id']
    archive = client.get('/api/v1/backup').content
    client.delete(f"/api/v1/techniques/{item['id']}")
    assert client.get('/api/v1/search?q=原创打磨').json()['techniques'] == []
    with Session(client.app.state.chat.engine) as db:
        assert chat_evidence(db, '原创打磨') == []
    assert client.patch(f"/api/v1/techniques/{item['id']}", json={'summary': '修订'}).json()['status'] == 'withdrawn'
    assert client.patch(f"/api/v1/techniques/{item['id']}", json={'status': 'published'}).status_code == 200
    assert client.get('/api/v1/search?q=原创打磨').json()['techniques'][0]['id'] == item['id']
    assert client.post('/api/v1/backup', files={'file': ('backup.zip', archive, 'application/zip')}).status_code == 200
    assert client.get(f"/api/v1/sources/{evidence['id']}").json()['source_type'] == 'original'


def test_new_technique_cannot_be_withdrawn_or_ambiguously_original(client):
    sid = source(client)
    for payload in ({'source_ids': [sid], 'status': 'withdrawn'}, {'original': True, 'status': 'withdrawn'}, {'original': True, 'source_ids': [sid]}):
        assert client.post('/api/v1/techniques', json={'title': '打磨', **payload}).status_code == 422
    assert len(client.get('/api/v1/sources').json()) == 1


def test_failed_original_creation_does_not_leave_orphan_source(client):
    assert client.post('/api/v1/techniques', json={'title': '失败', 'original': True, 'attachment_ids': [999]}).status_code == 422
    assert client.get('/api/v1/sources').json() == []
