"""Explicit NMM input ingestion; no model, graph, or background work runs here."""

import hashlib
from io import BytesIO
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session
from PIL import Image

from app.services.errors import ServiceError
from .models import NmmAsset
from .providers import ImageInput
from .repository import NmmRepository
from .schemas import NmmParams
from .storage import NmmStorage, normalize_image, validate_mask


REFERENCE_ROLES = frozenset(('reference_style', 'reference_palette', 'reference_steps'))


class NmmService:
    def __init__(self, engine, attachment_root: Path):
        self.engine = engine
        self.storage = NmmStorage(attachment_root)

    def create_project(
        self,
        *,
        title: str,
        original: ImageInput,
        mask: ImageInput,
        params: dict | NmmParams | None = None,
        references: list[tuple[str, ImageInput]] | None = None,
    ) -> UUID:
        """Validate all inputs before writes and compensate files on database failure."""
        parsed = params if isinstance(params, NmmParams) else NmmParams.model_validate(params or {})
        references = list(references or [])
        if len(references) > 12 or any(role not in REFERENCE_ROLES for role, _ in references):
            raise ValueError('参考图仅支持效果、配色或步骤用途，最多 12 张。')
        if sum(role != 'reference_steps' for role, _ in references) > 3:
            raise ValueError('效果与配色参考合计最多 3 张。')
        source = normalize_image(original.data, original.content_type, max_pixels=4_000_000)
        if source.width * source.height > 4_000_000:
            raise ValueError('NMM original exceeds the 4,000,000 pixel limit.')
        with Image.open(BytesIO(source.data)) as decoded:
            if decoded.mode == 'RGBA' and decoded.getchannel('A').getextrema() != (255, 255):
                raise ValueError('NMM original must be opaque.')
        region = validate_mask(
            mask.data, mask.content_type, width=source.width, height=source.height
        )
        inputs = [('original', source), ('mask', region)] + [
            (role, normalize_image(value.data, value.content_type)) for role, value in references
        ]
        written = []
        try:
            with Session(self.engine) as db, db.begin():
                repository = NmmRepository(db)
                project = repository.create_project(title, parsed)
                project_id = project.id
                assets = []
                for index, (role, image) in enumerate(inputs):
                    stored = self.storage.put(project_id, image)
                    written.append((project_id, stored.name))
                    asset = repository.add_asset(
                        project_id,
                        role=role,
                        storage_name=stored.name,
                        content_type=stored.content_type,
                        size=stored.size,
                        width=stored.width,
                        height=stored.height,
                        sha256=stored.sha256,
                        sort_order=index,
                    )
                    assets.append({'id': str(asset.id), 'role': role, 'sha256': stored.sha256})
                project.input_snapshots = {'1': {'params': parsed.model_dump(), 'assets': assets}}
            return project_id
        except BaseException as failure:
            for project_id, name in reversed(written):
                try:
                    self.storage.delete(project_id, name)
                except OSError:
                    failure.add_note('NMM 临时图片清理失败；需要检查项目存储目录。')
            raise

    def read_asset(self, asset_id: UUID) -> tuple[bytes, str]:
        with Session(self.engine) as db:
            asset = db.get(NmmAsset, asset_id)
            if asset is None:
                raise ServiceError(404, 'NMM 图片不存在。')
            try:
                raw = self.storage.read(asset.project_id, asset.storage_name)
            except FileNotFoundError:
                raise ServiceError(404, 'NMM 图片文件不存在。') from None
            if hashlib.sha256(raw).hexdigest() != asset.sha256:
                raise ServiceError(409, 'NMM 图片文件与记录不一致。')
            return raw, asset.content_type
