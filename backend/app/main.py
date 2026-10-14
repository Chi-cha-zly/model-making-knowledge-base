"""Local knowledge base API. Run migrations before starting against PostgreSQL."""

import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.parse import quote
from zipfile import ZipFile

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .models import Attachment, Base, Product, ProductAlias, Source, Technique, product_attachments, product_sources, technique_attachments, technique_sources
from .chat import ChatService, install_chat, export_chat, validate_chat_backup, restore_chat
from .chat_models import Conversation, Message, Run
from .llm import LangChainProvider
from .evaluation import install_evaluation, export_evaluation, validate_evaluation_backup, restore_evaluation
from .experiments import ExperimentService, install_experiments, export_experiments, validate_experiment_backup, restore_experiments
from .retrieval import search_documents
from .ingestion import backfill, clear_index, ingest_record
from .ingestion_models import Document, IngestionJob
from .content import content_adapter, derived_steps, image_ids
from .schemas import ProductInput, ProductPatch, SourceInput, SourcePatch, TechniqueInput, TechniquePatch


API = "/api/v1"
MAX_IMAGE = 5 * 1024 * 1024
MAX_BACKUP = 100 * 1024 * 1024
IMAGE_TYPES = {"image/png": (b"\x89PNG\r\n\x1a\n", ".png"), "image/jpeg": (b"\xff\xd8\xff", ".jpg"), "image/gif": (b"GIF8", ".gif"), "image/webp": (b"RIFF", ".webp")}


def _config():
    values = {}
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    root = Path(__file__).resolve().parents[2]
    directory = Path(os.environ.get("DATA_DIR") or values.get("DATA_DIR") or root / "data" / "attachments")
    if not directory.is_absolute():
        directory = root / directory
    backup_value = os.environ.get("BACKUP_MAX_BYTES") or values.get("BACKUP_MAX_BYTES")
    backup_limit = int(backup_value) if backup_value else MAX_BACKUP
    if backup_limit <= 0:
        raise ValueError("BACKUP_MAX_BYTES must be a positive integer")
    return (os.environ.get("DATABASE_URL") or values.get("DATABASE_URL") or f"sqlite:///{root / 'data' / 'knowledge.db'}",
            str(directory), backup_limit)


def _key(value: str | None) -> str:
    return (value or "").strip().casefold()


def _source_out(row):
    return {"id": row.id, "title": row.title, "url": row.url, "source_type": row.source_type,
            "author": row.author, "license_note": row.license_note, "retrieved_at": row.retrieved_at.isoformat() if row.retrieved_at else None,
            "content_hash": row.content_hash, "owner_id": row.owner_id, "visibility": row.visibility, "status": row.status}


def _technique_out(row):
    return {"id": row.id, "title": row.title, "category": row.category, "summary": row.summary,
            "steps": row.steps or [], "content_blocks": row.content_blocks or [], "prerequisites": row.prerequisites, "risks": row.risks,
            "difficulty": row.difficulty, "tags": row.tags or [], "source_ids": [s.id for s in row.sources],
            "attachment_ids": [a.id for a in row.attachments],
            "owner_id": row.owner_id, "visibility": row.visibility, "status": row.status}


def _product_out(row):
    return {"id": row.id, "brand": row.brand, "name": row.name, "sku": row.sku, "kind": row.kind,
            "aliases": [a.alias for a in row.aliases], "source_ids": [s.id for s in row.sources],
            "attachment_ids": [a.id for a in row.attachments],
            "properties": row.properties or {}, "manufacturer_url": row.manufacturer_url,
            "owner_id": row.owner_id, "visibility": row.visibility, "status": row.status}


def _attachment_out(row):
    return {"id": row.id, "filename": row.filename, "content_type": row.content_type,
            "size": row.size, "url": f"{API}/attachments/{row.id}"}


def _sources(db, ids):
    rows = db.scalars(select(Source).where(Source.id.in_(ids), Source.owner_id == "local", Source.status != "withdrawn")).all()
    if len(set(ids)) != len(ids) or len(rows) != len(ids):
        raise HTTPException(422, "source_ids must refer to existing, active sources")
    return rows


