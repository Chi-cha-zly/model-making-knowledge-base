"""Short-lived PostgreSQL graph persistence; schema is managed by Alembic only."""

from contextlib import asynccontextmanager
from uuid import UUID

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from sqlalchemy import text

CHECKPOINT_TABLES = frozenset(
    ('checkpoints', 'checkpoint_blobs', 'checkpoint_writes', 'checkpoint_migrations')
)


class PostgresCheckpointStore:
    def __init__(self, engine):
        if engine.dialect.name != 'postgresql':
            raise ValueError('PostgreSQL is required for NMM checkpoints')
        self.engine = engine

    @asynccontextmanager
    async def open(self):
        # Preserve driver options, especially fixture-specific schema search_path.
        args, options = self.engine.dialect.create_connect_args(self.engine.url)
        options.update(autocommit=True, prepare_threshold=0, row_factory=dict_row)
        async with await AsyncConnection.connect(*args, **options) as connection:
            cursor = await connection.execute(
                """SELECT count(*) AS count FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema() AND c.relkind = 'r'
                AND c.relname = ANY(%s)""",
                (list(CHECKPOINT_TABLES),),
            )
            row = await cursor.fetchone()
            if row['count'] != len(CHECKPOINT_TABLES):
                raise RuntimeError('NMM checkpoint schema is missing; run alembic upgrade head')
            cursor = await connection.execute('SELECT max(v) AS version FROM checkpoint_migrations')
            if (await cursor.fetchone())['version'] != 9:
                raise RuntimeError('NMM checkpoint schema version is incompatible')
            yield AsyncPostgresSaver(connection, serde=JsonPlusSerializer(pickle_fallback=False))


def delete_project_checkpoints(session, project_id):
    """Caller owns the transaction; only the validated project's NMM threads match."""
    prefix = f'nmm:{UUID(str(project_id))}:%'
    for table in ('checkpoint_writes', 'checkpoint_blobs', 'checkpoints'):
        session.execute(
            text(f'DELETE FROM {table} WHERE thread_id LIKE :prefix'), {'prefix': prefix}
        )
