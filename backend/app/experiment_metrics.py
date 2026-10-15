from datetime import datetime, timezone
import json
import re


def now():
    return datetime.now(timezone.utc).isoformat()


def evaluator(outputs: dict, reference_outputs: dict):
    # Mechanical feedback plus original generation costs; replay duration is separate.
    scores = dict(outputs['metrics'])
    scores.update({key: outputs.get(key) for key in ('latency_ms', 'cost_usd')})
    scores.update(outputs.get('usage') or {})
    return {
        'results': [
            {'key': key, 'score': value} for key, value in scores.items() if value is not None
        ]
    }


def bounded_hits(hits):
    selected = []
    size = 0
    for hit in hits:
        length = len(json.dumps(hit, ensure_ascii=False))
        if size + length <= 24000:
            selected.append(hit)
            size += length
    return selected


def answer_metrics(answer, hits):
    citations = re.findall(r'\[((?:technique|product):[0-9]+)\]', answer)
    valid = {h['key'] for h in hits}
    return {
        'answer_nonempty': float(bool(answer.strip())),
        'citation_validity': sum(c in valid for c in citations) / len(citations)
        if citations
        else None,
        'uncertainty_marker': float(
            any(x in answer for x in ('不足', '无法确认', '不确定', '没有资料', '待确认'))
        )
        if not hits
        else None,
    }


def cost(model, usage, base_url=None):
    from urllib.parse import urlsplit

    if not base_url or urlsplit(base_url).hostname != 'api.openai.com':
        return None
    prices = {'gpt-6.1-sol': (2, 10), 'gpt-6-luna': (0.1, 0.5)}
    if usage is None or model not in prices:
        return None
    i, o = prices[model]
    return (usage['input_tokens'] * i + usage['output_tokens'] * o) / 1_000_000
