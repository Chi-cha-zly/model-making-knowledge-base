"""Short, synchronous transactions for conversations and persisted run state."""

from datetime import datetime
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from ..chat_models import Conversation, Message, Run
from ..models import now
from ..services.errors import ServiceError

ACTIVE = ('queued', 'running')


def record(row, internal=False):
    fields = [c.name for c in row.__table__.columns]
    if isinstance(row, Run) and not internal:
        fields = [f for f in fields if f not in ('request_id', 'request_hash')]
    return {
        f: getattr(row, f).isoformat() if isinstance(getattr(row, f), datetime) else getattr(row, f)
        for f in fields
    }


def get(db, model, id):
    row = db.get(model, id)
    if row is None:
        raise ServiceError(404, 'Not found')
    return row


class ChatRepository:
    def __init__(self, engine):
        self.engine = engine

    def evidence(self, query):
        with Session(self.engine) as db:
            return evidence(db, query)

    def conversations(self):
        with Session(self.engine) as db:
            return [
                record(c)
                for c in db.scalars(
                    select(Conversation).order_by(
                        Conversation.updated_at.desc(), Conversation.id.desc()
                    )
                )
            ]

    def create_conversation(self, title):
        with Session(self.engine) as db:
            row = Conversation(title=title)
            db.add(row)
            db.commit()
            return record(row)

    def detail(self, cid):
        with Session(self.engine) as db:
            return {
                **record(get(db, Conversation, cid)),
                'messages': [
                    record(x)
                    for x in db.scalars(
                        select(Message).where(Message.conversation_id == cid).order_by(Message.id)
                    )
                ],
                'runs': [
                    record(x)
                    for x in db.scalars(
                        select(Run).where(Run.conversation_id == cid).order_by(Run.id)
                    )
                ],
            }

    def rename(self, cid, title):
        with Session(self.engine) as db:
            row = get(db, Conversation, cid)
            row.title, row.updated_at = title, now()
            db.commit()
            return record(row)

    def active_ids(self, cid):
        with Session(self.engine) as db:
            get(db, Conversation, cid)
            return list(
                db.scalars(select(Run.id).where(Run.conversation_id == cid, Run.status.in_(ACTIVE)))
            )

    def remove(self, cid):
        with Session(self.engine) as db:
            for model in (Run, Message, Conversation):
                db.execute(
                    delete(model).where(
                        model.id == cid if model is Conversation else model.conversation_id == cid
                    )
                )
            db.commit()

    def recover(self):
        with Session(self.engine) as db:
            for run in db.scalars(select(Run).where(Run.status.in_(ACTIVE))):
                run.status = 'stopped'
                run.error = '服务重启中断了生成；已保留部分文本，可重试或补充要求。'
                run.updated_at = now()
            db.commit()

    def snapshot(self, rid):
        with Session(self.engine) as db:
            return record(get(db, Run, rid))

    def has_active(self):
        with Session(self.engine) as db:
            return db.scalar(select(Run.id).where(Run.status.in_(ACTIVE)).limit(1)) is not None

    def stop(self, rid, reason):
        with Session(self.engine) as db:
            run = get(db, Run, rid)
            if run.status in ACTIVE:
                run.status, run.error, run.updated_at = 'stopped', reason, now()
                db.commit()
            return record(run)

    @staticmethod
    def history_turns(db, run):
        turns = []
        users = db.scalars(
            select(Message)
            .where(
                Message.conversation_id == run.conversation_id,
                Message.role == 'user',
                Message.id <= run.user_message_id,
            )
            .order_by(Message.id.desc())
            .limit(20)
        ).all()
        for user in users:
            turn = [{'role': 'user', 'content': user.content}]
            if user.id != run.user_message_id:
                prior = db.scalar(
                    select(Run)
                    .where(Run.user_message_id == user.id, Run.status == 'completed')
                    .order_by(Run.id.desc())
                    .limit(1)
                )
                if prior and prior.content:
                    turn.append({'role': 'assistant', 'content': prior.content[:24000]})
            turns.append(turn)
        return turns

    def begin_generation(self, rid, citations=None):
        with Session(self.engine) as db:
            run = get(db, Run, rid)
            if citations is not None:
                run.citations = citations
            run.status, run.updated_at = 'running', now()
            turns = self.history_turns(db, run)
            db.commit()
            return record(run), turns

    def flush_content(self, rid, content):
        with Session(self.engine) as db:
            run = get(db, Run, rid)
            if run.status not in ACTIVE:
                return False
            run.content, run.updated_at = content, now()
            get(db, Message, run.assistant_message_id).content = content
            get(db, Conversation, run.conversation_id).updated_at = now()
            db.commit()
        return True

    def finish(self, rid, error=None):
        with Session(self.engine) as db:
            run = db.get(Run, rid)
            if run and run.status in ACTIVE:
                run.status = (
                    'failed' if error is not None or not run.content.strip() else 'completed'
                )
                run.error = (
                    error
                    if error is not None
                    else None
                    if run.content.strip()
                    else '模型未返回文本，请重试。'
                )
                run.updated_at = now()
                db.commit()

    def request_state(self, cid, request_id, prior_id=None):
        with Session(self.engine) as db:
            get(db, Conversation, cid)
            duplicate = db.scalar(
                select(Run).where(Run.conversation_id == cid, Run.request_id == request_id)
            )
            latest = db.scalar(
                select(Run).where(Run.conversation_id == cid).order_by(Run.id.desc()).limit(1)
            )
            prior_content = (
                get(db, Message, get(db, Run, prior_id).user_message_id).content
                if prior_id
                else None
            )
            recent = list(
                db.scalars(
                    select(Message.content)
                    .where(Message.conversation_id == cid, Message.role == 'user')
                    .order_by(Message.id.desc())
                    .limit(4)
                )
            )
            return {
                'duplicate': record(duplicate, internal=True) if duplicate else None,
                'latest': record(latest) if latest else None,
                'prior_content': prior_content,
                'recent': recent,
            }

    def create_run(self, cid, data, action, prior_id, fingerprint, citations):
        with Session(self.engine) as db:
            conv = get(db, Conversation, cid)
            if action == 'retry':
                user = get(db, Message, get(db, Run, prior_id).user_message_id)
            else:
                user = Message(conversation_id=cid, role='user', content=data.content)
                db.add(user)
                db.flush()
            assistant = Message(conversation_id=cid, role='assistant', content='')
            db.add(assistant)
            db.flush()
            run = Run(
                conversation_id=cid,
                user_message_id=user.id,
                assistant_message_id=assistant.id,
                request_id=data.request_id,
                request_hash=fingerprint,
                status='queued',
                content='',
                citations=citations,
            )
            db.add(run)
            conv.updated_at = now()
            if conv.title == '新对话':
                conv.title = user.content[:60]
            db.commit()
            return record(run)


def export_chat(db):
    data = {
        name: [record(row, internal=True) for row in db.scalars(select(model).order_by(model.id))]
        for name, model in (('conversations', Conversation), ('messages', Message), ('runs', Run))
    }
    for run in data['runs']:
        if run['status'] in ACTIVE:
            run['status'] = 'stopped'
            run['error'] = '备份时生成尚未结束；已保存当前文本，可重试。'
    return data


def restore_chat(db, data):
    if data['version'] == 1:
        return
    for model in (Run, Message, Conversation):
        db.execute(delete(model))
    for kind, model in (('conversations', Conversation), ('messages', Message), ('runs', Run)):
        for source in data[kind]:
            row = dict(source)
            for key in ('created_at', 'updated_at'):
                if key in row:
                    row[key] = datetime.fromisoformat(row[key])
            if model is Run and row['status'] in ACTIVE:
                row['status'] = 'stopped'
                row['error'] = '从备份恢复了未完成的生成；可重试或补充要求。'
            db.add(model(**row))
        db.flush()


def evidence(db, query):
    from ..retrieval import search_documents
    from ..chat_context import bounded_evidence

    return bounded_evidence(search_documents(db, query, k=6))
