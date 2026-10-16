from io import BytesIO

from PIL import Image
import pytest

from app.nmm.image_processing import prepare_edit
from app.nmm.providers import ImageInput
from nmm_fakes import make_inputs


@pytest.mark.parametrize(
    'original_size,canvas_size,content_box',
    [
        ((4096, 1), (1536, 1024), (0, 511, 1536, 512)),
        ((1, 4096), (1024, 1536), (511, 0, 512, 1536)),
        ((2000, 2000), (1024, 1024), (0, 0, 1024, 1024)),
        ((1600, 800), (1536, 1024), (0, 128, 1536, 896)),
    ],
)
def test_fit_preserves_aspect_and_protects_every_padding_pixel(
    original_size, canvas_size, content_box
):
    inputs = make_inputs(original_size)
    images, mask, transform = prepare_edit(inputs['original'], inputs['mask'], [])
    with Image.open(BytesIO(images[0])) as source, Image.open(BytesIO(mask)) as alpha:
        assert source.size == alpha.size == canvas_size
        assert alpha.getchannel('A').point(lambda value: 255 - value).getbbox() == content_box
        assert set(alpha.getchannel('A').get_flattened_data()) <= {0, 255}
        assert source.mode == 'RGB'
    assert transform.original_size == original_size
    assert transform.canvas_size == canvas_size
    assert transform.content_box == content_box
    assert transform.api_size == f'{canvas_size[0]}x{canvas_size[1]}'


def test_transparent_editable_source_is_rejected_before_canvas_creation():
    source = Image.new('RGBA', (16, 16), (10, 20, 30, 100))
    stream = BytesIO()
    source.save(stream, 'PNG')
    with pytest.raises(ValueError, match='Opaque'):
        prepare_edit(ImageInput(stream.getvalue(), 'image/png'), make_inputs()['mask'], [])


def test_editable_source_stays_bounded_by_four_megapixels():
    inputs = make_inputs((2001, 2000))
    with pytest.raises(ValueError):
        prepare_edit(inputs['original'], inputs['mask'], [])
