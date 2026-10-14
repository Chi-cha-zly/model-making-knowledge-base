"""Request-scoped dependencies; all runtime resources come from app.state."""

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from ..repositories.catalog import CatalogRepository
from ..repositories.ingestion import IngestionRepository
from ..services.attachments import AttachmentService
from ..services.backups import BackupService
from ..services.catalog import CatalogService
from ..services.ingestion import IngestionService


def get_session(request: Request):
    with Session(request.app.state.engine) as session:
        yield session


def get_catalog(session: Session = Depends(get_session)):
    return CatalogService(CatalogRepository(session))


def get_attachments(request: Request, session: Session = Depends(get_session)):
    return AttachmentService(CatalogRepository(session), request.app.state.storage)


def get_ingestion(session: Session = Depends(get_session)):
    return IngestionService(IngestionRepository(session))


def get_backups(request: Request):
    state = request.app.state
    return BackupService(state.engine, state.storage, state.backup_limit)
