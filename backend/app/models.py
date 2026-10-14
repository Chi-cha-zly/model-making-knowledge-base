from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Table, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


technique_sources = Table(
    'technique_sources',
    Base.metadata,
    Column('technique_id', ForeignKey('techniques.id', ondelete='CASCADE'), primary_key=True),
    Column('source_id', ForeignKey('sources.id', ondelete='RESTRICT'), primary_key=True),
)
product_sources = Table(
    'product_sources',
    Base.metadata,
    Column('product_id', ForeignKey('products.id', ondelete='CASCADE'), primary_key=True),
    Column('source_id', ForeignKey('sources.id', ondelete='RESTRICT'), primary_key=True),
)
technique_attachments = Table(
    'technique_attachments',
    Base.metadata,
    Column('technique_id', ForeignKey('techniques.id', ondelete='CASCADE'), primary_key=True),
    Column('attachment_id', ForeignKey('attachments.id', ondelete='RESTRICT'), primary_key=True),
)
product_attachments = Table(
    'product_attachments',
    Base.metadata,
    Column('product_id', ForeignKey('products.id', ondelete='CASCADE'), primary_key=True),
    Column('attachment_id', ForeignKey('attachments.id', ondelete='RESTRICT'), primary_key=True),
)


def now():
    return datetime.now(timezone.utc)


class Source(Base):
    __tablename__ = 'sources'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(30), default='link')
    author: Mapped[str | None] = mapped_column(String(300), nullable=True)
    license_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_id: Mapped[str] = mapped_column(String(64), default='local')
    visibility: Mapped[str] = mapped_column(String(20), default='private')
    status: Mapped[str] = mapped_column(String(20), default='published')


class Technique(Base):
    __tablename__ = 'techniques'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_blocks: Mapped[list] = mapped_column(
        JSON, default=list, nullable=False, server_default='[]'
    )
    prerequisites: Mapped[str | None] = mapped_column(Text, nullable=True)
    risks: Mapped[str | None] = mapped_column(Text, nullable=True)
    difficulty: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    owner_id: Mapped[str] = mapped_column(String(64), default='local')
    visibility: Mapped[str] = mapped_column(String(20), default='private')
    status: Mapped[str] = mapped_column(String(20), default='published')
    sources: Mapped[list[Source]] = relationship(secondary=technique_sources)
    attachments: Mapped[list['Attachment']] = relationship(secondary=technique_attachments)


class Product(Base):
    __tablename__ = 'products'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    brand: Mapped[str | None] = mapped_column(String(200), nullable=True)
    name: Mapped[str] = mapped_column(String(300))
    sku: Mapped[str | None] = mapped_column(String(150), nullable=True)
    sku_key: Mapped[str | None] = mapped_column(String(150), unique=True, nullable=True)
    kind: Mapped[str | None] = mapped_column(String(100), nullable=True)
    properties: Mapped[dict] = mapped_column(JSON, default=dict)
    manufacturer_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[str] = mapped_column(String(64), default='local')
    visibility: Mapped[str] = mapped_column(String(20), default='private')
    status: Mapped[str] = mapped_column(String(20), default='published')
    sources: Mapped[list[Source]] = relationship(secondary=product_sources)
    attachments: Mapped[list['Attachment']] = relationship(secondary=product_attachments)
    aliases: Mapped[list['ProductAlias']] = relationship(
        cascade='all, delete-orphan', back_populates='product'
    )


class ProductAlias(Base):
    __tablename__ = 'product_aliases'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id', ondelete='CASCADE'))
    alias: Mapped[str] = mapped_column(String(300))
    alias_key: Mapped[str] = mapped_column(String(500), unique=True)
    product: Mapped[Product] = relationship(back_populates='aliases')


class Attachment(Base):
    __tablename__ = 'attachments'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(300))
    content_type: Mapped[str] = mapped_column(String(40))
    storage_name: Mapped[str] = mapped_column(String(64), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    owner_id: Mapped[str] = mapped_column(String(64), default='local')
    visibility: Mapped[str] = mapped_column(String(20), default='private')
    status: Mapped[str] = mapped_column(String(20), default='published')
