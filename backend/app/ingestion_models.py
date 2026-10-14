"""Versioned derived knowledge and a portable, indexed lexical posting list."""
from sqlalchemy import ForeignKey, Integer, JSON, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base
from pgvector.sqlalchemy import Vector


class Document(Base):
    __tablename__ = 'documents'
    id: Mapped[int] = mapped_column(primary_key=True)
    record_key: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    record_id: Mapped[int] = mapped_column(Integer)
    active_version: Mapped[int] = mapped_column(default=0)
    fingerprint: Mapped[str] = mapped_column(String(64), default='')
    status: Mapped[str] = mapped_column(String(20), default='pending')


class Chunk(Base):
    __tablename__ = 'chunks'
    __table_args__ = (UniqueConstraint('document_id', 'version', 'ordinal'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey('documents.id', ondelete='CASCADE'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default='text')
    content: Mapped[str] = mapped_column(Text)
    attachment_id: Mapped[int | None] = mapped_column(ForeignKey('attachments.id', ondelete='CASCADE'), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)


class Embedding(Base):
    __tablename__ = 'embeddings'
    id: Mapped[int] = mapped_column(primary_key=True)
    chunk_id: Mapped[int] = mapped_column(ForeignKey('chunks.id', ondelete='CASCADE'), unique=True)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vector: Mapped[list | None] = mapped_column(Vector(), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))


class IngestionJob(Base):
    __tablename__ = 'ingestion_jobs'
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey('documents.id', ondelete='CASCADE'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default='running')
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Posting(Base):
    __tablename__ = 'chunk_postings'
    __table_args__ = (Index('ix_chunk_postings_term_chunk', 'term', 'chunk_id'),)
    chunk_id: Mapped[int] = mapped_column(ForeignKey('chunks.id', ondelete='CASCADE'), primary_key=True)
    term: Mapped[str] = mapped_column(String(200), primary_key=True)


class DocumentSource(Base):
    """Provenance belongs to the activated version, not the latest mutable row."""
    __tablename__ = 'document_sources'
    document_id: Mapped[int] = mapped_column(ForeignKey('documents.id', ondelete='CASCADE'), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey('sources.id', ondelete='RESTRICT'), primary_key=True)
