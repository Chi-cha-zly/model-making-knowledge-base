"""Translate application errors to the public HTTP error shape."""
from fastapi.responses import JSONResponse
from ..services.errors import ServiceError


async def service_error_response(request, error: ServiceError):
    return JSONResponse(status_code=error.status_code, content={'detail': error.detail})
