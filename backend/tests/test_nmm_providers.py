from io import BytesIO

from PIL import Image
from pydantic import BaseModel, ConfigDict, ValidationError
import pytest

from app.nmm.providers import (
    FakeImageEditProvider,
    FakeVisionProvider,
    ImageInput,
    ProviderUnconfiguredError,
    UnconfiguredImageEditProvider,
    UnconfiguredVisionProvider,
)
from app.nmm.storage import ImageValidationError


class Answer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    summary: str


def image_input(image):
    stream = BytesIO()
    image.save(stream, 'PNG')
    return ImageInput(stream.getvalue(), 'image/png')


@pytest.mark.anyio
async def test_unconfigured_providers_never_fallback_or_read_operator_config(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('private config read')

    monkeypatch.setattr('app.runtime_config.read_settings', forbidden)
    monkeypatch.setenv('VISION_API_KEY', 'synthetic-only')
    image = image_input(Image.new('RGB', (3, 3)))
    with pytest.raises(ProviderUnconfiguredError, match='unconfigured'):
        await UnconfiguredVisionProvider().analyze([image], 'inspect', Answer)
    with pytest.raises(ProviderUnconfiguredError, match='unconfigured'):
        await UnconfiguredImageEditProvider().edit(image, image, [], 'edit', 1)


@pytest.mark.anyio
async def test_fake_vision_consumes_script_and_validates_schema():
    provider = FakeVisionProvider([{'summary': 'synthetic'}, {'unexpected': 'invalid'}])
    image = image_input(Image.new('RGB', (3, 3)))
    result = await provider.analyze([image], 'inspect', Answer)
    assert result == Answer(summary='synthetic')
    with pytest.raises(ValidationError):
        await provider.analyze([image], 'inspect', Answer)
    with pytest.raises(RuntimeError, match='exhausted'):
        await provider.analyze([image], 'inspect', Answer)


@pytest.mark.anyio
async def test_fake_image_is_deterministic_and_preserves_every_unmasked_pixel():
    image = Image.new('RGBA', (6, 5), (10, 20, 30, 70))
    mask = Image.new('L', image.size, 0)
    mask.paste(255, (2, 1, 5, 4))
    base, region = image_input(image), image_input(mask)
    provider = FakeImageEditProvider()
    first = await provider.edit(base, region, [], 'silver', 2)
    assert first == await provider.edit(base, region, [], 'silver', 2)
    assert len(first) == 2 and first[0] != first[1]
    for raw in first:
        with Image.open(BytesIO(raw)) as output:
            assert output.format == 'PNG' and output.size == image.size
            edited = []
            for y in range(image.height):
                for x in range(image.width):
                    if mask.getpixel((x, y)) == 0:
                        assert output.getpixel((x, y)) == image.getpixel((x, y))
                    else:
                        edited.append(output.getpixel((x, y)))
            assert len(set(edited)) > 1
            assert all(pixel != (10, 20, 30, 70) for pixel in edited)


@pytest.mark.anyio
async def test_fake_image_checks_mask_and_output_count():
    provider = FakeImageEditProvider()
    base = image_input(Image.new('RGB', (3, 3)))
    for mask in (Image.new('L', (3, 3), 0), Image.new('L', (2, 2), 255)):
        with pytest.raises(ImageValidationError):
            await provider.edit(base, image_input(mask), [], '', 1)
    for count in (0, -1, 5, True):
        with pytest.raises(ValueError):
            await provider.edit(base, image_input(Image.new('L', (3, 3), 255)), [], '', count)
