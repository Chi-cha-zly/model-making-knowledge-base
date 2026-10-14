"""Persistent conversations, messages and generation attempts.

Revision ID: 0003_agent_chat
Revises: 0002_attachment_links
"""

from alembic import op
import sqlalchemy as sa

revision = '0003_agent_chat'
down_revision = '0002_attachment_links'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'conversations',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('title', sa.String(300), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'messages',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column(
            'conversation_id',
            sa.Integer,
            sa.ForeignKey('conversations.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('role', sa.String(20), nullable=False),
        sa.Column('content', sa.Text, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('user','assistant')"),
    )
    op.create_index('ix_messages_conversation_id', 'messages', ['conversation_id'])
    op.create_table(
        'runs',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column(
            'conversation_id',
            sa.Integer,
            sa.ForeignKey('conversations.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'user_message_id',
            sa.Integer,
            sa.ForeignKey('messages.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'assistant_message_id',
            sa.Integer,
            sa.ForeignKey('messages.id', ondelete='CASCADE'),
            nullable=False,
            unique=True,
        ),
        sa.Column('request_id', sa.String(128), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('content', sa.Text, nullable=False),
        sa.Column('error', sa.Text, nullable=True),
        sa.Column('citations', sa.JSON, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('conversation_id', 'request_id'),
        sa.CheckConstraint("status IN ('queued','running','completed','stopped','failed')"),
    )
    op.create_index('ix_runs_conversation_id', 'runs', ['conversation_id'])
    op.create_index(
        'one_active_run_per_conversation',
        'runs',
        ['conversation_id'],
        unique=True,
        sqlite_where=sa.text("status IN ('queued','running')"),
        postgresql_where=sa.text("status IN ('queued','running')"),
    )


def downgrade():
    op.drop_table('runs')
    op.drop_table('messages')
    op.drop_table('conversations')
