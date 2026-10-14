"""One-process asyncio workers with durable, idempotent job lifecycle.

No worker is launched by importing this module. Work is explicitly submitted;
restart marks interrupted jobs stopped rather than silently repeating model calls.
"""

import asyncio
from contextlib import suppress
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from .ingestion import digest
from .job_models import Job
from .models import now

ACTIVE = ('queued', 'running')


def snapshot(row):
    return {
        column.name: (value.isoformat() if hasattr(value, 'isoformat') else value)
        for column in row.__table__.columns
        if column.name != 'request_hash'
        for value in [getattr(row, column.name)]
    }


class JobContext:
    def __init__(self, service, id):
        self.service, self.id = service, id

    def progress(self, completed, total):
        if not 0 <= completed <= total:
            raise ValueError('Invalid progress')
        self.service.update(self.id, progress=completed, total=total)


class JobService:
    def __init__(self, engine):
        self.engine = engine
        self.tasks = {}
        self.cancel_callbacks = {}
        self.lock = asyncio.Lock()

    def snapshot(self, id):
        with Session(self.engine) as db:
            row = db.get(Job, id)
            if row is None:
                raise HTTPException(404, '任务不存在。')
            return snapshot(row)

    def history(self, kind=None):
        with Session(self.engine) as db:
            query = select(Job).order_by(Job.id.desc()).limit(100)
            if kind:
                query = query.where(Job.kind == kind)
            return [snapshot(row) for row in db.scalars(query)]

    def update(self, id, **fields):
        with Session(self.engine) as db:
            row = db.get(Job, id)
            if row is not None and row.status in ACTIVE:
                for name, value in fields.items():
                    setattr(row, name, value)
                row.updated_at = now()
                db.commit()

    def recover(self):
        with Session(self.engine) as db:
            for row in db.scalars(select(Job).where(Job.status.in_(ACTIVE))):
                row.status = 'stopped'
                row.error = '服务重启，任务已中断；已完成的数据保留，请显式重试。'
                row.updated_at = now()
            db.commit()

    async def submit(self, kind, request_id, parameters, runner, *, on_cancel=None):
        async with self.lock:
            fingerprint = digest(parameters)
            with Session(self.engine) as db:
                existing = db.scalar(
                    select(Job).where(Job.kind == kind, Job.request_id == request_id)
                )
                if existing is not None:
                    if existing.request_hash != fingerprint:
                        raise HTTPException(409, 'request_id 已用于不同任务参数。')
                    return snapshot(existing)
                row = Job(kind=kind, request_id=request_id, request_hash=fingerprint)
                db.add(row)
                db.commit()
                output = snapshot(row)
            if on_cancel is not None:
                self.cancel_callbacks[output['id']] = on_cancel
            task = asyncio.create_task(self._run(output['id'], runner))
            self.tasks[output['id']] = task
            return output

    async def _run(self, id, runner):
        try:
            self.update(id, status='running')
            result = await runner(JobContext(self, id))
            status = (result or {}).get('status', 'completed')
            if status not in ('completed', 'failed', 'stopped'):
                status = 'failed'
            self.update(
                id,
                status=status,
                result=result,
                error=None if status == 'completed' else '任务未完成，请检查结果与模型配置。',
            )
        except asyncio.CancelledError:
            callback = self.cancel_callbacks.get(id)
            if callback is not None:
                callback()
            self.update(id, status='stopped', error='任务已停止，已完成的数据保留。')
            raise
        except Exception:
            self.update(id, status='failed', error='任务失败，请检查配置和输入；已完成的数据保留。')
        finally:
            self.tasks.pop(id, None)
            self.cancel_callbacks.pop(id, None)

    async def cancel(self, id):
        current = self.snapshot(id)
        callback = self.cancel_callbacks.get(id)
        task = self.tasks.get(id)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.tasks.pop(id, None)
        if current['status'] in ACTIVE and callback is not None:
            callback()
        self.cancel_callbacks.pop(id, None)
        self.update(id, status='stopped', error='任务已停止，已完成的数据保留。')
        return self.snapshot(id)

    async def shutdown(self):
        for id in list(self.tasks):
            await self.cancel(id)
