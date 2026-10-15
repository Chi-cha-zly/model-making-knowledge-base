from hashlib import sha256
from io import BytesIO
import struct
from uuid import UUID, uuid4
import zlib

from PIL import Image
import pytest

from app.nmm.storage import (
    ImageValidationError,
    NmmStorage,
    StoragePathError,
    normalize_image,
    validate_mask,
)


def encoded(image, format='PNG', **options):
    stream = BytesIO()
    image.save(stream, format, **options)
    return stream.getvalue()


@pytest.mark.parametrize(
    'format,mime', [('PNG', 'image/png'), ('JPEG', 'image/jpeg'), ('WEBP', 'image/webp')]
)
def test_normalizes_supported_images_to_png(format, mime):
    result = normalize_image(encoded(Image.new('RGB', (5, 3), 'red'), format), mime)
    assert (result.width, result.height, result.content_type) == (5, 3, 'image/png')
    with Image.open(BytesIO(result.data)) as image:
        image.load()
        assert image.format == 'PNG' and image.mode == 'RGBA'
    assert normalize_image(result.data, result.content_type) == result


def test_exif_orientation_applies_once_and_metadata_is_removed():
    image = Image.new('RGB', (3, 2))
    image.putpixel((0, 0), (255, 0, 0))
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = 'private source description'
    result = normalize_image(encoded(image, exif=exif), 'image/png')
    with Image.open(BytesIO(result.data)) as output:
        assert output.size == (2, 3)
        assert output.getpixel((1, 0)) == (255, 0, 0, 255)
        assert not output.getexif()
    assert b'private source description' not in result.data


@pytest.mark.parametrize(
    'raw,mime', [(b'not an image', 'image/png'), (b'', 'image/png'), (b'GIF89a', 'image/gif')]
)
def test_rejects_bad_or_unsupported_image(raw, mime):
    with pytest.raises(ImageValidationError):
        normalize_image(raw, mime)


def test_rejects_mime_mismatch_truncation_and_corrupt_payload():
    raw = encoded(Image.new('RGB', (10, 10), 'blue'))
    for data, mime in [(raw, 'image/jpeg'), (raw[:-15], 'image/png')]:
        with pytest.raises(ImageValidationError):
            normalize_image(data, mime)
    damaged = bytearray(raw)
    damaged[raw.index(b'IDAT') + 6] ^= 255
    with pytest.raises(ImageValidationError):
        normalize_image(bytes(damaged), 'image/png')


@pytest.mark.parametrize('format,mime', [('PNG', 'image/png'), ('WEBP', 'image/webp')])
def test_rejects_animation(format, mime):
    raw = encoded(
        Image.new('RGB', (3, 3), 'red'),
        format,
        save_all=True,
        append_images=[Image.new('RGB', (3, 3), 'blue')],
        duration=100,
        loop=0,
    )
    with pytest.raises(ImageValidationError, match='animated'):
        normalize_image(raw, mime)


def test_enforces_byte_and_pixel_limits_and_pillow_bomb_warning(monkeypatch):
    raw = encoded(Image.new('RGB', (10, 10)))
    with pytest.raises(ImageValidationError, match='byte'):
        normalize_image(raw, 'image/png', max_bytes=len(raw) - 1)
    with pytest.raises(ImageValidationError, match='pixel'):
        normalize_image(raw, 'image/png', max_pixels=99)
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 60)
    with pytest.raises(ImageValidationError):
        normalize_image(raw, 'image/png')
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 40)
    with pytest.raises(ImageValidationError):
        normalize_image(raw, 'image/png')


def test_mask_is_binary_same_size_nonempty_and_white_means_edit():
    image = Image.new('L', (4, 3), 0)
    image.putpixel((2, 1), 255)
    result = validate_mask(encoded(image), 'image/png', width=4, height=3)
    with Image.open(BytesIO(result.data)) as output:
        assert output.getpixel((0, 0)) == (0, 0, 0, 255)
        assert output.getpixel((2, 1)) == (255, 255, 255, 255)
    with pytest.raises(ImageValidationError, match='size'):
        validate_mask(encoded(image), 'image/png', width=3, height=4)


@pytest.mark.parametrize(
    'color', [(0, 0, 0, 255), (128, 128, 128, 255), (255, 0, 0, 255), (255, 255, 255, 0)]
)
def test_rejects_empty_grey_colored_or_transparent_masks(color):
    with pytest.raises(ImageValidationError):
        validate_mask(encoded(Image.new('RGBA', (2, 2), color)), 'image/png', width=2, height=2)


