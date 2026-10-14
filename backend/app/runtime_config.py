"""Explicit configuration; callers can inject an isolated settings dictionary."""

import os
import ipaddress
from uuid import UUID
from pathlib import Path
from urllib.parse import urlparse


def _clean(value):
    return str(value).strip().strip('"\'').strip()


def read_settings(path=None, environ=None):
    path = Path(path) if path is not None else Path(__file__).resolve().parents[2] / '.env'
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                values[key.strip()] = _clean(value)
    values.update({k: _clean(v) for k, v in (os.environ if environ is None else environ).items()})
    return values


def loopback_endpoint(value):
    try:
        host = urlparse(value).hostname
        return host == 'localhost' or bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False


def valid_endpoint(value):
    try:
        url = urlparse(value)
        return bool(
            url.hostname
            and url.scheme in ('https', 'http')
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
            and (url.scheme == 'https' or loopback_endpoint(value))
        )
    except ValueError:
        return False


def normalize_model(value):
    return (
        value.strip().lower().replace(' ', '-')
        if value.strip().lower().startswith('gpt-')
        else value.strip()
    )


def output_limit(settings):
    try:
        return max(1, min(2048, int(settings.get('AI_MAX_OUTPUT_TOKENS', '2048'))))
    except (TypeError, ValueError):
        return 2048


def langsmith_config(settings=None):
    values = read_settings() if settings is None else settings
    endpoint = values.get('LANGSMITH_ENDPOINT', 'https://api.smith.langchain.com').rstrip('/')
    workspace_valid = True
    if values.get('LANGSMITH_WORKSPACE_ID'):
        try:
            UUID(values['LANGSMITH_WORKSPACE_ID'])
        except (ValueError, TypeError, AttributeError):
            workspace_valid = False
    configured = bool(
        values.get('LANGSMITH_API_KEY') and valid_endpoint(endpoint) and workspace_valid
    )
    return {
        'configured': configured,
        'tracing_enabled': configured
        and values.get('LANGSMITH_TRACING', '').lower() in ('true', '1', 'yes'),
        'project': values.get('LANGSMITH_PROJECT') or 'modelkb-v4',
        'endpoint': endpoint if valid_endpoint(endpoint) else '',
        'message': (
            'LANGSMITH_WORKSPACE_ID 必须是有效的 UUID。'
            if not workspace_valid
            else 'LangSmith 已配置。'
            if configured
            else '尚未配置 LangSmith；请设置有效的 LANGSMITH_API_KEY 和端点。'
        ),
    }


def langsmith_client(settings=None):
    from langsmith import Client

    values = read_settings() if settings is None else settings
    config = langsmith_config(values)
    if not config['configured']:
        return None
    return Client(
        api_url=config['endpoint'],
        api_key=values['LANGSMITH_API_KEY'],
        auto_batch_tracing=True,
        timeout_ms=15000,
        otel_enabled=False,
        omit_traced_runtime_info=True,
        workspace_id=values.get('LANGSMITH_WORKSPACE_ID') or '',
    )


def integration_status(provider=None, settings=None):
    values = read_settings() if settings is None else settings
    model = getattr(provider, 'model', None) or normalize_model(values.get('AI_MODEL', '')) or None
    configured = getattr(provider, 'configured', None)
    if configured is None:
        endpoint = values.get('AI_BASE_URL', '')
        configured = bool(
            model
            and valid_endpoint(endpoint)
            and (values.get('AI_API_KEY') or loopback_endpoint(endpoint))
        )
    models = list(
        dict.fromkeys(filter(None, [model, normalize_model(values.get('AI_COMPARE_MODEL', ''))]))
    )
    return {
        'provider': 'langchain',
        'model': model,
        'model_configured': configured,
        'compare_models': models,
        'reasoning_effort': getattr(
            provider, 'reasoning_effort', values.get('AI_REASONING_EFFORT') or 'low'
        )
        or 'auto',
        'max_output_tokens': getattr(provider, 'max_output_tokens', output_limit(values)),
        'langsmith': langsmith_config(values),
    }
