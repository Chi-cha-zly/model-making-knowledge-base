"""Bounded local preprocessing for image requests; no remote URLs or file paths."""

from dataclasses import dataclass
from io import BytesIO

from PIL import Image

from .providers import ImageInput
from .storage import MAX_IMAGE_PIXELS, normalize_image, validate_mask

MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_PIXELS = 4_000_000
EDIT_PREPROCESS_VERSION = 'fit-pad-v1'


@dataclass(frozen=True)
class EditTransform:
    """Coordinates for one edit request; padding is never editable."""

    original_size: tuple[int, int]
    canvas_size: tuple[int, int]
    content_box: tuple[int, int, int, int]

    @property
    def api_size(self):
        return f'{self.canvas_size[0]}x{self.canvas_size[1]}'

    def restore(self, image):
        if image.size != self.canvas_size:
            raise ValueError('Returned canvas size does not match the request')
        restored = image.crop(self.content_box)
        if restored.size != self.original_size:
            restored = restored.resize(self.original_size, Image.Resampling.LANCZOS)
        return restored


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
    width, height = original.width, original.height
    canvas_size = (
        (1024, 1024) if width == height else (1536, 1024) if width > height else (1024, 1536)
    )
    scale = min(1, canvas_size[0] / width, canvas_size[1] / height)
    fitted_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    left, top = ((canvas_size[0] - fitted_size[0]) // 2, (canvas_size[1] - fitted_size[1]) // 2)
    transform = EditTransform(
        (width, height), canvas_size, (left, top, left + fitted_size[0], top + fitted_size[1])
    )
    with Image.open(BytesIO(original.data)) as decoded:
        if decoded.getchannel('A').getextrema() != (255, 255):
            raise ValueError('Opaque input required')
        fitted = decoded.convert('RGB').resize(fitted_size, Image.Resampling.LANCZOS)
    source = Image.new('RGB', canvas_size, (0, 0, 0))
    source.paste(fitted, (left, top))
    with Image.open(BytesIO(binary.data)) as decoded:
        fitted_region = decoded.convert('L').resize(fitted_size, Image.Resampling.NEAREST)
    if fitted_region.getextrema()[1] == 0:
        raise ValueError('Empty mask after fitting')
    region = Image.new('L', canvas_size, 0)
    region.paste(fitted_region, (left, top))
    transparent_mask = Image.new('RGBA', canvas_size, (0, 0, 0, 255))
    transparent_mask.putalpha(region.point(lambda value: 255 - value))
    mask_bytes = encode_png(transparent_mask)
    if len(mask_bytes) >= 4 * 1024 * 1024:
        raise ValueError('Mask exceeds protocol byte limit')
    images = [encode_png(source)] + [
        encode_png(prepare_image(reference)) for reference in references
    ]
    if sum(map(len, images)) + len(mask_bytes) > MAX_BODY_BYTES - 128 * 1024:
        raise ValueError('Request body too large')
    return images, mask_bytes, transform
