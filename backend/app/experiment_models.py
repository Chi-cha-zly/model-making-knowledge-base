"""Independent V4 job history and frozen experiment inputs."""

from sqlalchemy import Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base


class ExperimentJob(Base):
    __tablename__ = 'experiment_jobs'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