def _attachments(db, ids):
    rows = db.scalars(select(Attachment).where(Attachment.id.in_(ids), Attachment.owner_id == "local", Attachment.status == "published")).all()
    if len(set(ids)) != len(ids) or len(rows) != len(ids):
        raise HTTPException(422, "attachment_ids must refer to existing, active attachments")
    return rows


def _get(db, model, id):
    row = db.get(model, id)
    if row is None or row.owner_id != "local":
        raise HTTPException(404, "Not found")
    return row


def _aliases(db, product, aliases):
    brand_key = _key(product.brand)
    keys = set()
    output = []
    existing = {_key(item.alias): item for item in product.aliases}
    for alias in aliases:
        clean = alias.strip()
        key = brand_key + ":" + _key(clean)
        if not clean or key in keys:
            raise HTTPException(422, "Aliases must be nonempty and distinct")
        keys.add(key)
        item = existing.get(_key(clean)) or ProductAlias()
        item.alias = clean
        item.alias_key = key
        output.append(item)
    product.aliases = output


def _save(db):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Duplicate SKU or alias")


async def _spool_backup(file: UploadFile, storage: Path, limit: int) -> TemporaryDirectory:
    temporary = TemporaryDirectory(dir=storage, prefix="backup-upload-")
    try:
        total = 0
        with (Path(temporary.name) / "backup.zip").open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise HTTPException(413, "Backup exceeds configured size limit")
                output.write(chunk)
        return temporary
    except Exception:
        temporary.cleanup()
        raise


