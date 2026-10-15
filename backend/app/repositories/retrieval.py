"""Bounded SQL recall and current authorization. No network or commits."""

import re

from sqlalchemy import and_, or_, select, func, case, text, tuple_

from ..embedding_contract import EMBEDDING_DIMENSIONS, embedding_text
from ..ingestion import digest, index_terms, IMAGE_ORIGINS
from ..ingestion_models import Chunk, Document, DocumentSource, Embedding, Posting
from ..models import Attachment, Product, Source, Technique
from ..lexical import _normalize, _tokens, STOP_PHRASES
from ..retrieval import SYNONYMS, _contains, rank_candidates
from ..retrieval_types import EvidenceRef


def visible_chunk():
    source = (
        select(DocumentSource.source_id)
        .join(Source)
        .where(
            DocumentSource.document_id == Document.id,
            DocumentSource.version == Chunk.version,
            Source.owner_id == 'local',
            Source.status == 'published',
        )
        .exists()
    )
    technique = (
        select(Technique.id)
        .where(
            Technique.id == Document.record_id,
            Technique.owner_id == 'local',
            Technique.status == 'published',
        )
        .exists()
    )
    product = (
        select(Product.id)
        .where(
            Product.id == Document.record_id,
            Product.owner_id == 'local',
            Product.status == 'published',
        )
        .exists()
    )
    image = or_(
        Chunk.attachment_id.is_(None),
        select(Attachment.id)
        .where(
            Attachment.id == Chunk.attachment_id,
            Attachment.owner_id == 'local',
            Attachment.status == 'published',
            Attachment.sha256 == Chunk.payload['attachment_hash'].as_string(),
        )
        .exists(),
    )
    return and_(
        Chunk.version == Document.active_version,
        source,
        image,
        or_(
            and_(Document.kind == 'technique', technique), and_(Document.kind == 'product', product)
        ),
    )


def query_terms(query):
    normalized = _normalize(query[-64000:]).strip()
    cleaned = normalized
    for phrase in STOP_PHRASES:
        if re.fullmatch(r'[\u4e00-\u9fff]+', phrase):
            cleaned = cleaned.replace(phrase, ' ')
    words = set(_tokens(cleaned, single=len(cleaned.strip()) == 1))
    if not words:
        return set(), []
    for group in SYNONYMS:
        if any(_contains(normalized, term) for term in group):
            for term in group:
                words.update(_tokens(term))
    terms = {'t:' + word for word in words if len(word) <= 190}
    for word in words:
        if re.fullmatch(r'[a-z]{4,40}', word):
            terms.update(term for term in index_terms(word) if term.startswith('f:'))
    for token in re.findall(r'[a-z0-9]+(?:[\s_-]+[a-z0-9]+)*', normalized):
        pieces = re.split(r'[\s_-]+', token)
        for start in range(len(pieces)):
            for end in range(start + 1, min(len(pieces), start + 4) + 1):
                compact = ''.join(pieces[start:end])
                if re.search(r'\d', compact):
                    terms.add('s:' + compact)
    return words, sorted(terms)[:1500]


def lexical_candidates(db, query, limit=200):
    _, terms = query_terms(query)
    if not terms:
        return []
    weight = func.sum(case((Posting.term.like('s:%'), 100), (Posting.term.like('t:%'), 3), else_=1))
    statement = (
        select(Chunk.id)
        .join(Posting)
        .join(Document)
        .where(Posting.term.in_(terms), visible_chunk())
        .group_by(Chunk.id)
        .order_by(weight.desc(), Chunk.id.desc())
        .limit(limit)
    )
    return list(db.scalars(statement))


def ready_predicate(model):
    return and_(
        Embedding.status == 'ready',
        Embedding.model == model,
        Embedding.dimensions == EMBEDDING_DIMENSIONS,
        Embedding.vector.is_not(None),
    )


def has_vectors(db, model):
    return (
        db.scalar(
            select(Embedding.id)
            .join(Chunk)
            .join(Document)
            .where(visible_chunk(), ready_predicate(model))
            .limit(1)
        )
        is not None
    )


def vector_candidates(db, vector, model, limit=200):
    db.execute(text('SET LOCAL hnsw.ef_search = 200'))
    db.execute(text("SET LOCAL hnsw.iterative_scan = 'strict_order'"))
    distance = Embedding.vector.cosine_distance(vector)
    rows = db.execute(
        select(
            Chunk.id,
            Chunk.content,
            Chunk.payload,
            Embedding.content_hash,
            distance.label('distance'),
        )
        .select_from(Embedding)
        .join(Chunk)
        .join(Document)
        .where(visible_chunk(), ready_predicate(model))
        .order_by(distance)
        .limit(limit)
    ).all()
    # Immutable chunks are normally content-hash matched; reject corrupted rows.
    return [
        row.id
        for row in rows
        if digest(embedding_text(row.content, row.payload)) == row.content_hash
    ]


def sources_for(db, identities):
    ids = [identity[0] for identity in identities]
    sources = {}
    if not ids:
        return sources
    for document_id, version, source in db.execute(
        select(DocumentSource.document_id, DocumentSource.version, Source)
        .join(Source)
        .where(
            tuple_(DocumentSource.document_id, DocumentSource.version).in_(identities),
            Source.owner_id == 'local',
            Source.status == 'published',
        )
        .order_by(Source.id)
    ):
        if (document_id, version) in identities:
            sources.setdefault((document_id, version), []).append(
                {'id': source.id, 'title': source.title, 'url': source.url}
            )
    return sources


