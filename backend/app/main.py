"""Application factory: dependencies, lifecycle, and router assembly only."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .credentials import CredentialError

from .api import attachments, backups, catalog, ingestion, nmm
from .api.errors import service_error_response
from .api.jobs import install_jobs
from .chat import install_chat
from .evaluation import install_evaluation
from .experiments import install_experiments
from .services.errors import ServiceError
from .services.search import SearchService
from .services.runtime import application_lifespan, build_runtime
from .user_settings import install_settings


def create_app(
    database_url: str | None = None,
    storage_dir: str | Path | None = None,
    provider=None,
    settings_path=None,
    runtime_settings=None,
    credential_store=None,
    nmm_vision_provider=None,
    nmm_image_provider=None,
    nmm_checkpoint_store=None,
) -> FastAPI:
    runtime = build_runtime(
        database_url,
        storage_dir,
        provider,
        settings_path,
        runtime_settings,
        credential_store,
        nmm_vision_provider,
        nmm_image_provider,
        nmm_checkpoint_store,
    )
    app = FastAPI(title='模型制作知识库 API', version='0.4.0', lifespan=application_lifespan)
    app.state.configure_saved_provider = provider is None
    app.state.retrieval = runtime.retrieval
    app.state.search = SearchService(runtime.engine, runtime.retrieval)
    app.state.nmm = runtime.nmm
    app.state.jobs = runtime.jobs
    app.state.engine = runtime.engine
    app.state.storage = runtime.storage
    app.state.backup_limit = runtime.backup_limit
    app.add_exception_handler(ServiceError, service_error_response)
    app.add_exception_handler(
        CredentialError,
        lambda request, error: JSONResponse(status_code=503, content={'detail': str(error)}),
    )
    install_settings(app, runtime.user_settings, runtime.chat, runtime.experiments)
    install_experiments(app, runtime.experiments)
    install_chat(app, runtime.chat)
    install_evaluation(app, runtime.engine)
    install_jobs(app)
    for router in (
        catalog.router,
        attachments.router,
        ingestion.router,
        backups.router,
        nmm.router,
    ):
        app.include_router(router)
    return app


app = create_app()
