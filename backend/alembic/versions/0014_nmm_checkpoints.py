"""Durable NMM checkpoints, frozen from langgraph-checkpoint-postgres 3.2.0.

SDK migrations 0..9 are represented by their final schema here. Indexes are
created transactionally (not CONCURRENTLY); runtime never calls saver.setup().
"""

from alembic import op

revision = '0014_nmm_checkpoints'
down_revision = '0013_nmm_foundation'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('CREATE TABLE checkpoint_migrations (v INTEGER PRIMARY KEY)')
    op.execute("""CREATE TABLE checkpoints (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL,
        parent_checkpoint_id TEXT,
        type TEXT,
        checkpoint JSONB NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}',
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
    )""")
    op.execute("""CREATE TABLE checkpoint_blobs (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL,
        version TEXT NOT NULL,
        type TEXT NOT NULL,
        blob BYTEA,
        PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
    )""")
    op.execute("""CREATE TABLE checkpoint_writes (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        channel TEXT NOT NULL,
        type TEXT,
        blob BYTEA NOT NULL,
        task_path TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
    )""")
    op.execute('CREATE INDEX checkpoints_thread_id_idx ON checkpoints(thread_id)')
    op.execute('CREATE INDEX checkpoint_blobs_thread_id_idx ON checkpoint_blobs(thread_id)')
    op.execute('CREATE INDEX checkpoint_writes_thread_id_idx ON checkpoint_writes(thread_id)')
    op.execute('INSERT INTO checkpoint_migrations(v) SELECT generate_series(0, 9)')


def downgrade():
    op.execute('DROP TABLE checkpoint_writes')
    op.execute('DROP TABLE checkpoint_blobs')
    op.execute('DROP TABLE checkpoints')
    op.execute('DROP TABLE checkpoint_migrations')
