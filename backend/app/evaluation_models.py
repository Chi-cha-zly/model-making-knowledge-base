"""Independent evaluation records. Reports are immutable JSON snapshots."""
from sqlalchemy import Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base


class EvaluationDataset(Base):
    __tablename__ = 'evaluation_datasets'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class EvaluationRun(Base):
    __tablename__ = 'evaluation_runs'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
