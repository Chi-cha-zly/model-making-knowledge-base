"""HTTP operations for catalog ingestion."""
from fastapi import APIRouter, Depends
from ..services.ingestion import IngestionService
from .dependencies import get_ingestion

router = APIRouter(prefix='/api/v1/ingestion')


@router.get('/jobs')
def ingestion_jobs(service: IngestionService = Depends(get_ingestion)):
    return service.jobs()


@router.post('/backfill')
def ingestion_backfill(service: IngestionService = Depends(get_ingestion)):
    return service.backfill()


@router.post('/documents/{id}/retry')
def retry_ingestion(id: int, service: IngestionService = Depends(get_ingestion)):
    return service.retry(id)
