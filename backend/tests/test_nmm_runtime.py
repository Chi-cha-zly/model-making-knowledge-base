"""NMM runtime uses explicit, isolated capability configuration without network work."""

import httpx
import pytest

from app.services.runtime import build_runtime
from nmm_fakes import OfflineVision, RecordingImageEdit

SETTINGS = {
    'VISION_BASE_URL': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    'VISION_MODEL': 'qwen3-vl-plus',
    'VISION_API_KEY': 'synthetic-vision',
    'IMAGE_BASE_URL': 'https://api.openai.com/v1',
    'IMAGE_MODEL': 'gpt-image-2.5-sunburst',
    'IMAGE_API_KEY': 'synthetic-image',
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail('runtime construction must not send HTTP requests')

    monkeypatch.setattr(httpx.AsyncClient, 'send', forbidden)


def runtime(tmp_path, **kwargs):
    return build_runtime(
        'postgresql+psycopg://blocked:blocked@127.0.0.1:1/blocked',
        tmp_path / 'attachments',
        **kwargs,
    )


def test_configured_runtime_builds_live_capabilities_without_calls(tmp_path):
    value = runtime(tmp_path, runtime_settings=SETTINGS)
    try:
        assert value.nmm.vision.mode == 'live'
        assert value.nmm.image_edit.mode == 'live'
        assert value.nmm.provider_mode == 'live'
        assert 'synthetic-' not in str(value.nmm.vision.public_config)
        assert 'synthetic-' not in str(value.nmm.image_edit.public_config)
    finally:
        value.engine.dispose()


def test_explicit_empty_settings_override_ambient_and_chat_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr('app.services.runtime.read_settings', lambda: SETTINGS)
    value = runtime(tmp_path, runtime_settings={})
    try:
        assert value.nmm.provider_mode == 'unconfigured'
    finally:
        value.engine.dispose()
    missing_image = {key: value for key, value in SETTINGS.items() if key != 'IMAGE_API_KEY'}
    missing_image['AI_API_KEY'] = 'synthetic-chat'
    value = runtime(tmp_path / 'second', runtime_settings=missing_image)
    try:
        assert value.nmm.vision.mode == 'live'
        assert value.nmm.image_edit.mode == 'unconfigured'
    finally:
        value.engine.dispose()


def test_explicit_python_injection_wins_per_capability(tmp_path):
    vision, image = OfflineVision(), RecordingImageEdit()
    value = runtime(
        tmp_path, runtime_settings=SETTINGS, nmm_vision_provider=vision, nmm_image_provider=image
    )
    try:
        assert value.nmm.vision is vision and value.nmm.image_edit is image
        assert vision.calls == [] and image.calls == []
    finally:
        value.engine.dispose()
    value = runtime(tmp_path / 'one', runtime_settings=SETTINGS, nmm_vision_provider=vision)
    try:
        assert value.nmm.vision is vision and value.nmm.image_edit.mode == 'live'
    finally:
        value.engine.dispose()


def test_configured_source_is_used_when_runtime_settings_absent(tmp_path, monkeypatch):
    monkeypatch.setattr('app.services.runtime.read_settings', lambda: SETTINGS)
    value = runtime(tmp_path)
    try:
        assert value.nmm.vision.mode == 'live' and value.nmm.image_edit.mode == 'live'
    finally:
        value.engine.dispose()


def test_unsupported_nmm_configuration_does_not_break_application_build(tmp_path):
    value = runtime(tmp_path, runtime_settings=SETTINGS | {'IMAGE_MODEL': 'unsupported-model'})
    try:
        assert value.nmm.image_edit.mode == 'unconfigured'
        assert value.nmm.image_edit.safe_message
    finally:
        value.engine.dispose()
