"""HTTP catalog contract; business rules live in CatalogService."""

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from ..schemas import (
    ProductInput,
    ProductPatch,
    SourceInput,
    SourcePatch,
    TechniqueInput,
    TechniquePatch,
)
from ..services.catalog import CatalogService
from .dependencies import get_catalog

router = APIRouter(prefix='/api/v1')


@router.get('/sources')
def list_sources(service: CatalogService = Depends(get_catalog)):
    return service.list_sources()


@router.post('/sources', status_code=201)
def create_source(data: SourceInput, service: CatalogService = Depends(get_catalog)):
    return service.create_source(data)


@router.get('/sources/{id}')
def get_source(id: int, service: CatalogService = Depends(get_catalog)):
    return service.get_source(id)


@router.patch('/sources/{id}')
def patch_source(id: int, data: SourcePatch, service: CatalogService = Depends(get_catalog)):
    return service.patch_source(id, data)


@router.delete('/sources/{id}')
def withdraw_source(id: int, service: CatalogService = Depends(get_catalog)):
    return service.withdraw_source(id)


@router.get('/techniques')
def list_techniques(service: CatalogService = Depends(get_catalog)):
    return service.list_techniques()


@router.post('/techniques', status_code=201)
def create_technique(data: TechniqueInput, service: CatalogService = Depends(get_catalog)):
    return service.create_technique(data)


@router.get('/techniques/{id}')
def get_technique(id: int, service: CatalogService = Depends(get_catalog)):
    return service.get_technique(id)


@router.patch('/techniques/{id}')
def patch_technique(id: int, data: TechniquePatch, service: CatalogService = Depends(get_catalog)):
    return service.patch_technique(id, data)


@router.delete('/techniques/{id}')
def withdraw_technique(id: int, service: CatalogService = Depends(get_catalog)):
    return service.withdraw_technique(id)


@router.get('/products')
def list_products(service: CatalogService = Depends(get_catalog)):
    return service.list_products()


@router.post('/products', status_code=201)
def create_product(data: ProductInput, service: CatalogService = Depends(get_catalog)):
    return service.create_product(data)


@router.get('/products/{id}')
def get_product(id: int, service: CatalogService = Depends(get_catalog)):
    return service.get_product(id)


@router.patch('/products/{id}')
def patch_product(id: int, data: ProductPatch, service: CatalogService = Depends(get_catalog)):
    return service.patch_product(id, data)


@router.delete('/products/{id}')
def withdraw_product(id: int, service: CatalogService = Depends(get_catalog)):
    return service.withdraw_product(id)


@router.get('/search')
def search(
    q: str = Query(min_length=1, max_length=2000), service: CatalogService = Depends(get_catalog)
):
    return service.search(q)


@router.post('/imports/markdown', status_code=201)
async def import_markdown(
    file: UploadFile = File(...),
    source_title: str | None = Form(None),
    source_url: str | None = Form(None),
    service: CatalogService = Depends(get_catalog),
):
    data = await file.read(1024 * 1024 + 1)
    return service.import_markdown(file.filename, data, source_title, source_url)
