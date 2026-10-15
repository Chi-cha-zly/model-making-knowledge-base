"""NMM capability contracts. Fake implementations require explicit injection.

No configuration or network clients are loaded here. A future real adapter must expose
usage separately when the contract grows; these byte/model returns do not imply billing.
"""

from collections import deque
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Protocol, TypeVar

from PIL import Image, ImageOps
from pydantic import BaseModel

from .storage import normalize_image, validate_mask

Model = TypeVar('Model', bound=BaseModel)


@dataclass(frozen=True)
class ImageInput:
    data: bytes
    content_type: str


class VisionProvider(Protocol):
    async def analyze(
        self, images: list[ImageInput], prompt: str, schema: type[Model]
    ) -> Model: ...


class ImageEditProvider(Protocol):
    async def edit(
        self, image: ImageInput, mask: ImageInput, references: list[ImageInput], prompt: str, n: int
    ) -> list[bytes]: ...


class ProviderUnconfiguredError(RuntimeError):
    pass


class UnconfiguredVisionProvider:
    async def analyze(self, images: list[ImageInput], prompt: str, schema: type[Model]) -> Model:
        raise ProviderUnconfiguredError('NMM vision provider is unconfigured.')


class UnconfiguredImageEditProvider:
    async def edit(
        self, image: ImageInput, mask: ImageInput, references: list[ImageInput], prompt: str, n: int
    ) -> list[bytes]:
        raise ProviderUnconfiguredError('NMM image edit provider is unconfigured.')


class FakeVisionProvider:
    """Consume explicit scripted outputs; invalid outputs raise Pydantic errors."""

    def __init__(self, outputs: Iterable[dict | BaseModel]):
        self._outputs = deque(deepcopy(list(outputs)))

    async def analyze(self, images: list[ImageInput], prompt: str, schema: type[Model]) -> Model:
        for image in images:
            normalize_image(image.data, image.content_type)
        if not self._outputs:
            raise RuntimeError('Fake vision script exhausted.')
        output = self._outputs.popleft()
        if isinstance(output, BaseModel):
            output = output.model_dump()
        return schema.model_validate(output)


class FakeImageEditProvider:
    """Deterministic local gradient only; this is not an NMM rendering algorithm."""

    async def edit(
        self, image: ImageInput, mask: ImageInput, references: list[ImageInput], prompt: str, n: int
    ) -> list[bytes]:
        if type(n) is not int or not 1 <= n <= 4:
            raise ValueError('Fake image output count must be between 1 and 4.')
        base = normalize_image(image.data, image.content_type)
        region = validate_mask(mask.data, mask.content_type, width=base.width, height=base.height)
        for reference in references:
            normalize_image(reference.data, reference.content_type)
        outputs = []
        with (
            Image.open(BytesIO(base.data)) as source,
            Image.open(BytesIO(region.data)) as selection,
        ):
            with selection.convert('L') as binary, Image.linear_gradient('L') as ramp:
                with ramp.resize(source.size) as gradient:
                    for index in range(n):
                        seed = sha256(f'{prompt}\0{index}'.encode()).digest()
                        low = tuple(24 + value % 80 for value in seed[:3])
                        high = tuple(176 + value % 80 for value in seed[3:6])
                        with ImageOps.colorize(gradient, low, high) as rgb:
                            with rgb.convert('RGBA') as colored:
                                with Image.composite(colored, source, binary) as edited:
                                    stream = BytesIO()
                                    edited.save(stream, 'PNG')
                                    outputs.append(stream.getvalue())
        return outputs
