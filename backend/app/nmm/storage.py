"""Validated canonical sRGB images in a separate, project-scoped local store.

Embedded ICC profiles are converted to sRGB before metadata is removed. Without an
ICC profile, decoded RGB values are assumed to be sRGB; EXIF color-space hints do
not supply a transform and are not used to guess another source profile.
"""

from contextlib import contextmanager, suppress
from dataclasses import dataclass
import errno
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import stat
from uuid import UUID, uuid4
import warnings

from PIL import Image, ImageCms, ImageOps

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
_FORMATS = {'image/png': 'PNG', 'image/jpeg': 'JPEG', 'image/webp': 'WEBP'}


class ImageValidationError(ValueError):
    pass


class StoragePathError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedImage:
    data: bytes
    content_type: str
    width: int
    height: int


@dataclass(frozen=True)
class StoredImage:
    name: str
    sha256: str
    content_type: str
    size: int
    width: int
    height: int


def _srgb_rgba(image: Image.Image) -> Image.Image:
    if 'icc_profile' not in image.info:
        return image.convert('RGBA')
    try:
        profile_bytes = image.info['icc_profile']
        if not isinstance(profile_bytes, bytes) or not profile_bytes:
            raise ValueError('Empty ICC profile.')
        profile = ImageCms.ImageCmsProfile(BytesIO(profile_bytes))
        # Expand palette/grayscale alpha without reinterpreting CMYK or RGB color values.
        mode = {'P': 'RGBA', 'PA': 'RGBA', 'LA': 'L', '1': 'L'}.get(image.mode, image.mode)
        with image.convert(mode) as source:
            with ImageCms.profileToProfile(
                source,
                profile,
                ImageCms.createProfile('sRGB'),
                outputMode='RGB',
                renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
            ) as converted:
                result = converted.convert('RGBA')
        if 'A' in image.getbands() or 'transparency' in image.info:
            with image.convert('RGBA') as original:
                with original.getchannel('A') as alpha:
                    result.putalpha(alpha)
        return result
    except (ImageCms.PyCMSError, OSError, ValueError, TypeError) as exc:
        raise ImageValidationError('Invalid or unsupported embedded ICC profile.') from exc


