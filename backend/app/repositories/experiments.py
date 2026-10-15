"""Persist experiment snapshots and durable links to shared jobs."""

from copy import deepcopy
from sqlalchemy import String, cast, delete, select
from sqlalchemy.orm import Session
from ..experiment_models import ExperimentJob
from ..job_models import Job
from ..services.errors import ServiceError
from ..experiment_metrics import now

ACTIVE = ('queued', 'running')


def _job_links_query():
    # Shared jobs store str(ExperimentJob.id); arbitrary legacy request IDs are not links.
    return (
        select(ExperimentJob.id, Job.id)
        .select_from(ExperimentJob)
        .join(Job, Job.request_id == cast(ExperimentJob.id, String))
        .where(Job.kind == 'experiment')
    )


class ExperimentRepository:
    def __init__(self, engine):
        self.engine = engine

    def job_links(self):
        with Session(self.engine) as db:
            return dict(db.execute(_job_links_query()).all())

    def job_id(self, id):
        with Session(self.engine) as db:
            return db.scalar(
                _job_links_query().with_only_columns(Job.id).where(ExperimentJob.id == id)
            )

    def history(self):
        with Session(self.engine) as db:
            return [
                deepcopy(r.payload)
                for r in db.scalars(
                    select(ExperimentJob).order_by(ExperimentJob.id.desc()).limit(50)
                )
            ]

    def snapshot(self, id):
        with Session(self.engine) as db:
            row = db.get(ExperimentJob, id)
            if row is None:
                raise ServiceError(404, '实验不存在。')
            return deepcopy(row.payload)

    def frozen(self, id):
        with Session(self.engine) as db:
            return deepcopy(db.get(ExperimentJob, id).snapshot)

    def update(self, id, fields, stamp):
        with Session(self.engine) as db:
            row = db.get(ExperimentJob, id)
            row.payload = {**row.payload, **fields, 'updated_at': stamp}
            db.commit()

    def existing(self, request_id):
        with Session(self.engine) as db:
            row = db.scalar(select(ExperimentJob).where(ExperimentJob.request_id == request_id))
            return (row.request_hash, deepcopy(row.payload)) if row else None

    def dataset_documents(self, dataset_id):
        from .evaluation import dataset as load_dataset
        from ..evaluation_demo import demo_fixture
        from ..retrieval import load_documents

        with Session(self.engine) as db:
            dataset = load_dataset(db, dataset_id)
            documents = demo_fixture()[1] if dataset['corpus'] == 'demo' else load_documents(db)
            return dataset, documents

    def create(self, request_id, digest, payload, snapshot):
        with Session(self.engine) as db:
            row = ExperimentJob(
                request_id=request_id, request_hash=digest, payload=payload, snapshot=snapshot
            )
            db.add(row)
            db.flush()
            row.payload = {**payload, 'id': row.id}
            db.commit()
            return deepcopy(row.payload)

    def recover(self, stamp):
        with Session(self.engine) as db:
            for row in db.scalars(select(ExperimentJob)):
                if row.payload['status'] in ('queued', 'running'):
                    row.payload = {
                        **row.payload,
                        'status': 'stopped',
                        'error': '服务重启，实验已中断；云端可能保留部分记录。',
                        'updated_at': stamp,
                    }
            db.commit()


def export_experiments(db):
    return {
        'experiments': [
            {
                'request_id': r.request_id,
                'request_hash': r.request_hash,
                'payload': r.payload,
                'snapshot': r.snapshot,
            }
            for r in db.scalars(select(ExperimentJob).order_by(ExperimentJob.id))
        ]
    }


def restore_experiments(db, data):
    if data['version'] < 4:
        return
    db.execute(delete(ExperimentJob))
    for r in data['experiments']:
        p = deepcopy(r['payload'])
        if p['status'] in ACTIVE:
            p.update(status='stopped', error='备份中的实验已中断。', updated_at=now())
        db.add(
            ExperimentJob(
                id=p['id'],
                request_id=r['request_id'],
                request_hash=r['request_hash'],
                payload=p,
                snapshot=deepcopy(r['snapshot']),
            )
        )
