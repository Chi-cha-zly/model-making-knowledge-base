"""Bounded backup upload/download transport and active-generation guard."""

from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ..services.backups import BackupService
from .dependencies import get_backups

router = APIRouter(prefix='/api/v1/backup')


@router.get('')
def export_backup(service: BackupService = Depends(get_backups)):
    path = service.export_backup()
    return FileResponse(
        path,
        media_type='application/zip',
        filename='knowledge-backup.zip',
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@router.post('')
async def restore_backup(
    request: Request, file: UploadFile = File(...), service: BackupService = Depends(get_backups)
):
    state = request.app.state
    async with state.chat.lock, state.nmm.lock, state.jobs.lock:
        if state.chat.has_active() or state.experiments.has_active():
            raise HTTPException(409, '请先停止正在生成的对话和实验，再恢复备份。')
        service.ensure_nmm_idle()
        with TemporaryDirectory(dir=state.storage, prefix='backup-upload-') as name:
            total = 0
            with (Path(name) / 'backup.zip').open('wb') as output:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > state.backup_limit:
                        raise HTTPException(413, 'Backup exceeds configured size limit')
                    output.write(chunk)
            return service.restore_backup_archive(Path(name))