def _decode(raw: bytes, content_type: str, max_bytes: int, max_pixels: int) -> Image.Image:
    if not isinstance(raw, bytes) or not raw or len(raw) > max_bytes:
        raise ImageValidationError('Image byte limit exceeded or empty input.')
    if content_type not in _FORMATS:
        raise ImageValidationError('Unsupported image content type.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw), formats=list(_FORMATS.values())) as source:
                if source.format != _FORMATS[content_type]:
                    raise ImageValidationError('Image content type does not match decoded format.')
                if source.width * source.height > max_pixels:
                    raise ImageValidationError('Image pixel limit exceeded.')
                if getattr(source, 'n_frames', 1) != 1 or (
                    source.format == 'PNG' and 'loop' in source.info
                ):
                    raise ImageValidationError('An animated image is not supported.')
                source.verify()
            # verify checks integrity; a fresh open + load completes decoding.
            with Image.open(BytesIO(raw), formats=list(_FORMATS.values())) as source:
                source.load()
                with ImageOps.exif_transpose(source) as oriented:
                    with _srgb_rgba(oriented) as rgba:
                        clean = Image.new('RGBA', rgba.size)
                        clean.paste(rgba)
                        return clean
    except ImageValidationError:
        raise
    except (
        OSError,
        ValueError,
        SyntaxError,
        EOFError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise ImageValidationError('Invalid or unsafe image.') from exc


def _encode(image: Image.Image, max_bytes: int = MAX_IMAGE_BYTES) -> NormalizedImage:
    stream = BytesIO()
    image.save(stream, format='PNG')
    raw = stream.getvalue()
    if len(raw) > max_bytes:
        raise ImageValidationError('Normalized image byte limit exceeded.')
    return NormalizedImage(raw, 'image/png', image.width, image.height)


def normalize_image(
    raw: bytes,
    content_type: str,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> NormalizedImage:
    """Decode, orient and strip metadata without resizing or accepting animations."""
    with _decode(raw, content_type, max_bytes, max_pixels) as image:
        return _encode(image, max_bytes)


def validate_mask(
    raw: bytes,
    content_type: str,
    *,
    width: int,
    height: int,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> NormalizedImage:
    """White edits, black preserves; alpha and grey never imply a selection."""
    with _decode(raw, content_type, max_bytes, max_pixels) as image:
        if image.size != (width, height):
            raise ImageValidationError('Mask size must match the oriented image.')
        colors = image.getcolors(maxcolors=3)
        if colors is None or any(
            color not in ((0, 0, 0, 255), (255, 255, 255, 255)) for _, color in colors
        ):
            raise ImageValidationError('Mask must contain only opaque black or white pixels.')
        if not any(color == (255, 255, 255, 255) for _, color in colors):
            raise ImageValidationError('Mask must contain a nonempty white edit region.')
        return _encode(image, max_bytes)


def _uuid(value: UUID | str) -> str:
    try:
        normalized = str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise StoragePathError('Expected a canonical UUID.') from exc
    if str(value) != normalized:
        raise StoragePathError('Expected a canonical UUID.')
    return normalized


def _name(value: str) -> str:
    if not isinstance(value, str) or not value.endswith('.png'):
        raise StoragePathError('Expected a UUID PNG filename.')
    return f'{_uuid(value[:-4])}.png'


class NmmStorage:
    """POSIX descriptor-relative I/O prevents directory/file symlink traversal.

    Caller owns database compensation. put flushes the new file before returning;
    delete is idempotent for compensation and never follows a symbolic link.
    """

    def __init__(
        self,
        attachment_root: str | Path,
        *,
        max_bytes: int = MAX_IMAGE_BYTES,
        max_pixels: int = MAX_IMAGE_PIXELS,
    ):
        if any(type(limit) is not int or limit <= 0 for limit in (max_bytes, max_pixels)):
            raise ImageValidationError('Image limits must be positive integers.')
        self.max_bytes, self.max_pixels = max_bytes, max_pixels
        # The configured parent is trusted and may use macOS /tmp or /var aliases.
        # Resolve it once, but never resolve the independently protected nmm subtree.
        self.root = Path(attachment_root).absolute().parent.resolve() / 'nmm'

    @contextmanager
    def _directory(self, project: str, *, create: bool = False):
        descriptor = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            parts = (*self.root.parts[1:], project)
            for index, component in enumerate(parts):
                if create and index >= len(parts) - 2:
                    with suppress(FileExistsError):
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = child
            yield descriptor
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise StoragePathError('Storage path contains a link or non-directory.') from exc
            raise
        finally:
            os.close(descriptor)

    def put(self, project_id: UUID | str, image: NormalizedImage) -> StoredImage:
        project = _uuid(project_id)
        validated = normalize_image(
            image.data, image.content_type, max_bytes=self.max_bytes, max_pixels=self.max_pixels
        )
        if (image.width, image.height) != (validated.width, validated.height):
            raise ImageValidationError('Image dimensions do not match the encoded image.')
        name = f'{uuid4()}.png'
        with self._directory(project, create=True) as directory:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                mode=0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, 'wb') as target:
                    target.write(validated.data)
                    target.flush()
                    os.fsync(target.fileno())
                os.fsync(directory)
            except BaseException:
                os.unlink(name, dir_fd=directory)
                raise
        return StoredImage(
            name,
            sha256(validated.data).hexdigest(),
            validated.content_type,
            len(validated.data),
            validated.width,
            validated.height,
        )

    def read(self, project_id: UUID | str, name: str) -> bytes:
        project, name = _uuid(project_id), _name(name)
        with self._directory(project) as directory:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            with os.fdopen(descriptor, 'rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise StoragePathError('Stored image must be a regular single-link file.')
                data = source.read(self.max_bytes + 1)
                if len(data) > self.max_bytes:
                    raise ImageValidationError('Stored image byte limit exceeded.')
                return data

    def delete(self, project_id: UUID | str, name: str) -> None:
        project, name = _uuid(project_id), _name(name)
        try:
            with self._directory(project) as directory:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise StoragePathError('Stored image must be a regular single-link file.')
                os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