def test_storage_round_trip_hash_unique_name_and_project_isolation(tmp_path):
    storage = NmmStorage(tmp_path / 'attachments')
    project = uuid4()
    image = normalize_image(encoded(Image.new('RGB', (2, 3), 'green')), 'image/png')
    first = storage.put(project, image)
    second = storage.put(project, image)
    assert storage.root == tmp_path / 'nmm'
    assert first.name != second.name
    UUID(first.name.removesuffix('.png'))
    assert first.sha256 == sha256(image.data).hexdigest()
    assert (first.size, first.width, first.height, first.content_type) == (
        len(image.data),
        2,
        3,
        'image/png',
    )
    assert storage.read(project, first.name) == image.data
    with pytest.raises(FileNotFoundError):
        storage.read(uuid4(), first.name)
    storage.delete(project, first.name)
    storage.delete(project, first.name)
    assert storage.read(project, second.name) == image.data
    with pytest.raises(FileNotFoundError):
        storage.read(project, first.name)


@pytest.mark.parametrize(
    'project,name',
    [
        ('../escape', 'x.png'),
        ('/tmp/x', 'x.png'),
        (str(uuid4()), '../x.png'),
        (str(uuid4()), '/tmp/x.png'),
        (str(uuid4()), 'x.png'),
    ],
)
def test_rejects_untrusted_paths_before_filesystem_access(tmp_path, project, name):
    storage = NmmStorage(tmp_path / 'attachments')
    with pytest.raises(StoragePathError):
        storage.read(project, name)
    with pytest.raises(StoragePathError):
        storage.delete(project, name)
    assert not storage.root.exists()


@pytest.mark.parametrize('target', ['root', 'project', 'file'])
def test_storage_refuses_symlinks_without_touching_target(tmp_path, target):
    storage = NmmStorage(tmp_path / 'attachments')
    project = uuid4()
    name = f'{uuid4()}.png'
    outside = tmp_path / 'outside'
    outside.mkdir()
    secret = outside / name
    secret.write_bytes(b'keep')
    if target == 'root':
        storage.root.symlink_to(outside, target_is_directory=True)
    else:
        storage.root.mkdir()
        directory = storage.root / str(project)
        if target == 'project':
            directory.symlink_to(outside, target_is_directory=True)
        else:
            directory.mkdir()
            (directory / name).symlink_to(secret)
    for operation in (storage.read, storage.delete):
        with pytest.raises(StoragePathError):
            operation(project, name)
    if target != 'file':
        image = normalize_image(encoded(Image.new('RGB', (1, 1))), 'image/png')
        with pytest.raises(StoragePathError):
            storage.put(project, image)
    assert secret.read_bytes() == b'keep'


def test_single_frame_apng_and_truncated_jpeg_are_rejected():
    image = Image.new('RGB', (4, 3), 'blue')

    # Pillow saves a single-frame save_all image as ordinary PNG; construct a real APNG.
    def chunk(kind, payload):
        body = kind + payload
        return struct.pack('>I', len(payload)) + body + struct.pack('>I', zlib.crc32(body))

    png = encoded(image)
    control = chunk(b'acTL', struct.pack('>II', 1, 0))
    frame = chunk(b'fcTL', struct.pack('>IIIIIHHBB', 0, 4, 3, 0, 0, 1, 10, 0, 0))
    apng = png[:33] + control + frame + png[33:]
    with Image.open(BytesIO(apng)) as fixture:
        assert fixture.n_frames == 1 and fixture.info['loop'] == 0
    jpeg = encoded(image, 'JPEG')[:-2]
    for raw, mime in [(apng, 'image/png'), (jpeg, 'image/jpeg')]:
        with pytest.raises(ImageValidationError):
            normalize_image(raw, mime)


