"""Deterministic local relevance retrieval; scores never assert compatibility.

Internal documents retain full V2 evidence in ``text`` for fair baseline scoring.
``fields``, ``sku`` and ``aliases`` add ranked-only signals; public hits omit them.
"""
from collections import Counter
import math
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Product, Technique

RETRIEVAL_VERSION = 'v3-local-bm25-1'
BASELINE_VERSION = 'v2-lexical'
PUBLIC_FIELDS = ('key', 'type', 'id', 'title', 'excerpt', 'source_ids', 'sources')
# Narrow vocabulary equivalence only; none of these imply material compatibility.
SYNONYMS = (
    ('水口', '浇口'), ('水贴', '水转印', 'decal', 'decals'),
    ('喷笔', 'airbrush'), ('渗线', '勾线', 'panel lining'),
    ('打磨', '砂磨', 'sanding'), ('消光', '哑光', 'matte'),
    ('遮盖', '遮蔽', 'masking'),
)
STOP_WORDS = set('你好 请 请问 帮我 推荐 一下 模型 制作 方法 怎么 如何 什么 这个 那个 可以 应该 想要 我想 知道 更多 资料 参考 注意 安全 处理 入门 使用 的 是 有 吗 呢 吧 了 和 与 the a an and or how what to for of is please'.split())
STOP_PHRASES = sorted(STOP_WORDS | {'请帮我', '模型制作', '告诉我'}, key=lambda x: (-len(x), x))
TOKEN_RE = re.compile(r'[a-z0-9]+(?:[-_][a-z0-9]+)*|[\u4e00-\u9fff]+')


def readable_excerpt(value):
    """Render structured evidence without dropping meaningful false/zero values."""
    if value is None:
        return ''
    if isinstance(value, dict):
        lines = []
        for label, item in value.items():
            rendered = readable_excerpt(item)
            if rendered:
                separator = '\n' if isinstance(item, (dict, list)) else ''
                lines.append(f'{label}：{separator}{rendered}')
        return '\n'.join(lines)
    if isinstance(value, list):
        return '\n'.join('- ' + text for item in value if (text := readable_excerpt(item)))
    if isinstance(value, bool):
        return '是' if value else '否'
    return str(value).strip()


def load_documents(db: Session) -> list[dict]:
    documents = []
    for model, kind in ((Technique, 'technique'), (Product, 'product')):
        statement = select(model).where(model.owner_id == 'local', model.status == 'published').options(selectinload(model.sources))
        if model is Product:
            statement = statement.options(selectinload(Product.aliases))
        for row in db.scalars(statement.order_by(model.id)):
            sources = sorted((s for s in row.sources if s.owner_id == 'local' and s.status == 'published'), key=lambda s: s.id)
            if not sources:
                continue
            if kind == 'technique':
                title = row.title
                # Keep labels, order and fields identical to V2's original body.
                body = readable_excerpt({'摘要': row.summary, '正文': [b.get('text', b.get('caption', '')) for b in row.content_blocks or []], '前提条件': row.prerequisites,
                                         '风险': row.risks, '难度': row.difficulty, '标签': row.tags})
                sku, aliases = '', []
                fields = {'category': row.category or '', 'tags': readable_excerpt(row.tags)}
            else:
                title = ' '.join(filter(None, (row.brand, row.name, row.sku)))
                aliases = [a.alias for a in row.aliases]
                body = readable_excerpt({'属性': row.properties, '类别': row.kind, '别名': aliases})
                sku = row.sku or ''
                fields = {'brand': row.brand or '', 'name': row.name, 'category': row.kind or ''}
            documents.append({'key': f'{kind}:{row.id}', 'type': kind, 'id': row.id, 'title': title,
                              'excerpt': body[:3000], 'text': body, 'sku': sku, 'aliases': aliases, 'fields': fields,
                              'source_ids': [s.id for s in sources],
                              'sources': [{'id': s.id, 'title': s.title, 'url': s.url} for s in sources]})
    return documents


def _normalize(value):
    return unicodedata.normalize('NFKC', value).casefold()


def _tokens(text, *, single=False):
    tokens = []
    for word in TOKEN_RE.findall(_normalize(text)):
        if word in STOP_WORDS:
            continue
        if re.fullmatch(r'[\u4e00-\u9fff]+', word):
            if len(word) == 1:
                if single:
                    tokens.append(word)
            else:
                tokens.extend(word[i:i + 2] for i in range(len(word) - 1) if word[i:i + 2] not in STOP_WORDS)
                if len(word) > 2:
                    tokens.append(word)
        else:
            tokens.append(word)
    return tokens


def _contains(text, term):
    if not term:
        return False
    if re.search(r'[a-z0-9]', term):
        return re.search(r'(?<![a-z0-9])' + re.escape(term) + r'(?![a-z0-9])', text) is not None
    return term in text


