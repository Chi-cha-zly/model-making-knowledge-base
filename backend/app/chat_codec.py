"""Chat archive validation independent of HTTP and generation."""

from datetime import datetime
import json
import re
from .chat_models import Conversation, Message, Run
from .repositories.chat import ACTIVE


def validate_chat_backup(data):
    """Validate IDs, roles and turn ownership before replacing any persisted records."""
    if data['version'] == 1:
        return  # Legacy catalog-only backups preserve current chat history.
    maps = {}
    for kind, model in (('conversations', Conversation), ('messages', Message), ('runs', Run)):
        rows = data[kind]
        if not isinstance(rows, list):
            raise ValueError('Invalid chat table')
        mapped = {}
        columns = {c.name for c in model.__table__.columns}
        for row in rows:
            if not isinstance(row, dict) or set(row) != columns:
                raise ValueError('Invalid chat fields')
            if type(row['id']) is not int or row['id'] <= 0 or row['id'] in mapped:
                raise ValueError('Invalid or duplicate chat ID')
            for key in ('created_at', 'updated_at'):
                if key in columns:
                    datetime.fromisoformat(row[key])
            mapped[row['id']] = row
        maps[kind] = mapped
    for conv in maps['conversations'].values():
        if not isinstance(conv['title'], str) or not 1 <= len(conv['title']) <= 300:
            raise ValueError('Invalid conversation title')
    for msg in maps['messages'].values():
        if (
            msg['conversation_id'] not in maps['conversations']
            or msg['role'] not in ('user', 'assistant')
            or not isinstance(msg['content'], str)
        ):
            raise ValueError('Invalid chat message')
        if len(msg['content']) > (16000 if msg['role'] == 'user' else 100000):
            raise ValueError('Chat message exceeds size limit')
    requests, assistants = set(), set()
    for run in maps['runs'].values():
        if run['conversation_id'] not in maps['conversations'] or run['status'] not in (
            *ACTIVE,
            'stopped',
            'completed',
            'failed',
        ):
            raise ValueError('Invalid run')
        if (
            not isinstance(run['request_id'], str)
            or not 1 <= len(run['request_id']) <= 128
            or not re.fullmatch('[0-9a-f]{64}', run['request_hash'])
        ):
            raise ValueError('Invalid run request')
        request = (run['conversation_id'], run['request_id'])
        if request in requests or run['assistant_message_id'] in assistants:
            raise ValueError('Duplicate run request or assistant')
        requests.add(request)
        assistants.add(run['assistant_message_id'])
        for field, role in (('user_message_id', 'user'), ('assistant_message_id', 'assistant')):
            msg = maps['messages'].get(run[field])
            if not msg or msg['role'] != role or msg['conversation_id'] != run['conversation_id']:
                raise ValueError('Broken run message reference')
        if run['content'] != maps['messages'][run['assistant_message_id']][
            'content'
        ] or not isinstance(run['citations'], list):
            raise ValueError('Invalid run content')
        if run['error'] is not None and not isinstance(run['error'], str):
            raise ValueError('Invalid run error')
        if len(json.dumps(run['citations'], ensure_ascii=False)) > 25000:
            raise ValueError('Citation context exceeds size limit')
        for citation in run['citations']:
            if (
                not isinstance(citation, dict)
                or citation.get('type') not in ('technique', 'product')
                or type(citation.get('id')) is not int
            ):
                raise ValueError('Invalid citation')
            if (
                not isinstance(citation.get('title'), str)
                or not isinstance(citation.get('excerpt'), str)
                or not isinstance(citation.get('sources'), list)
                or not isinstance(citation.get('source_ids'), list)
            ):
                raise ValueError('Invalid citation fields')
            if citation['source_ids'] != [
                s.get('id') for s in citation['sources'] if isinstance(s, dict)
            ]:
                raise ValueError('Invalid citation sources')
            image_ids = citation.get('attachment_ids', [])
            if (
                not isinstance(image_ids, list)
                or len(image_ids) > 6
                or any(type(i) is not int or i <= 0 for i in image_ids)
            ):
                raise ValueError('Invalid citation images')
            for source in citation['sources']:
                if (
                    not isinstance(source, dict)
                    or type(source.get('id')) is not int
                    or not isinstance(source.get('title'), str)
                    or (source.get('url') is not None and not isinstance(source.get('url'), str))
                ):
                    raise ValueError('Invalid citation source')