def test_default_byte_limit_is_enforced_before_decoding(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('oversized bytes reached decoder')

    monkeypatch.setattr(Image, 'open', forbidden)
    with pytest.raises(ImageValidationError, match='byte'):
        normalize_image(b'x' * (20 * 1024 * 1024 + 1), 'image/png')


def test_storage_cleans_up_failed_writes_and_never_overwrites_existing_uuid(tmp_path, monkeypatch):
    import app.nmm.storage as module

    storage = NmmStorage(tmp_path / 'attachments')
    project = uuid4()
    image = normalize_image(encoded(Image.new('RGB', (2, 2), 'red')), 'image/png')
    existing = storage.put(project, image)
    fixed = UUID(existing.name[:-4])
    with monkeypatch.context() as patch:
        patch.setattr(module, 'uuid4', lambda: fixed)
        with pytest.raises(FileExistsError):
            storage.put(project, image)
    assert storage.read(project, existing.name) == image.data
    original_sync = module.os.fsync

    def fail_sync(fd):
        raise OSError('simulated disk error')

    monkeypatch.setattr(module.os, 'fsync', fail_sync)
    with pytest.raises(OSError, match='simulated disk error'):
        storage.put(project, image)
    monkeypatch.setattr(module.os, 'fsync', original_sync)
    assert [path.name for path in (storage.root / str(project)).iterdir()] == [existing.name]


def test_storage_supports_explicit_size_limits(tmp_path):
    image = normalize_image(encoded(Image.new('RGB', (2, 2))), 'image/png')
    storage = NmmStorage(tmp_path / 'attachments', max_bytes=len(image.data) - 1)
    with pytest.raises(ImageValidationError, match='byte'):
        storage.put(uuid4(), image)
    assert not storage.root.exists()


def test_storage_resolves_trusted_attachment_parent_symlink(tmp_path):
    parent = tmp_path / 'linked-parent'
    real = tmp_path / 'real-parent'
    real.mkdir()
    parent.symlink_to(real, target_is_directory=True)
    storage = NmmStorage(parent / 'attachments')
    image = normalize_image(encoded(Image.new('RGB', (1, 1))), 'image/png')
    project = uuid4()
    saved = storage.put(project, image)
    assert storage.root == real / 'nmm'
    assert storage.read(project, saved.name) == image.data
    storage.delete(project, saved.name)
    assert not (real / 'nmm' / str(project) / saved.name).exists()


def test_embedded_icc_uses_color_transform_and_keeps_alpha(monkeypatch):
    from PIL import ImageCms

    source = Image.new('RGBA', (3, 2))
    source.putdata(
        [
            (30, 60, 90, 0),
            (70, 90, 120, 1),
            (10, 100, 250, 64),
            (240, 80, 20, 128),
            (1, 2, 3, 200),
            (250, 230, 200, 255),
        ]
    )
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB'))
    reference = ImageCms.profileToProfile(
        source,
        profile,
        ImageCms.createProfile('sRGB'),
        outputMode='RGB',
        renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
    ).convert('RGBA')
    reference.putalpha(source.getchannel('A'))
    original = ImageCms.profileToProfile
    transformed = []

    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        transformed.append(result.size)
        return result

    monkeypatch.setattr(ImageCms, 'profileToProfile', observe)
    result = normalize_image(encoded(source, icc_profile=profile.tobytes()), 'image/png')
    assert transformed == [source.size]
    with Image.open(BytesIO(result.data)) as output:
        assert output.tobytes() == reference.tobytes()
        assert output.getchannel('A').tobytes() == source.getchannel('A').tobytes()
        assert 'icc_profile' not in output.info
    assert normalize_image(result.data, result.content_type) == result
    assert len(transformed) == 1


@pytest.mark.parametrize('profile_kind', ['invalid', 'incompatible'])
def test_rejects_invalid_or_incompatible_embedded_icc(profile_kind):
    from PIL import ImageCms

    profile = (
        b'not an ICC profile'
        if profile_kind == 'invalid'
        else ImageCms.ImageCmsProfile(ImageCms.createProfile('LAB')).tobytes()
    )
    raw = encoded(Image.new('RGB', (2, 2), 'orange'), icc_profile=profile)
    with pytest.raises(ImageValidationError, match='ICC'):
        normalize_image(raw, 'image/png')


def test_without_icc_uses_srgb_assumption_without_guessing_from_exif(monkeypatch):
    from PIL import ImageCms

    def forbidden(*args, **kwargs):
        raise AssertionError('No ICC profile was provided')

    monkeypatch.setattr(ImageCms, 'profileToProfile', forbidden)
    source = Image.new('RGBA', (2, 2), (40, 80, 160, 17))
    exif = Image.Exif()
    exif[40961] = 65535  # EXIF uncalibrated color space is not a usable ICC profile.
    result = normalize_image(encoded(source, exif=exif), 'image/png')
    with Image.open(BytesIO(result.data)) as output:
        assert output.tobytes() == source.tobytes()
