"""Explicit offline NMM providers and synthetic PNG inputs, never runtime defaults."""

import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
import json

from PIL import Image
from langgraph.checkpoint.memory import InMemorySaver

from app.nmm.providers import FakeImageEditProvider, ImageInput


def make_png(color=(80, 90, 100), size=(16, 16)):
    image = Image.new('RGB', size, color)
    output = BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()


def make_inputs(size=(16, 16)):
    return {
        'original': ImageInput(make_png(size=size), 'image/png'),
        'mask': ImageInput(make_png((255, 255, 255), size), 'image/png'),
    }


class OfflineVision:
    def __init__(self, *, bad_plans=0, review_warnings=None, fail=False):
        self.bad_plans = bad_plans
        self.review_warnings = review_warnings or ['offline review warning']
        self.fail = fail
        self.calls = []

    async def analyze(self, images, prompt, schema):
        request = json.loads(prompt)
        self.calls.append((request['task'], len(images)))
        if self.fail:
            raise RuntimeError('secret-provider-error-do-not-expose')
        if request['task'] == 'analysis':
            value = {'summary': 'offline miniature analysis', 'warnings': []}
        elif request['task'] == 'plan':
            if self.bad_plans:
                self.bad_plans -= 1
                return schema.model_validate({'steps': []})
            value = {
                'steps': [
                    dict(
                        step,
                        title=f'Paint {step["index"]}',
                        color_description='Shown offline color',
                        areas='Shown operation region',
                        technique='Thin layers',
                        expected_result='Match the shown layer',
                        change_from_previous='Paint the highlighted operation',
                        common_mistakes='Painting outside the mask',
                        needs_confirmation=[],
                    )
                    for step in request['steps']
                ],
                'overall_tips': ['Keep the shown lighting consistent.'],
            }
        else:
            value = {
                'issues': [
                    {'step': 1, 'type': 'other', 'detail': warning, 'severity': 'low'}
                    for warning in self.review_warnings
                ],
                'summary': 'Offline review.',
            }
        return schema.model_validate(value)


class RecordingImageEdit(FakeImageEditProvider):
    def __init__(self, *, block=False, fail=False):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block, self.fail = block, fail

    async def edit(self, image, mask, references, prompt, n):
        self.calls.append(len(references))
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.fail:
            raise RuntimeError('secret-image-error-do-not-expose')
        return await super().edit(image, mask, references, prompt, n)


class MemoryCheckpointStore:
    def __init__(self):
        self.saver = InMemorySaver()

    @asynccontextmanager
    async def open(self):
        yield self.saver


async def wait_job(jobs, job):
    task = jobs.tasks.get(job['id'])
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=30)
    return jobs.snapshot(job['id'])
