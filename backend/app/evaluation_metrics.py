"""Pure reproducible retrieval metrics and canonical fingerprints."""

import hashlib
import json
import math

METRIC_KEYS = ('hit_rate', 'recall', 'mrr', 'ndcg', 'no_answer_accuracy')


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
        ).encode('utf-8')
    ).hexdigest()


def case_metrics(hits, judgments, k):
    grades = {j['key']: j['grade'] for j in judgments}
    if not grades:
        return {
            **{name: None for name in METRIC_KEYS},
            'no_answer_accuracy': float(not hits),
            'positive_cases': 0,
            'no_answer_cases': 1,
        }
    retrieved = hits[:k]
    ranks = [i for i, h in enumerate(retrieved, 1) if h['key'] in grades]
    dcg = sum(
        (2 ** grades.get(h['key'], 0) - 1) / math.log2(i + 1) for i, h in enumerate(retrieved, 1)
    )
    ideal = sum(
        (2**grade - 1) / math.log2(i + 1)
        for i, grade in enumerate(sorted(grades.values(), reverse=True)[:k], 1)
    )
    return {
        'hit_rate': float(bool(ranks)),
        'recall': len({h['key'] for h in retrieved} & grades.keys()) / len(grades),
        'mrr': 1 / ranks[0] if ranks else 0.0,
        'ndcg': dcg / ideal,
        'no_answer_accuracy': None,
        'positive_cases': 1,
        'no_answer_cases': 0,
    }


def aggregate_metrics(rows):
    result = {
        name: sum(vals) / len(vals)
        if (vals := [r[name] for r in rows if r[name] is not None])
        else None
        for name in METRIC_KEYS
    }
    return {
        **result,
        'positive_cases': sum(r['positive_cases'] for r in rows),
        'no_answer_cases': sum(r['no_answer_cases'] for r in rows),
    }
