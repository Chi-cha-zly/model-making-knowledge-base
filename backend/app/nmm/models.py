from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Index,
    text,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, now
from .schemas import AssetRole


class NmmProject(Base):
    __tablename__ = 'nmm_projects'
    __table_args__ = (
        CheckConstraint('revision >= 1'),
        CheckConstraint('generation_count >= 0'),
        CheckConstraint(
            "status IN ('draft','generating','awaiting_confirmation','planning',"
            "'rendering','done','failed','stopped')"
        ),
        Index('ix_nmm_projects_status_updated_at', 'status', 'updated_at'),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(40), default='draft')
    phase: Mapped[str] = mapped_column(String(40), default='input')
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    input_snapshots: Mapped[dict] = mapped_column(JSON, default=dict)
    analysis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    selected_candidate_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            'nmm_assets.id', name='fk_nmm_selected_candidate', ondelete='SET NULL', use_alter=True
        ),
        nullable=True,
    )
    workflow_thread_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    active_job_id: Mapped[int | None] = mapped_column(
        ForeignKey('jobs.id', ondelete='SET NULL'), nullable=True
    )
    generation_count: Mapped[int] = mapped_column(Integer, default=0)
    execution: Mapped[dict] = mapped_column(JSON, default=dict)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class NmmAsset(Base):
    __tablename__ = 'nmm_assets'
    __table_args__ = (
        Index(
            'ix_nmm_assets_project_revision_role_sort',
            'project_id',
            'revision',
            'role',
            'sort_order',
        ),
        Index(
            'uq_nmm_assets_input_role',
            'project_id',
            'revision',
            'role',
            unique=True,
            postgresql_where=text("role IN ('original', 'mask')"),
        ),
        CheckConstraint('revision >= 1'),
        CheckConstraint('size > 0 AND width > 0 AND height > 0'),
        CheckConstraint('sort_order >= 0'),
        CheckConstraint("content_type IN ('image/png', 'image/jpeg', 'image/webp')"),
        CheckConstraint('length(sha256) = 64'),
        CheckConstraint('role IN (' + ','.join(repr(role.value) for role in AssetRole) + ')'),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey('nmm_projects.id', ondelete='CASCADE'), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(40))
    storage_name: Mapped[str] = mapped_column(Text, unique=True)
    content_type: Mapped[str] = mapped_column(String(40))
    size: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class NmmPlan(Base):
    __tablename__ = 'nmm_plans'
    __table_args__ = (
        CheckConstraint('revision >= 1'),
        Index('ix_nmm_plans_project_revision', 'project_id', 'revision'),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey('nmm_projects.id', ondelete='CASCADE'), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey('nmm_assets.id', ondelete='RESTRICT'))
    plan: Mapped[dict] = mapped_column(JSON)
    palette: Mapped[list] = mapped_column(JSON)
    renderer_version: Mapped[str] = mapped_column(String(100))
    render_params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class NmmStep(Base):
    __tablename__ = 'nmm_steps'
    __table_args__ = (
        CheckConstraint('index >= 1 AND index <= 9'),
        UniqueConstraint('plan_id', 'index'),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey('nmm_plans.id', ondelete='CASCADE'), index=True
    )
    index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    image_asset_id: Mapped[UUID] = mapped_column(ForeignKey('nmm_assets.id', ondelete='RESTRICT'))
    diff_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey('nmm_assets.id', ondelete='RESTRICT'), nullable=True
    )
    description: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
