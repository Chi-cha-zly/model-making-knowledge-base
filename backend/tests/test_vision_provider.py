import asyncio
import base64
import json
import httpx
import pytest
from app.llm import LangChainProvider
from app.errors import ProviderError

DESCRIPTION = {'description': '剑刃的灰色高光示范', 'materials': ['金属效果'],
               'techniques': ['NMM'], 'colors': ['灰色'], 'step_number': 2,
               'visible_text': 'AK11190', 'uncertainties': ['无法从图像确认实际油漆型号']}


def test_vision_is_explicit_structured_and_never_uses_chat_endpoint():
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={'id': 'v', 'model': 'vision-test',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': json.dumps(DESCRIPTION)}, 'finish_reason': 'stop'}]})
    provider = LangChainProvider(settings={'AI_BASE_URL': 'https://chat.invalid/v1', 'AI_API_KEY': 'chat-key',
        'AI_MODEL': 'chat-only', 'VISION_BASE_URL': 'https://vision.invalid/v1', 'VISION_API_KEY': 'vision-key',
        'VISION_MODEL': 'vision-test'}, transport=httpx.MockTransport(respond))
    image = b'\xff\xd8\xfftest-image'
    assert asyncio.run(provider.describe_image(image, 'image/jpeg')) == DESCRIPTION
    assert len(calls) == 1 and calls[0].url.host == 'vision.invalid'
    body = json.loads(calls[0].content)
    assert body['model'] == 'vision-test'
    assert base64.b64encode(image).decode() in json.dumps(body)


def test_vision_unconfigured_and_malformed_output_are_not_success():
    provider = LangChainProvider(settings={'AI_MODEL': 'chat', 'AI_BASE_URL': 'https://chat.invalid', 'AI_API_KEY': 'x'})
    assert not provider.vision_configured
    with pytest.raises(ProviderError):
        asyncio.run(provider.describe_image(b'\xff\xd8\xffx', 'image/jpeg'))
    def respond(request):
        return httpx.Response(200, json={'id': 'v', 'model': 'vision',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '{"description":"invented"}'}, 'finish_reason': 'stop'}]})
    provider = LangChainProvider(settings={'VISION_MODEL': 'vision', 'VISION_BASE_URL': 'https://vision.invalid', 'VISION_API_KEY': 'x'}, transport=httpx.MockTransport(respond))
    with pytest.raises(ProviderError):
        asyncio.run(provider.describe_image(b'\xff\xd8\xffx', 'image/jpeg'))


def test_vision_schema_bounds_list_items_for_backups_and_model_results():
    from app.vision_models import ImageDescription
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ImageDescription.model_validate({**DESCRIPTION, 'materials': ['x' * 501]})
