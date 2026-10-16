"""Bounded local preprocessing for image requests; no remote URLs or file paths."""

from io import BytesIO

from PIL import Image

from .providers import ImageInput
from .storage import MAX_IMAGE_PIXELS, normalize_image, validate_mask

MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_PIXELS = 4_000_000


def encode_png(image):
    stream = BytesIO()
    image.save(stream, 'PNG')
    return stream.getvalue()


def prepare_image(value: ImageInput, *, long_edge=1536, opaque=False, max_pixels=MAX_IMAGE_PIXELS):
    normalized = normalize_image(value.data, value.content_type, max_pixels=max_pixels)
    with Image.open(BytesIO(normalized.data)) as decoded:
        image = decoded.convert('RGBA')
        if opaque and image.getchannel('A').getextrema() != (255, 255):
            raise ValueError('Opaque input required')
        image.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
        return image.convert('RGB') if opaque else image


def prepare_edit(image, mask, references):
    if not isinstance(references, list) or len(references) > 3:
        raise ValueError('At most three references allowed')
    original = normalize_image(image.data, image.content_type, max_pixels=MAX_OUTPUT_PIXELS)
    binary = validate_mask(
        mask.data, mask.content_type, width=original.width, height=original.height
    )
    source = prepare_image(image, opaque=True, max_pixels=MAX_OUTPUT_PIXELS)
    with Image.open(BytesIO(binary.data)) as decoded:
        region = decoded.convert('L').resize(source.size, Image.Resampling.NEAREST)
    if region.getextrema()[1] == 0:
        raise ValueError('Empty mask')
    transparent_mask = Image.new('RGBA', source.size, (0, 0, 0, 255))
    transparent_mask.putalpha(region.point(lambda value: 255 - value))
    mask_bytes = encode_png(transparent_mask)
    if len(mask_bytes) >= 4 * 1024 * 1024:
        raise ValueError('Mask exceeds protocol byte limit')
    images = [encode_png(source)] + [
        encode_png(prepare_image(reference)) for reference in references
    ]
    if sum(map(len, images)) + len(mask_bytes) > MAX_BODY_BYTES - 128 * 1024:
        raise ValueError('Request body too large')
    width, height = source.size
    size = '1024x1024' if width == height else '1536x1024' if width > height else '1024x1536'
    return images, mask_bytes, size
