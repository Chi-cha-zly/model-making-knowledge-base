"""Multipart uploads and private image HTTP responses."""

from urllib.parse import quote
from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response
from ..services.attachments import AttachmentService, MAX_IMAGE
from .dependencies import get_attachments

router = APIRouter(prefix='/api/v1/attachments')


@router.post('', status_code=201)
async def upload_attachment(
    file: UploadFile = File(...), service: AttachmentService = Depends(get_attachments)
):
    return service.upload(file.filename, file.content_type or '', await file.read(MAX_IMAGE + 1))


@router.get('/{id}')
def download_attachment(id: int, service: AttachmentService = Depends(get_attachments)):
    data, content_type, filename = service.download(id)
    return Response(
        content=data,
        media_type=content_type,
        headers={
            'Content-Disposition': f"inline; filename*=UTF-8''{quote(filename, safe='')}",
            'X-Content-Type-Options': 'nosniff',
        },
    )
