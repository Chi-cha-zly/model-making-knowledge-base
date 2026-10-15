"""Deterministic local relevance retrieval; scores never assert compatibility.

Internal documents retain full V2 evidence in ``text`` for fair baseline scoring.
``fields``, ``sku`` and ``aliases`` add ranked-only signals; public hits omit them.
"""

from collections import Counter
import math
import re

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Product, Technique
from .lexical import readable_excerpt, _normalize, _tokens, STOP_WORDS, STOP_PHRASES, TOKEN_RE  # noqa: F401

RETRIEVAL_VERSION = 'v3-local-bm25-1'
BASELINE_VERSION = 'v2-lexical'
PUBLIC_FIELDS = ('key', 'type', 'id', 'title', 'excerpt', 'source_ids', 'sources')
# Narrow vocabulary equivalence only; none of these imply material compatibility.
SYNONYMS = (
    ('水口', '浇口'),
    ('水贴', '水转印', 'decal', 'decals'),
    ('喷笔', 'airbrush'),
    ('渗线', '勾线', 'panel lining'),
    ('打磨', '砂磨', 'sanding'),
    ('消光', '哑光', 'matte'),
    ('遮盖', '遮蔽', 'masking'),
)


def load_documents(db: Session) -> list[dict]:
    documents = []
    for model, kind in ((Technique, 'technique'), (Product, 'product')):
        statement = (
            select(model)
            .where(model.owner_id == 'local', model.status == 'published')
            .options(selectinload(model.sources))
        )
        if model is Product:
            statement = statement.options(selectinload(Product.aliases))
        for row in db.scalars(statement.order_by(model.id)):
            sources = sorted(
                (s for s in row.sources if s.owner_id == 'local' and s.status == 'published'),
                key=lambda s: s.id,
            )
            if not sources:
                continue
            if kind == 'technique':
                title = row.title
                # Keep labels, order and fields identical to V2's original body.
                body = readable_excerpt(
                    {
                        '摘要': row.summary,
                        '正文': [
                            b.get('text', b.get('caption', '')) for b in row.content_blocks or []
                        ],
                        '前提条件': row.prerequisites,
                        '风险': row.risks,
                        '难度': row.difficulty,
                        '标签': row.tags,
                    }
                )
                sku, aliases = '', []
                fields = {'category': row.category or '', 'tags': readable_excerpt(row.tags)}
            else:
                title = ' '.join(filter(None, (row.brand, row.name, row.sku)))
                aliases = [a.alias for a in row.aliases]
                body = readable_excerpt({'属性': row.properties, '类别': row.kind, '别名': aliases})
                sku = row.sku or ''
                fields = {'brand': row.brand or '', 'name': row.name, 'category': row.kind or ''}
            documents.append(
                {
                    'key': f'{kind}:{row.id}',
                    'type': kind,
                    'id': row.id,
                    'title': title,
                    'excerpt': body[:3000],
                    'text': body,
                    'sku': sku,
                    'aliases': aliases,
                    'fields': fields,
                    'source_ids': [s.id for s in sources],
                    'sources': [{'id': s.id, 'title': s.title, 'url': s.url} for s in sources],
                }
            )
    return documents


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
        return len(positions) == 1 or (
            len(positions) == 2
            and positions[1] == positions[0] + 1
            and left[positions[0]] == right[positions[1]]
            and left[positions[1]] == right[positions[0]]
        )
    shorter, longer = sorted((left, right), key=len)
    return any(shorter == longer[:i] + longer[i + 1 :] for i in range(len(longer)))


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
            tokens.update(word[i : i + 2] for i in range(len(word) - 1))
    ranked = []
    for doc in documents:
        haystack = (doc['title'] + ' ' + doc.get('text', doc['excerpt'])).casefold()
        score = sum(len(t) for t in tokens if t in haystack) + (
            30 if doc['title'].casefold() in normalized else 0
        )
        if score:
            ranked.append((0, float(score), doc, ['V2 词元子串匹配']))
    return ranked


def rank_candidates(documents: list[dict], query: str, strategy='ranked', k=6) -> list[dict]:
    """Rank one already-authorized corpus snapshot. Scores are not probabilities."""
    if strategy not in ('baseline', 'ranked'):
        raise ValueError('Unknown retrieval strategy')
    if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= 400:
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
                            weights[token] = 0.65
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
        vocabulary = {
            word
            for _, counts, _, _ in prepared
            for word in counts
            if re.fullmatch(r'[a-z]{4,40}', word)
        }
        fuzzy = set()
        for term in sorted(terms):
            if re.fullmatch(r'[a-z]{4,40}', term) and term not in vocabulary:
                for candidate in sorted(vocabulary):
                    if _one_edit(term, candidate):
                        weights.setdefault(candidate, 0.45)
                        fuzzy.add(candidate)
        lengths = [sum(counts.values()) for _, counts, _, _ in prepared]
        average = sum(lengths) / len(lengths) if lengths else 1
        frequencies = Counter(term for _, counts, _, _ in prepared for term in counts)
        ranked = []
        for (doc, counts, title, aliases), length in zip(prepared, lengths):
            matched = sorted(term for term in weights if counts.get(term))
            exact_sku = _identifier_match(normalized, doc.get('sku', ''))
            exact_alias = any(
                _contains(normalized, alias)
                for alias in aliases
                if alias and alias not in STOP_WORDS
            )
            typo_sku = not exact_sku and _sku_transposition(normalized, doc.get('sku', ''))
            if not matched and not (exact_sku or exact_alias or typo_sku):
                continue
            score = 0.0
            for term in matched:
                tf = counts[term]
                idf = math.log(
                    1 + (len(documents) - frequencies[term] + 0.5) / (frequencies[term] + 0.5)
                )
                score += (
                    weights[term]
                    * idf
                    * (tf * 2.2)
                    / (tf + 1.2 * (0.25 + 0.75 * length / (average or 1)))
                )
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
    return [
        {
            **{field: doc[field] for field in PUBLIC_FIELDS},
            'rank': rank,
            'score': round(score, 6),
            'reasons': reasons,
        }
        for rank, (_, score, doc, reasons) in enumerate(ranked[:k], 1)
    ]


def rank_documents(documents: list[dict], query: str, strategy='ranked', k=6) -> list[dict]:
    if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= 20:
        raise ValueError('k must be between 1 and 20')
    return rank_candidates(documents, query, strategy=strategy, k=k)


ONLINE_RETRIEVAL_VERSION = 'p0-indexed-chunks-1'


def search_documents(db: Session, query: str, k=6) -> list[dict]:
    """Compatibility entry point for the indexed lexical strategy only."""
    from .repositories.retrieval import lexical_search

    if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= 20:
        raise ValueError('k must be between 1 and 20')
    return lexical_search(db, query, k=k)[0]