def create_app(database_url: str | None = None, storage_dir: str | Path | None = None,
               initialize_sqlite: bool = True, provider=None, settings_path=None, runtime_settings=None) -> FastAPI:
    default_db, default_storage, backup_limit = _config()
    database_url = database_url or default_db
    storage = Path(storage_dir or default_storage).resolve()
    storage.mkdir(parents=True, exist_ok=True)
    if database_url.startswith("sqlite"):
        Path(database_url.removeprefix("sqlite:///")) .parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, pool_pre_ping=True)
    if database_url.startswith("sqlite") and initialize_sqlite:
        Base.metadata.create_all(engine)
    chat = ChatService(engine, provider if provider is not None else (LangChainProvider(runtime_settings) if runtime_settings is not None else LangChainProvider.from_env()))
    from .user_settings import PrivateSettings, compose_prompt, install_settings
    # Provider settings preserve the existing injectable environment seam.
    from .runtime_config import read_settings
    env = dict(runtime_settings) if runtime_settings is not None else dict(getattr(chat.provider, 'settings', None) or read_settings())
    private_path = Path(settings_path).resolve() if settings_path is not None else storage.parent / '.modelkb-private-settings.json'
    if private_path == storage or storage in private_path.parents:
        raise ValueError('Private settings must be outside attachment storage')
    user_settings = PrivateSettings(private_path, env)
    if provider is None and (runtime_settings is not None or user_settings.data['active_profile_id'] != 'env'):
        chat.provider = LangChainProvider(user_settings.values())
    experiments = ExperimentService(engine, chat.provider, chat.lock, settings=user_settings.values())
    chat.system_prompt = experiments.system_prompt = compose_prompt(user_settings.data['custom_prompt'])
    @asynccontextmanager
    async def lifespan(app):
        # Upgrade legacy data once at application startup, never during search.
        with Session(engine) as db:
            backfill(db, missing_only=True)
            db.commit()
        experiments.recover()
        async with chat.lifespan(app):
            try:
                yield
            finally:
                async with chat.lock:
                    for id in list(experiments.tasks):
                        await experiments.stop(id)
    app = FastAPI(title="模型制作知识库 API", version="0.4.0", lifespan=lifespan)
    install_settings(app, user_settings, chat, experiments)
    install_experiments(app, experiments)
    install_chat(app, chat)
    install_evaluation(app, engine)
    app.state.backup_limit = backup_limit
    app.state.engine = engine

    @contextmanager
    def session():
        with Session(engine) as db:
            yield db

    @app.get(f"{API}/ingestion/jobs")
    def ingestion_jobs():
        with session() as db:
            return [{"id": row.id, "document_id": row.document_id, "version": row.version,
                     "status": row.status, "progress": row.progress, "error": row.error}
                    for row in db.scalars(select(IngestionJob).order_by(IngestionJob.id.desc()).limit(100))]

    @app.post(f"{API}/ingestion/backfill")
    def ingestion_backfill():
        with session() as db:
            count = backfill(db)
            db.commit()
            return {"indexed": count, "embeddings": "pending"}

    @app.post(f"{API}/ingestion/documents/{{id}}/retry")
    def retry_ingestion(id: int):
        with session() as db:
            document = db.get(Document, id)
            if document is None:
                raise HTTPException(404, "Not found")
            model = Technique if document.kind == 'technique' else Product
            row = _get(db, model, document.record_id)
            document = ingest_record(db, row, force=True)
            db.commit()
            return {"document_id": document.id, "version": document.active_version, "status": document.status}

    @app.get(f"{API}/sources")
    def list_sources():
        with session() as db:
            return [_source_out(x) for x in db.scalars(select(Source).where(Source.owner_id == "local").order_by(Source.id)).all()]

    @app.post(f"{API}/sources", status_code=201)
    def create_source(data: SourceInput):
        with session() as db:
            row = Source(**data.model_dump())
            db.add(row)
            _save(db)
            db.refresh(row)
            return _source_out(row)

    @app.get(f"{API}/sources/{{id}}")
    def get_source(id: int):
        with session() as db:
            return _source_out(_get(db, Source, id))

    @app.patch(f"{API}/sources/{{id}}")
    def patch_source(id: int, data: SourcePatch):
        with session() as db:
            row = _get(db, Source, id)
            for key, value in data.model_dump(exclude_unset=True).items():
                setattr(row, key, value)
            _save(db)
            return _source_out(row)

    @app.delete(f"{API}/sources/{{id}}")
    def withdraw_source(id: int):
        return patch_source(id, SourcePatch(status="withdrawn"))

    @app.get(f"{API}/techniques")
    def list_techniques():
        with session() as db:
            rows = db.scalars(select(Technique).where(Technique.owner_id == "local").options(selectinload(Technique.sources), selectinload(Technique.attachments)).order_by(Technique.id)).all()
            return [_technique_out(x) for x in rows]

    @app.post(f"{API}/techniques", status_code=201)
    def create_technique(data: TechniqueInput):
        with session() as db:
            values = data.model_dump(exclude={"source_ids", "attachment_ids", "original"})
            if "content_blocks" in data.model_fields_set:
                values["steps"] = derived_steps(values["content_blocks"])
            attachment_ids = list(dict.fromkeys(data.attachment_ids + image_ids(values["content_blocks"])))
            sources = [Source(title=("原创：" + data.title)[:300], source_type="original", author="我",
                              url=None, status="published", visibility=data.visibility)] if data.original else _sources(db, data.source_ids)
            row = Technique(**values, sources=sources, attachments=_attachments(db, attachment_ids))
            db.add(row)
            _save(db)
            return _technique_out(row)

    @app.get(f"{API}/techniques/{{id}}")
    def get_technique(id: int):
        with session() as db:
            return _technique_out(_get(db, Technique, id))

    @app.patch(f"{API}/techniques/{{id}}")
    def patch_technique(id: int, data: TechniquePatch):
        with session() as db:
            row = _get(db, Technique, id)
            values = data.model_dump(exclude_unset=True)
            if "steps" in values and "content_blocks" not in values and row.content_blocks:
                raise HTTPException(422, "Edit content_blocks instead of legacy steps")
            blocks = values.get("content_blocks", row.content_blocks or [])
            used = image_ids(blocks)
            if "content_blocks" in values:
                values["steps"] = derived_steps(blocks)
                selected = values.get("attachment_ids", [a.id for a in row.attachments])
                values["attachment_ids"] = list(dict.fromkeys(selected + used))
            elif "attachment_ids" in values and not set(used) <= set(values["attachment_ids"] or []):
                raise HTTPException(422, "Attachments used by content_blocks cannot be removed")
            for key, value in values.items():
                if key == "source_ids":
                    row.sources = _sources(db, value)
                elif key == "attachment_ids":
                    row.attachments = _attachments(db, value)
                else:
                    setattr(row, key, value)
            _save(db)
            return _technique_out(row)

    @app.delete(f"{API}/techniques/{{id}}")
    def withdraw_technique(id: int):
        return patch_technique(id, TechniquePatch(status="withdrawn"))

    @app.get(f"{API}/products")
    def list_products():
        with session() as db:
            rows = db.scalars(select(Product).where(Product.owner_id == "local").options(selectinload(Product.sources), selectinload(Product.aliases), selectinload(Product.attachments)).order_by(Product.id)).all()
            return [_product_out(x) for x in rows]

    @app.post(f"{API}/products", status_code=201)
    def create_product(data: ProductInput):
        with session() as db:
            values = data.model_dump(exclude={"source_ids", "aliases", "attachment_ids"})
            row = Product(**values, sku_key=_key(data.sku) or None, sources=_sources(db, data.source_ids), attachments=_attachments(db, data.attachment_ids))
            _aliases(db, row, data.aliases)
            db.add(row)
            _save(db)
            return _product_out(row)

    @app.get(f"{API}/products/{{id}}")
    def get_product(id: int):
        with session() as db:
            return _product_out(_get(db, Product, id))

    @app.patch(f"{API}/products/{{id}}")
    def patch_product(id: int, data: ProductPatch):
        with session() as db:
            row = _get(db, Product, id)
            values = data.model_dump(exclude_unset=True)
            for key, value in values.items():
                if key not in {"source_ids", "aliases", "attachment_ids"}:
                    setattr(row, key, value)
            if "sku" in values:
                row.sku_key = _key(row.sku) or None
            if "source_ids" in values:
                row.sources = _sources(db, values["source_ids"])
            if "attachment_ids" in values:
                row.attachments = _attachments(db, values["attachment_ids"])
            if "aliases" in values or "brand" in values:
                _aliases(db, row, values.get("aliases", [a.alias for a in row.aliases]))
            _save(db)
            return _product_out(row)

    @app.delete(f"{API}/products/{{id}}")
    def withdraw_product(id: int):
        return patch_product(id, ProductPatch(status="withdrawn"))

    @app.get(f"{API}/search")
    def search(q: str = Query(min_length=1, max_length=2000)):
        with session() as db:
            hits = search_documents(db, q, k=20)
            # Keep catalog record shapes while retaining the global mixed-type rank.
            output = {"techniques": [], "products": [], "ranking": [hit["key"] for hit in hits]}
            for hit in hits:
                if hit["type"] == "technique":
                    record = _technique_out(db.get(Technique, hit["id"]))
                    target = "techniques"
                else:
                    record = _product_out(db.get(Product, hit["id"]))
                    target = "products"
                record["source_ids"] = hit["source_ids"]
                output[target].append(record)
            return output

    @app.post(f"{API}/imports/markdown", status_code=201)
    async def import_markdown(file: UploadFile = File(...), source_title: str | None = Form(None), source_url: str | None = Form(None)):
        if not file.filename or not file.filename.lower().endswith(".md"):
            raise HTTPException(415, "A Markdown .md file is required")
        data = await file.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise HTTPException(413, "Markdown file exceeds 1 MiB")
        try:
            body = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(422, "Markdown must be UTF-8")
        if not body.strip():
            raise HTTPException(422, "Markdown is empty")
        heading = re.search(r"^#\s+(.+)$", body, flags=re.MULTILINE)
        title = heading.group(1).strip() if heading else Path(file.filename).stem
        with session() as db:
            src = Source(title=source_title or file.filename, url=source_url, source_type="markdown", content_hash=hashlib.sha256(data).hexdigest())
            item = Technique(title=title, summary=body, sources=[src])
            db.add(item)
            _save(db)
            return {"source": _source_out(src), "technique": _technique_out(item)}

    @app.post(f"{API}/attachments", status_code=201)
    async def upload_attachment(file: UploadFile = File(...)):
        content_type = file.content_type or ""
        if content_type not in IMAGE_TYPES:
            raise HTTPException(415, "Only PNG, JPEG, GIF, and WebP images are supported")
        raw = await file.read(MAX_IMAGE + 1)
        if len(raw) > MAX_IMAGE:
            raise HTTPException(413, "Image exceeds 5 MiB")
        magic, extension = IMAGE_TYPES[content_type]
        valid = raw.startswith(magic) and (content_type != "image/webp" or raw[8:12] == b"WEBP")
        if not valid:
            raise HTTPException(415, "Image data does not match content type")
        name = Path((file.filename or "image").replace("\\", "/")).name
        if name in ("", ".", ".."):
            name = "image" + extension
        storage_name = uuid.uuid4().hex + extension
        path = storage / storage_name
        path.write_bytes(raw)
        try:
            with session() as db:
                row = Attachment(filename=name, content_type=content_type, storage_name=storage_name, sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
                db.add(row)
                _save(db)
                return _attachment_out(row)
        except Exception:
            path.unlink(missing_ok=True)
            raise

    @app.get(f"{API}/attachments/{{id}}")
    def download_attachment(id: int):
        with session() as db:
            row = _get(db, Attachment, id)
            if row.status != "published":
                raise HTTPException(404, "Not found")
            path = storage / row.storage_name
            if not path.is_file():
                raise HTTPException(404, "Attachment missing")
            return Response(content=path.read_bytes(), media_type=row.content_type,
                            headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(row.filename, safe='')}",
                                     "X-Content-Type-Options": "nosniff"})

    @app.get(f"{API}/backup")
    async def export_backup():
        with session() as db:
            payload = {
                "version": 5,
                **export_experiments(db),
                **export_evaluation(db),
                **export_chat(db),
                "sources": [_source_out(x) for x in db.scalars(select(Source).order_by(Source.id)).all()],
                "techniques": [_technique_out(x) for x in db.scalars(select(Technique).options(selectinload(Technique.sources), selectinload(Technique.attachments)).order_by(Technique.id)).all()],
                "products": [_product_out(x) for x in db.scalars(select(Product).options(selectinload(Product.sources), selectinload(Product.aliases), selectinload(Product.attachments)).order_by(Product.id)).all()],
                "attachments": [{**_attachment_out(x), "storage_name": x.storage_name, "sha256": x.sha256,
                                 "owner_id": x.owner_id, "visibility": x.visibility, "status": x.status}
                                for x in db.scalars(select(Attachment).order_by(Attachment.id)).all()],
            }
            manifest = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            # ZipFile uses ZIP_STORED here, so payload bytes are a lower bound
            # on the final archive size. Reject oversized data before reading files.
            expanded = len(manifest) + sum(item["size"] for item in payload["attachments"])
            if expanded > app.state.backup_limit:
                raise HTTPException(413, "Backup exceeds restore size limit")
            archive_path = storage / f"backup-{uuid.uuid4().hex}.zip"
            try:
                with ZipFile(archive_path, "w") as archive:
                    archive.writestr("manifest.json", manifest)
                    for item in payload["attachments"]:
                        path = storage / item["storage_name"]
                        if not path.is_file():
                            raise HTTPException(500, "Attachment file missing")
                        archive.write(path, f"attachments/{item['storage_name']}")
                if archive_path.stat().st_size > app.state.backup_limit:
                    raise HTTPException(413, "Backup exceeds restore size limit")
            except Exception:
                archive_path.unlink(missing_ok=True)
                raise
            return FileResponse(archive_path, media_type="application/zip", filename="knowledge-backup.zip",
                                background=BackgroundTask(archive_path.unlink, missing_ok=True))

    @app.post(f"{API}/backup")
    async def restore_backup(file: UploadFile = File(...)):
        async with chat.lock:
            if chat.has_active() or experiments.has_active():
                raise HTTPException(409, "请先停止正在生成的对话和实验，再恢复备份。")
            temporary = await _spool_backup(file, storage, app.state.backup_limit)
            try:
                return restore_backup_archive(Path(temporary.name))
            finally:
                temporary.cleanup()

    def restore_backup_archive(upload_dir: Path):
        archive_path = upload_dir / "backup.zip"
        validated = upload_dir / "validated"
        validated.mkdir()
        try:
            with ZipFile(archive_path) as archive:
                infos = archive.infolist()
                if len(infos) > 10000 or sum(x.file_size for x in infos) > app.state.backup_limit * 2:
                    raise ValueError("Backup expands beyond size limit")
                if len({x.filename for x in infos}) != len(infos):
                    raise ValueError("Duplicate backup entry")
                for info in infos:
                    path = PurePosixPath(info.filename)
                    if path.is_absolute() or ".." in path.parts or info.filename.startswith("\\") or (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("Unsafe backup path")
                if [x.filename for x in infos].count("manifest.json") != 1:
                    raise ValueError("Missing or duplicate manifest")
                if archive.getinfo("manifest.json").file_size > min(app.state.backup_limit, 64 * 1024 * 1024):
                    raise ValueError("Manifest exceeds size limit")
                data = json.loads(archive.read("manifest.json"))
                if data.get("version") not in (1, 2, 3, 4, 5):
                    raise ValueError("Unsupported backup version")
                validate_chat_backup(data)
                validate_evaluation_backup(data)
                validate_experiment_backup(data)
                attachments = {}
                for item in data["attachments"]:
                    name = item["storage_name"]
                    if not re.fullmatch(r"[0-9a-f]{32}\.(png|jpg|gif|webp)", name):
                        raise ValueError("Unsafe attachment name")
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(f"attachments/{name}") as source, (validated / name).open("wb") as output:
                        while chunk := source.read(1024 * 1024):
                            size += len(chunk)
                            digest.update(chunk)
                            output.write(chunk)
                    if digest.hexdigest() != item["sha256"] or size != item["size"]:
                        raise ValueError("Attachment checksum mismatch")
                    attachments[name] = size
                allowed = {"manifest.json", *(f"attachments/{name}" for name in attachments)}
                if {x.filename for x in infos} != allowed:
                    raise ValueError("Unexpected backup entry")
                source_ids = {x["id"] for x in data["sources"]}
                if any(not set(x["source_ids"]) <= source_ids for kind in ("techniques", "products") for x in data[kind]):
                    raise ValueError("Broken source reference")
                attachment_ids = {x["id"] for x in data["attachments"]}
                if any(not set(x.get("attachment_ids", [])) <= attachment_ids for kind in ("techniques", "products") for x in data[kind]):
                    raise ValueError("Broken attachment reference")
                attachment_records = {a["id"]: a for a in data["attachments"]}
                for item in data["techniques"]:
                    blocks = content_adapter.validate_python(item.get("content_blocks", []))
                    item["content_blocks"] = content_adapter.dump_python(blocks)
                    used = image_ids(item["content_blocks"])
                    if not set(used) <= set(item.get("attachment_ids", [])):
                        raise ValueError("Broken content block attachment reference")
                    if any(attachment_records[aid].get("owner_id") != "local" or attachment_records[aid].get("status") != "published" for aid in used):
                        raise ValueError("Inactive content block attachment")
                    if item["content_blocks"]:
                        item["steps"] = derived_steps(item["content_blocks"])

        except OSError as exc:
            raise HTTPException(500, "Restore could not stage attachments") from exc
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, RuntimeError) as exc:
            raise HTTPException(400, f"Invalid backup: {exc}")
        except Exception as exc:
            from zipfile import BadZipFile
            if isinstance(exc, BadZipFile):
                raise HTTPException(400, "Invalid ZIP backup")
            raise
        # Stage all files before changing the database. Keep copies of existing
        # files so a failed replace or commit can restore the previous state.
        try:
            with TemporaryDirectory(dir=storage, prefix="restore-") as temp_name:
                temp = Path(temp_name)
                incoming = temp / "incoming"
                previous = temp / "previous"
                incoming.mkdir()
                previous.mkdir()
                for name in attachments:
                    os.replace(validated / name, incoming / name)
                    target = storage / name
                    if target.exists():
                        os.link(target, previous / name)
                with session() as db:
                    placed = []
                    try:
                        clear_index(db)
                        db.execute(delete(technique_attachments))
                        db.execute(delete(product_attachments))
                        db.execute(delete(technique_sources))
                        db.execute(delete(product_sources))
                        db.query(ProductAlias).delete()
                        db.query(Technique).delete()
                        db.query(Product).delete()
                        db.query(Source).delete()
                        db.query(Attachment).delete()
                        for item in data["sources"]:
                            fields = {k: item.get(k) for k in ("id", "title", "url", "source_type", "author", "license_note", "content_hash", "owner_id", "visibility", "status")}
                            fields["retrieved_at"] = datetime.fromisoformat(item["retrieved_at"]) if item.get("retrieved_at") else None
                            db.add(Source(**fields))
                        for item in data["attachments"]:
                            fields = {k: item.get(k) for k in ("id", "filename", "content_type", "storage_name", "sha256", "size", "owner_id", "visibility", "status")}
                            db.add(Attachment(**fields))
                        db.flush()
                        for item in data["techniques"]:
                            fields = {k: item.get(k) for k in ("id", "title", "category", "summary", "steps", "content_blocks", "prerequisites", "risks", "difficulty", "tags", "owner_id", "visibility", "status")}
                            db.add(Technique(**fields, sources=[db.get(Source, sid) for sid in item["source_ids"]],
                                             attachments=[db.get(Attachment, aid) for aid in item.get("attachment_ids", [])]))
                        for item in data["products"]:
                            fields = {k: item.get(k) for k in ("id", "brand", "name", "sku", "kind", "properties", "manufacturer_url", "owner_id", "visibility", "status")}
                            product = Product(**fields, sku_key=_key(item.get("sku")) or None, sources=[db.get(Source, sid) for sid in item["source_ids"]],
                                              attachments=[db.get(Attachment, aid) for aid in item.get("attachment_ids", [])])
                            _aliases(db, product, item["aliases"])
                            db.add(product)
                        restore_chat(db, data)
                        restore_evaluation(db, data)
                        restore_experiments(db, data)
                        db.flush()
                        # Restore is all-or-nothing, including every derived index.
                        backfill(db, strict=True)
                        if engine.dialect.name == "postgresql":
                            for table in ("sources", "techniques", "products", "product_aliases", "attachments", "conversations", "messages", "runs", "evaluation_runs", "experiment_jobs"):
                                db.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 1), EXISTS(SELECT 1 FROM {table}))"))
                        for name in attachments:
                            os.replace(incoming / name, storage / name)
                            placed.append(name)
                        db.commit()
                    except Exception as exc:
                        db.rollback()
                        for name in reversed(placed):
                            target = storage / name
                            saved = previous / name
                            if saved.exists():
                                os.replace(saved, target)
                            else:
                                target.unlink(missing_ok=True)
                        if isinstance(exc, OSError):
                            raise HTTPException(500, "Restore could not write attachments") from exc
                        raise HTTPException(400, "Backup data is invalid") from exc
        except OSError as exc:
            raise HTTPException(500, "Restore could not stage attachments") from exc
        return {"restored": True, "sources": len(data["sources"]), "techniques": len(data["techniques"]),
                "products": len(data["products"]), "attachments": len(attachments)}

    return app


app = create_app(initialize_sqlite=False)
