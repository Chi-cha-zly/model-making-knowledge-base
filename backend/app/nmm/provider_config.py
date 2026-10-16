"""Explicit capability-local configuration; never consult ambient SDK settings."""

from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

from .providers import UnconfiguredImageEditProvider, UnconfiguredVisionProvider

IMAGE_MODELS = frozenset(
    (
        'gpt-image-2.5-sunburst',
        'gpt-image-2.5-flare',
        'gpt-image-2.5-sunburst-2026-09-08',
        'gpt-image-2.5-flare-2026-09-08',
    )
)


class MissingVision(UnconfiguredVisionProvider):
    mode = 'unconfigured'
    safe_message = 'NMM 视觉能力未配置或配置不受支持；仅支持官方百炼 qwen3-vl-plus。'
    public_config = {'provider': 'unconfigured', 'model': None}
    configuration_id = sha256(b'nmm-vision-unconfigured').hexdigest()


class MissingImage(UnconfiguredImageEditProvider):
    mode = 'unconfigured'
    safe_message = 'NMM 图像编辑未配置或配置不受支持；仅支持官方 OpenAI GPT Image 2.5。'
    public_config = {'provider': 'unconfigured', 'model': None}
    configuration_id = sha256(b'nmm-image-unconfigured').hexdigest()


def endpoint(value, *, image):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value.strip())
        if (
            url.scheme != 'https'
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.port not in (None, 443)
        ):
            return None
        host = (url.hostname or '').lower()
        path = url.path.rstrip('/')
        if image:
            return (
                ('https://api.openai.com/v1', None)
                if host == 'api.openai.com' and path == '/v1'
                else None
            )
        if path != '/compatible-mode/v1':
            return None
        if host == 'dashscope.aliyuncs.com' or re.fullmatch(
            r'[a-z0-9][a-z0-9-]{0,62}\.cn-beijing\.maas\.aliyuncs\.com', host
        ):
            region = 'beijing'
        elif host == 'dashscope-intl.aliyuncs.com' or re.fullmatch(
            r'[a-z0-9][a-z0-9-]{0,62}\.ap-southeast-1\.maas\.aliyuncs\.com', host
        ):
            region = 'singapore'
        else:
            return None
        return (f'https://{host}{path}', region)
    except ValueError:
        return None


def _value(settings, name):
    value = settings.get(name)
    return value.strip() if isinstance(value, str) else ''


def configuration_id(config, key):
    return sha256(
        json.dumps(
            {'config': config, 'credential': key}, sort_keys=True, separators=(',', ':')
        ).encode()
    ).hexdigest()


def build_nmm_providers(settings, *, transport=None):
    from .live_providers import DashScopeVisionProvider, OpenAIImageEditProvider

    image, vision = MissingImage(), MissingVision()
    for capability in ('IMAGE', 'VISION'):
        key, model, base = (
            _value(settings, f'{capability}_{suffix}')
            for suffix in ('API_KEY', 'MODEL', 'BASE_URL')
        )
        address = endpoint(base, image=capability == 'IMAGE')
        if (
            not key
            or len(key) > 4096
            or any(ord(char) < 32 or ord(char) > 126 for char in key)
            or not address
        ):
            continue
        if capability == 'IMAGE' and model in IMAGE_MODELS:
            image = OpenAIImageEditProvider(
                base_url=address[0], model=model, api_key=key, transport=transport
            )
        elif capability == 'VISION' and model == 'qwen3-vl-plus':
            vision = DashScopeVisionProvider(
                base_url=address[0],
                model=model,
                api_key=key,
                region=address[1],
                transport=transport,
            )
    return vision, image
