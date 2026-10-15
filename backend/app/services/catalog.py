"""Catalog rules and transactions, usable without an HTTP request."""

import hashlib
import re
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from ..content import image_ids
from ..models import Product, ProductAlias, Source, Technique
from ..schemas import (
    ProductInput,
    ProductPatch,
    SourceInput,
    SourcePatch,
    TechniqueInput,
    TechniquePatch,
)
from .errors import ServiceError
from .ingestion import IngestionService
from .serializers import _source_out, _technique_out, _product_out


def _key(value: str | None) -> str:
    return (value or '').strip().casefold()


def _aliases(db, product, aliases):
    brand_key = _key(product.brand)
    keys = set()
    output = []
    existing = {_key(item.alias): item for item in product.aliases}
    for alias in aliases:
        clean = alias.strip()
        key = brand_key + ':' + _key(clean)
        if not clean or key in keys:
            raise ServiceError(422, 'Aliases must be nonempty and distinct')
        keys.add(key)
        item = existing.get(_key(clean)) or ProductAlias()
        item.alias = clean
        item.alias_key = key
        output.append(item)
    product.aliases = output


class CatalogService:
    def __init__(self, repository):
        self.repository = repository

    def _get(self, model, record_id):
        row = self.repository.get(model, record_id)
        if row is None or row.owner_id != 'local':
            raise ServiceError(404, 'Not found')
        return row

    def _sources(self, ids):
        rows = self.repository.sources(ids)
        if len(set(ids)) != len(ids) or len(rows) != len(ids):
            raise ServiceError(422, 'source_ids must refer to existing, active sources')
        return rows

    def _attachments(self, ids):
        rows = self.repository.attachments(ids)
        if len(set(ids)) != len(ids) or len(rows) != len(ids):
            raise ServiceError(422, 'attachment_ids must refer to existing, active attachments')
        return rows

    def _save(self, record=None):
        try:
            if record is not None:
                IngestionService(self.repository.session).reindex(record)
            self.repository.commit()
        except IntegrityError as exc:
            self.repository.rollback()
            raise ServiceError(409, 'Duplicate SKU or alias') from exc
        except Exception:
            self.repository.rollback()
            raise

    def list_sources(self):
        return [_source_out(x) for x in self.repository.list(Source)]

    def create_source(self, data: SourceInput):
        row = Source(**data.model_dump())
        self.repository.add(row)
        self._save()
        return _source_out(row)

    def get_source(self, id: int):
        return _source_out(self._get(Source, id))

    def patch_source(self, id: int, data: SourcePatch):
        row = self._get(Source, id)
        for key, value in data.model_dump(exclude_unset=True).items():
            setattr(row, key, value)
        self._save()
        return _source_out(row)

    def withdraw_source(self, id: int):
        return self.patch_source(id, SourcePatch(status='withdrawn'))

    def list_techniques(self):
        rows = self.repository.list(Technique)
        return [_technique_out(x) for x in rows]

    def create_technique(self, data: TechniqueInput):
        values = data.model_dump(exclude={'source_ids', 'attachment_ids', 'original'})
        attachment_ids = list(
            dict.fromkeys(data.attachment_ids + image_ids(values['content_blocks']))
        )
        sources = (
            [
                Source(
                    title=('原创：' + data.title)[:300],
                    source_type='original',
                    author='我',
                    url=None,
                    status='published',
                    visibility=data.visibility,
                )
            ]
            if data.original
            else self._sources(data.source_ids)
        )
        row = Technique(**values, sources=sources, attachments=self._attachments(attachment_ids))
        self.repository.add(row)
        self._save(row)
        return _technique_out(row)

    def get_technique(self, id: int):
        return _technique_out(self._get(Technique, id))

    def patch_technique(self, id: int, data: TechniquePatch):
        row = self._get(Technique, id)
        values = data.model_dump(exclude_unset=True)
        blocks = values.get('content_blocks', row.content_blocks or [])
        used = image_ids(blocks)
        if 'content_blocks' in values:
            selected = values.get('attachment_ids', [a.id for a in row.attachments])
            values['attachment_ids'] = list(dict.fromkeys(selected + used))
        elif 'attachment_ids' in values and not set(used) <= set(values['attachment_ids'] or []):
            raise ServiceError(422, 'Attachments used by content_blocks cannot be removed')
        for key, value in values.items():
            if key == 'source_ids':
                row.sources = self._sources(value)
            elif key == 'attachment_ids':
                row.attachments = self._attachments(value)
            else:
                setattr(row, key, value)
        self._save(row)
        return _technique_out(row)

    def withdraw_technique(self, id: int):
        return self.patch_technique(id, TechniquePatch(status='withdrawn'))

    def list_products(self):
        rows = self.repository.list(Product)
        return [_product_out(x) for x in rows]

    def create_product(self, data: ProductInput):
        values = data.model_dump(exclude={'source_ids', 'aliases', 'attachment_ids'})
        row = Product(
            **values,
            sku_key=_key(data.sku) or None,
            sources=self._sources(data.source_ids),
            attachments=self._attachments(data.attachment_ids),
        )
        _aliases(None, row, data.aliases)
        self.repository.add(row)
        self._save(row)
        return _product_out(row)

    def get_product(self, id: int):
        return _product_out(self._get(Product, id))

    def patch_product(self, id: int, data: ProductPatch):
        row = self._get(Product, id)
        values = data.model_dump(exclude_unset=True)
        for key, value in values.items():
            if key not in {'source_ids', 'aliases', 'attachment_ids'}:
                setattr(row, key, value)
        if 'sku' in values:
            row.sku_key = _key(row.sku) or None
        if 'source_ids' in values:
            row.sources = self._sources(values['source_ids'])
        if 'attachment_ids' in values:
            row.attachments = self._attachments(values['attachment_ids'])
        if 'aliases' in values or 'brand' in values:
            _aliases(None, row, values.get('aliases', [a.alias for a in row.aliases]))
        self._save(row)
        return _product_out(row)

    def withdraw_product(self, id: int):
        return self.patch_product(id, ProductPatch(status='withdrawn'))

    def search(self, query):
        results = self.repository.search(query)
        output = {'techniques': [], 'products': [], 'ranking': [hit['key'] for hit, _ in results]}
        for hit, row in results:
            record = _technique_out(row) if hit['type'] == 'technique' else _product_out(row)
            record['source_ids'] = hit['source_ids']
            output['techniques' if hit['type'] == 'technique' else 'products'].append(record)
        return output

    def import_markdown(self, filename, data, source_title=None, source_url=None):
        if not filename or not filename.lower().endswith('.md'):
            raise ServiceError(415, 'A Markdown .md file is required')
        if len(data) > 1024 * 1024:
            raise ServiceError(413, 'Markdown file exceeds 1 MiB')
        try:
            body = data.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ServiceError(422, 'Markdown must be UTF-8') from exc
        if not body.strip():
            raise ServiceError(422, 'Markdown is empty')
        heading = re.search(r'^#\s+(.+)$', body, flags=re.MULTILINE)
        title = heading.group(1).strip() if heading else Path(filename).stem
        source = Source(
            title=source_title or filename,
            url=source_url,
            source_type='markdown',
            content_hash=hashlib.sha256(data).hexdigest(),
        )
        item = Technique(title=title, summary=body, sources=[source])
        self.repository.add(item)
        self._save(item)
        return {'source': _source_out(source), 'technique': _technique_out(item)}
