"""Validate and persist image uploads with filesystem/database compensation."""
import hashlib
import uuid
from pathlib import Path

from ..models import Attachment
from .errors import ServiceError
from .serializers import _attachment_out

MAX_IMAGE = 5 * 1024 * 1024
IMAGE_TYPES = {'image/png': (b'\x89PNG\r\n\x1a\n', '.png'), 'image/jpeg': (b'\xff\xd8\xff', '.jpg'),
               'image/gif': (b'GIF8', '.gif'), 'image/webp': (b'RIFF', '.webp')}


class AttachmentService:
    def __init__(self, repository, storage):
        self.repository = repository
        self.storage = storage

    def upload(self, filename, content_type, raw):
        if content_type not in IMAGE_TYPES:
            raise ServiceError(415, 'Only PNG, JPEG, GIF, and WebP images are supported')
        if len(raw) > MAX_IMAGE:
            raise ServiceError(413, 'Image exceeds 5 MiB')
        magic, extension = IMAGE_TYPES[content_type]
        if not raw.startswith(magic) or (content_type == 'image/webp' and raw[8:12] != b'WEBP'):
            raise ServiceError(415, 'Image data does not match content type')
        name = Path((filename or 'image').replace('\\', '/')).name
        if name in ('', '.', '..'):
            name = 'image' + extension
        storage_name = uuid.uuid4().hex + extension
        path = self.storage / storage_name
        path.write_bytes(raw)
        try:
            row = Attachment(filename=name, content_type=content_type, storage_name=storage_name,
                             sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
            self.repository.add(row)
            self.repository.commit()
            return _attachment_out(row)
        except Exception:
            self.repository.rollback()
            path.unlink(missing_ok=True)
            raise

    def download(self, attachment_id):
        row = self.repository.get(Attachment, attachment_id)
        if row is None or row.owner_id != 'local' or row.status != 'published':
            raise ServiceError(404, 'Not found')
        path = self.storage / row.storage_name
        if not path.is_file():
            raise ServiceError(404, 'Attachment missing')
        return path.read_bytes(), row.content_type, row.filename
