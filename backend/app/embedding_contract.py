"""The persisted embedding space. Never resize or manufacture model vectors."""

import math

from .errors import ProviderError

EMBEDDING_DIMENSIONS = 1024
QUERY_MODELS = frozenset({'text-embedding-v4'})
DIMENSION_MODELS = frozenset({'text-embedding-v3', 'text-embedding-v4'})
FLOAT32_MAX = 3.4028234663852886e38


def validate_vectors(vectors, count):
    if not isinstance(vectors, list) or len(vectors) != count or not vectors:
        raise ProviderError('向量服务返回数量不匹配。')
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS:
            raise ProviderError('向量维度必须为 1024；原向量保持不变。')
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or abs(value) > FLOAT32_MAX
            for value in vector
        ):
            raise ProviderError('向量服务返回无效数值。')
        if not any(abs(value) >= 1.401298464324817e-45 for value in vector):
            raise ProviderError('向量服务返回零向量，无法进行余弦检索。')
    return EMBEDDING_DIMENSIONS


def embedding_text(content, payload):
    """Actual encoder input, shared by publication and retrieval integrity checks."""
    return content if content.strip() else payload.get('title', '')


def embedding_batch_limit(model):
    """One logical task batch must never span multiple provider HTTP requests."""
    return 10 if model in DIMENSION_MODELS else 64