def hydrate(db, chunk_ids, query, *, semantic_ids=()):
    if not chunk_ids:
        return {}, {}
    rows = list(
        db.scalars(select(Chunk).join(Document).where(Chunk.id.in_(chunk_ids), visible_chunk()))
    )
    by_id = {row.id: row for row in rows}
    rows = [by_id[cid] for cid in dict.fromkeys(chunk_ids) if cid in by_id]
    doc_ids = {row.document_id for row in rows}
    # Full short context remains bounded and undergoes the same live authorization.
    short_ids = list(
        db.scalars(
            select(Chunk.document_id)
            .join(Document)
            .where(Chunk.document_id.in_(doc_ids), Chunk.version == Document.active_version)
            .group_by(Chunk.document_id)
            .having(func.sum(func.length(Chunk.content)) <= 2800)
        )
    )
    short = {}
    if short_ids:
        for row in db.scalars(
            select(Chunk)
            .join(Document)
            .where(Chunk.document_id.in_(short_ids), visible_chunk())
            .order_by(Chunk.ordinal)
        ):
            short.setdefault(row.document_id, []).append(row)
    identities = {(row.document_id, row.version) for row in rows}
    sources = sources_for(db, identities)
    groups = {}
    for row in rows:
        groups.setdefault(row.document_id, []).append(row)
    words, _ = query_terms(query)
    documents, refs = {}, {}
    for document_id, matches in groups.items():
        first = matches[0]
        selected = short.get(document_id, matches[:4])
        content = '\n'.join(row.content for row in selected)[:6400]
        excerpt = content[:3000]
        if document_id not in short:
            windows = []
            for row in matches:
                normalized = _normalize(row.content)
                matched = set(_tokens(row.content)) & words
                starts = {
                    max(0, normalized.find(term) - 200) // 400 * 400
                    for term in matched
                    if term in normalized
                }
                for start in sorted(starts or {0}):
                    snippet = row.content[start : start + 950]
                    origin = next(
                        (label for label in IMAGE_ORIGINS if row.content.startswith(label)), ''
                    )
                    if origin and not snippet.startswith(origin):
                        snippet = origin + '\n' + snippet
                    windows.append((snippet, set(_tokens(snippet)) & words, row.id))
            uncovered, excerpts = set(words), []
            semantic = next(
                (
                    by_id[cid]
                    for cid in semantic_ids
                    if cid in by_id and by_id[cid].document_id == document_id
                ),
                None,
            )
            if semantic is not None:
                excerpts.append(semantic.content[:950])
                uncovered.difference_update(_tokens(excerpts[0]))
            while windows and len(excerpts) < 3:
                index = max(range(len(windows)), key=lambda i: (len(windows[i][1] & uncovered), -i))
                snippet, covered, _ = windows.pop(index)
                if excerpts and not (covered & uncovered):
                    break
                excerpts.append(snippet)
                uncovered.difference_update(covered)
            if excerpts:
                excerpt = '\n…\n'.join(excerpts)
        provenance = sources.get((document_id, first.version), [])
        if not provenance:
            continue
        key = first.payload['key']
        documents[key] = {
            **first.payload,
            'text': content,
            'excerpt': excerpt,
            'source_ids': [source['id'] for source in provenance],
            'sources': provenance,
            'attachment_ids': sorted(
                {
                    row.attachment_id
                    for row in matches
                    if row.attachment_id is not None
                    and (row.id in semantic_ids or set(_tokens(row.content)) & words)
                }
            ),
        }
        refs[key] = EvidenceRef(
            document_id,
            first.version,
            tuple(
                (row.id, digest(row.content))
                for row in {row.id: row for row in selected + matches}.values()
            ),
        )
    return documents, refs


def lexical_search(db, query, k=20):
    candidates = lexical_candidates(db, query)
    documents, refs = hydrate(db, candidates, query)
    hits = rank_candidates(list(documents.values()), query, k=k)
    for hit in hits:
        hit['attachment_ids'] = documents[hit['key']]['attachment_ids']
    return hits, refs, candidates


def revalidate(db, hits, refs):
    chunk_ids = [cid for ref in refs.values() for cid, _ in ref.chunks]
    chunks = (
        {
            row.id: row
            for row in db.scalars(
                select(Chunk).join(Document).where(Chunk.id.in_(chunk_ids), visible_chunk())
            )
        }
        if chunk_ids
        else {}
    )
    sources = sources_for(db, {(ref.document_id, ref.version) for ref in refs.values()})
    output, valid_refs = [], {}
    for hit in hits:
        ref = refs.get(hit['key'])
        if ref is None or any(
            cid not in chunks
            or chunks[cid].version != ref.version
            or chunks[cid].document_id != ref.document_id
            or digest(chunks[cid].content) != fingerprint
            for cid, fingerprint in ref.chunks
        ):
            continue
        provenance = sources.get((ref.document_id, ref.version), [])
        if not provenance:
            continue
        output.append(
            {
                **hit,
                'rank': len(output) + 1,
                'sources': provenance,
                'source_ids': [source['id'] for source in provenance],
            }
        )
        valid_refs[hit['key']] = ref
    return output, valid_refs


def document_text(db, key, limit=6000):
    """Full active-version text of one currently visible record, or '' if not visible."""
    parts = db.scalars(
        select(Chunk.content)
        .join(Document)
        .where(Document.record_key == key, visible_chunk())
        .order_by(Chunk.ordinal)
    )
    return '\n'.join(part for part in parts if part.strip())[:limit]