def _identifier_match(query, sku):
    compact = re.sub(r'[\s_-]+', '', _normalize(sku))
    if not compact:
        return False
    # Allow separators within identifiers, with ASCII boundaries (XF2 != XF20).
    pattern = r'(?<![a-z0-9])' + r'[\s_-]*'.join(map(re.escape, compact)) + r'(?![a-z0-9])'
    return re.search(pattern, query) is not None


def _one_edit(left, right):
    """One insertion/deletion/substitution or adjacent transposition."""
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        positions = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        return len(positions) == 1 or (len(positions) == 2 and positions[1] == positions[0] + 1
            and left[positions[0]] == right[positions[1]] and left[positions[1]] == right[positions[0]])
    shorter, longer = sorted((left, right), key=len)
    return any(shorter == longer[:i] + longer[i + 1:] for i in range(len(longer)))


def _sku_transposition(query, sku):
    sku = re.sub(r'[\s_-]+', '', _normalize(sku))
    # Long codes only; do not confuse adjacent short color numbers.
    if len(re.findall(r'\d', sku)) < 5:
        return False
    for word in TOKEN_RE.findall(query):
        word = re.sub(r'[_-]', '', word)
        if len(word) != len(sku) or not re.search(r'\d', word):
            continue
        changed = [i for i, (a, b) in enumerate(zip(word, sku)) if a != b]
        if len(changed) == 2 and changed[1] == changed[0] + 1 and _one_edit(word, sku):
            return True
    return False


def _baseline(documents, query):
    # Frozen V2 semantics: no NFKC, synonym, stop-word or token-boundary changes.
    normalized = query.casefold()
    tokens = set(re.findall(r'[a-z0-9][a-z0-9-]*|[\u4e00-\u9fff]+', normalized))
    for word in list(tokens):
        if re.fullmatch(r'[\u4e00-\u9fff]+', word):
            tokens.update(word[i:i + 2] for i in range(len(word) - 1))
    ranked = []
    for doc in documents:
        haystack = (doc['title'] + ' ' + doc.get('text', doc['excerpt'])).casefold()
        score = sum(len(t) for t in tokens if t in haystack) + (30 if doc['title'].casefold() in normalized else 0)
        if score:
            ranked.append((0, float(score), doc, ['V2 词元子串匹配']))
    return ranked


def rank_documents(documents: list[dict], query: str, strategy='ranked', k=6) -> list[dict]:
    """Rank one already-authorized corpus snapshot. Scores are not probabilities."""
    if strategy not in ('baseline', 'ranked'):
        raise ValueError('Unknown retrieval strategy')
    if not isinstance(k, int) or not 1 <= k <= 20:
        raise ValueError('k must be between 1 and 20')
    if strategy == 'baseline':
        ranked = _baseline(documents, query)
    else:
        # Chat can join four 16k turns; retain its newest bounded context.
        normalized = _normalize(query[-64000:]).strip()
        cleaned = normalized
        for phrase in STOP_PHRASES:
            if re.fullmatch(r'[\u4e00-\u9fff]+', phrase):
                cleaned = cleaned.replace(phrase, ' ')
        terms = set(_tokens(cleaned, single=len(cleaned.strip()) == 1))
        if not terms:
            return []
        weights = {term: 1.0 for term in terms}
        expanded = set()
        for group in SYNONYMS:
            if any(_contains(normalized, term) for term in group):
                for term in group:
                    for token in _tokens(term):
                        if token not in weights:
                            weights[token] = .65
                            expanded.add(token)
        prepared = []
        for doc in documents:
            # Weighted fields plus BM25 saturation avoid raw body repetition wins.
            title = _normalize(doc['title'])
            body = _normalize(doc.get('text', doc['excerpt']))
            aliases = [_normalize(a) for a in doc.get('aliases', [])]
            extra = ' '.join(str(v) for v in doc.get('fields', {}).values())
            counts = Counter(_tokens(title))
            for term in list(counts):
                counts[term] *= 3
            counts.update(_tokens(body))
            counts.update(_tokens(extra))
            for alias in aliases:
                counts.update(_tokens(alias))
            # Explicit single Han character queries remain useful (e.g. 白).
            for term in terms:
                if len(term) == 1 and '\u4e00' <= term <= '\u9fff':
                    counts[term] = (title + ' ' + body).count(term)
                    if not counts[term]:
                        del counts[term]
            prepared.append((doc, counts, title, aliases))
        vocabulary = {word for _, counts, _, _ in prepared for word in counts if re.fullmatch(r'[a-z]{4,40}', word)}
        fuzzy = set()
        for term in sorted(terms):
            if re.fullmatch(r'[a-z]{4,40}', term) and term not in vocabulary:
                for candidate in sorted(vocabulary):
                    if _one_edit(term, candidate):
                        weights.setdefault(candidate, .45)
                        fuzzy.add(candidate)
        lengths = [sum(counts.values()) for _, counts, _, _ in prepared]
        average = sum(lengths) / len(lengths) if lengths else 1
        frequencies = Counter(term for _, counts, _, _ in prepared for term in counts)
        ranked = []
        for (doc, counts, title, aliases), length in zip(prepared, lengths):
            matched = sorted(term for term in weights if counts.get(term))
            exact_sku = _identifier_match(normalized, doc.get('sku', ''))
            exact_alias = any(_contains(normalized, alias) for alias in aliases if alias and alias not in STOP_WORDS)
            typo_sku = not exact_sku and _sku_transposition(normalized, doc.get('sku', ''))
            if not matched and not (exact_sku or exact_alias or typo_sku):
                continue
            score = 0.0
            for term in matched:
                tf = counts[term]
                idf = math.log(1 + (len(documents) - frequencies[term] + .5) / (frequencies[term] + .5))
                score += weights[term] * idf * (tf * 2.2) / (tf + 1.2 * (.25 + .75 * length / (average or 1)))
            reasons = []
            tier = 3 if exact_sku else 2 if exact_alias else 1 if typo_sku else 0
            if exact_sku:
                reasons.append('编号规范化精确匹配')
            if exact_alias:
                reasons.append('别名精确匹配')
            if typo_sku:
                reasons.append('长编号相邻字符容错（需核对型号）')
            if any(t in expanded for t in matched):
                reasons.append('有限领域同义词匹配（不确认兼容性）')
            if any(t in fuzzy for t in matched):
                reasons.append('英文单次编辑容错')
            direct = [t for t in matched if t in terms]
            if direct:
                reasons.append('词法匹配：' + '、'.join(direct[:8]))
            # Tier is explicit, never swamped by long-query BM25 sums.
            ranked.append((tier, score + tier * 100, doc, reasons))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]['type'], item[2]['id']))
    return [{**{field: doc[field] for field in PUBLIC_FIELDS}, 'rank': rank,
             'score': round(score, 6), 'reasons': reasons}
            for rank, (_, score, doc, reasons) in enumerate(ranked[:k], 1)]


