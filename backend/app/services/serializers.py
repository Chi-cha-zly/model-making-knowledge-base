"""Public catalog representations, independent of HTTP routing."""

API = '/api/v1'


def _source_out(row):
    return {
        'id': row.id,
        'title': row.title,
        'url': row.url,
        'source_type': row.source_type,
        'author': row.author,
        'license_note': row.license_note,
        'retrieved_at': row.retrieved_at.isoformat() if row.retrieved_at else None,
        'content_hash': row.content_hash,
        'owner_id': row.owner_id,
        'visibility': row.visibility,
        'status': row.status,
    }


def _technique_out(row):
    return {
        'id': row.id,
        'title': row.title,
        'category': row.category,
        'summary': row.summary,
        'content_blocks': row.content_blocks or [],
        'prerequisites': row.prerequisites,
        'risks': row.risks,
        'difficulty': row.difficulty,
        'tags': row.tags or [],
        'source_ids': [s.id for s in row.sources],
        'attachment_ids': [a.id for a in row.attachments],
        'owner_id': row.owner_id,
        'visibility': row.visibility,
        'status': row.status,
    }


def _product_out(row):
    return {
        'id': row.id,
        'brand': row.brand,
        'name': row.name,
        'sku': row.sku,
        'kind': row.kind,
        'aliases': [a.alias for a in row.aliases],
        'source_ids': [s.id for s in row.sources],
        'attachment_ids': [a.id for a in row.attachments],
        'properties': row.properties or {},
        'manufacturer_url': row.manufacturer_url,
        'owner_id': row.owner_id,
        'visibility': row.visibility,
        'status': row.status,
    }


def _attachment_out(row):
    return {
        'id': row.id,
        'filename': row.filename,
        'content_type': row.content_type,
        'size': row.size,
        'url': f'{API}/attachments/{row.id}',
    }
