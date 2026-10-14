"""Persisted chat history and generation attempts; independent of catalog rows."""
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base, now


class Conversation(Base):
    __tablename__ = 'conversations'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Message(Base):
    __tablename__ = 'messages'
    __table_args__ = (CheckConstraint("role IN ('user','assistant')"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey('conversations.id', ondelete='CASCADE'), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Run(Base):
    __tablename__ = 'runs'
    __table_args__ = (
        UniqueConstraint('conversation_id', 'request_id'),
        CheckConstraint("status IN ('queued','running','completed','stopped','failed')"),
        Index('one_active_run_per_conversation', 'conversation_id', unique=True,
              postgresql_where=text("status IN ('queued','running')")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey('conversations.id', ondelete='CASCADE'), index=True)
    user_message_id: Mapped[int] = mapped_column(ForeignKey('messages.id', ondelete='CASCADE'))
    assistant_message_id: Mapped[int] = mapped_column(ForeignKey('messages.id', ondelete='CASCADE'), unique=True)
    request_id: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default='queued')
    content: Mapped[str] = mapped_column(Text, default='')
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