ONLINE_RETRIEVAL_VERSION = 'p0-indexed-chunks-1'


def search_documents(db: Session, query: str, k=6) -> list[dict]:
    """Query persisted postings, then rank at most 200 matching chunks.

    Authorization is evaluated before the SQL limit. Full-corpus snapshots remain
    available only to the explicitly offline evaluation functions above.
    """
    from sqlalchemy import and_, or_, func, case
    from .models import Source, Attachment
    from .ingestion import index_terms
    from .ingestion_models import Document, Chunk, Posting, DocumentSource
    if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= 20:
        raise ValueError('k must be between 1 and 20')
    normalized = _normalize(query[-64000:]).strip()
    cleaned = normalized
    for phrase in STOP_PHRASES:
        if re.fullmatch(r'[\u4e00-\u9fff]+', phrase):
            cleaned = cleaned.replace(phrase, ' ')
    words = set(_tokens(cleaned, single=len(cleaned.strip()) == 1))
    if not words:
        return []
    for group in SYNONYMS:
        if any(_contains(normalized, term) for term in group):
            for term in group:
                words.update(_tokens(term))
    terms = {'t:' + word for word in words if len(word) <= 190}
    for word in words:
        if re.fullmatch(r'[a-z]{4,40}', word):
            terms.update(term for term in index_terms(word) if term.startswith('f:'))
    # Normalize separator variants without scanning every SKU in the catalog.
    for token in re.findall(r'[a-z0-9]+(?:[\s_-]+[a-z0-9]+)*', normalized):
        pieces = re.split(r'[\s_-]+', token)
        for start in range(len(pieces)):
            for end in range(start + 1, min(len(pieces), start + 4) + 1):
                compact = ''.join(pieces[start:end])
                if re.search(r'\d', compact):
                    terms.add('s:' + compact)
    # Long chat history remains bounded before building the IN expression.
    terms = sorted(terms)[:1500]
    valid_source = and_(Source.owner_id == 'local', Source.status == 'published')
    valid_technique = select(Technique.id).where(
        Technique.id == Document.record_id, Technique.owner_id == 'local',
        Technique.status == 'published').exists()
    valid_product = select(Product.id).where(
        Product.id == Document.record_id, Product.owner_id == 'local',
        Product.status == 'published').exists()
    bound_source = select(DocumentSource.source_id).join(Source, Source.id == DocumentSource.source_id).where(
        DocumentSource.document_id == Document.id, DocumentSource.version == Chunk.version, valid_source).exists()
    current_image = or_(Chunk.attachment_id.is_(None), select(Attachment.id).where(
        Attachment.id == Chunk.attachment_id, Attachment.owner_id == 'local', Attachment.status == 'published',
        Attachment.sha256 == Chunk.payload['attachment_hash'].as_string()).exists())
    weight = func.sum(case((Posting.term.like('s:%'), 100), (Posting.term.like('t:%'), 3), else_=1))
    statement = (select(Chunk.id, weight.label('weight')).join(Posting, Posting.chunk_id == Chunk.id)
                 .join(Document, Document.id == Chunk.document_id)
                 .where(Posting.term.in_(terms), Chunk.version == Document.active_version, bound_source, current_image,
                        or_(and_(Document.kind == 'technique', valid_technique),
                            and_(Document.kind == 'product', valid_product)))
                 .group_by(Chunk.id).order_by(weight.desc(), Chunk.id.desc()).limit(200))
    candidates = list(db.execute(statement))
    if not candidates:
        return []
    chunks = {row.id: row for row in db.scalars(select(Chunk).where(Chunk.id.in_([x.id for x in candidates])))}
    # Short records retain their complete readable evidence. Only the bounded
    # candidate document IDs participate in this second query; long documents
    # keep their highest matching chunk as the excerpt.
    document_ids = {chunk.document_id for chunk in chunks.values()}
    short_ids = list(db.scalars(select(Chunk.document_id).join(Document, Document.id == Chunk.document_id)
                    .where(Chunk.document_id.in_(document_ids), Chunk.version == Document.active_version)
                    .group_by(Chunk.document_id).having(func.sum(func.length(Chunk.content)) <= 2800)))
    short_text = {}
    if short_ids:
        for chunk in db.scalars(select(Chunk).join(Document, Document.id == Chunk.document_id)
                                .where(Chunk.document_id.in_(short_ids), Chunk.version == Document.active_version, current_image)
                                .order_by(Chunk.document_id, Chunk.ordinal)):
            short_text.setdefault(chunk.document_id, []).append(chunk.content)
    documents = {}
    long_parts = {}
    matched_images = {}
    for candidate in candidates:
        chunk = chunks[candidate.id]
        payload = chunk.payload
        key = payload['key']
        if chunk.attachment_id is not None and set(_tokens(chunk.content)) & words:
            matched_images.setdefault(key, set()).add(chunk.attachment_id)
        if chunk.document_id not in short_text:
            long_parts.setdefault(key, []).append(chunk.content)
        if key in documents:
            # Include several matched sections without unbounded evidence growth.
            if chunk.document_id not in short_text and len(documents[key]['text']) < 6400:
                documents[key]['text'] += '\n' + chunk.content
            continue
        sources = list(db.scalars(select(Source).join(DocumentSource, DocumentSource.source_id == Source.id)
                       .where(DocumentSource.document_id == chunk.document_id,
                              DocumentSource.version == chunk.version, valid_source).order_by(Source.id)))
        evidence_text = '\n'.join(short_text[chunk.document_id]) if chunk.document_id in short_text else chunk.content
        documents[key] = {**payload, 'text': evidence_text, 'excerpt': evidence_text[:3000],
                          'source_ids': [s.id for s in sources],
                          'sources': [{'id': s.id, 'title': s.title, 'url': s.url} for s in sources]}
    # Generate at most four windows per 1600-character chunk. Coverage is
    # measured on the actual emitted window, never on text clipped from it.
    for key, parts in long_parts.items():
        windows = []
        seen_windows = set()
        for text in parts:
            normalized_text = _normalize(text)
            matched = set(_tokens(text)) & words
            starts = {max(0, normalized_text.find(term) - 200) // 400 * 400
                      for term in matched if term in normalized_text}
            for start in sorted(starts):
                snippet = text[start:start + 950]
                from .ingestion import IMAGE_ORIGINS
                origin = next((label for label in IMAGE_ORIGINS if text.startswith(label)), '')
                if origin and not snippet.startswith(origin):
                    snippet = origin + '\n' + snippet
                if snippet not in seen_windows:
                    seen_windows.add(snippet)
                    windows.append((snippet, set(_tokens(snippet)) & words))
        uncovered = set(words)
        excerpts = []
        while windows and uncovered and len(excerpts) < 3:
            index = max(range(len(windows)), key=lambda i: (len(windows[i][1] & uncovered), -i))
            snippet, covered = windows.pop(index)
            if not (covered & uncovered):
                break
            excerpts.append(snippet)
            uncovered.difference_update(covered)
        if excerpts:
            documents[key]['excerpt'] = '\n…\n'.join(excerpts)
    hits = rank_documents(list(documents.values()), query, k=k)
    for hit in hits:
        hit['attachment_ids'] = sorted(matched_images.get(hit['key'], set()))
    return hits


# Register tables and commit-time indexing for ORM callers as well as the API.
from . import ingestion as _ingestion
