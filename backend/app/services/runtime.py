"""Runtime composition and lifecycle, separate from HTTP endpoint definitions."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from .chat import ChatService
from ..db import create_db_engine, init_db
from .experiments import ExperimentService
from .ingestion import IngestionService
from ..llm import LangChainProvider
from ..jobs import JobService
from ..runtime_config import read_settings
from ..repositories.settings import PrivateSettings
from ..prompts import compose_prompt
from .retrieval import RetrievalService

MAX_BACKUP = 100 * 1024 * 1024


@dataclass
class Runtime:
    engine: object
    storage: Path
    backup_limit: int
    chat: ChatService
    experiments: ExperimentService
    user_settings: PrivateSettings
    jobs: JobService
    retrieval: RetrievalService


def build_runtime(
    database_url=None,
    storage_dir=None,
    provider=None,
    settings_path=None,
    runtime_settings=None,
    credential_store=None,
):
    config = read_settings()
    root = Path(__file__).resolve().parents[3]
    storage = Path(storage_dir or config.get('DATA_DIR') or root / 'data' / 'attachments')
    if not storage.is_absolute():
        storage = root / storage
    storage = storage.resolve()
    storage.mkdir(parents=True, exist_ok=True)
    backup_limit = int(config.get('BACKUP_MAX_BYTES') or MAX_BACKUP)
    if backup_limit <= 0:
        raise ValueError('BACKUP_MAX_BYTES must be a positive integer')
    engine = create_db_engine(
        database_url
        or config.get('DATABASE_URL')
        or 'postgresql+psycopg://modelkb:modelkb@127.0.0.1:5432/modelkb'
    )
    chat = ChatService(
        engine,
        provider
        if provider is not None
        else (
            LangChainProvider(runtime_settings)
            if runtime_settings is not None
            else LangChainProvider.from_env()
        ),
    )
    env = (
        dict(runtime_settings)
        if runtime_settings is not None
        else dict(getattr(chat.provider, 'settings', None) or config)
    )
    retrieval = RetrievalService(
        engine,
        provider_getter=lambda: chat.provider,
        mode=(runtime_settings if runtime_settings is not None else config).get(
            'RETRIEVAL_MODE', 'lexical'
        ),
    )
    chat.retrieval = retrieval
    private_path = (
        Path(settings_path).resolve()
        if settings_path is not None
        else storage.parent / '.modelkb-private-settings.json'
    )
    if private_path == storage or storage in private_path.parents:
        raise ValueError('Private settings must be outside attachment storage')
    user_settings = PrivateSettings(private_path, env, credentials=credential_store)
    jobs = JobService(engine)
    experiments = ExperimentService(engine, chat.provider, chat.lock, settings=env, jobs=jobs)
    chat.system_prompt = experiments.system_prompt = compose_prompt(
        user_settings.data['custom_prompt']
    )
    return Runtime(engine, storage, backup_limit, chat, experiments, user_settings, jobs, retrieval)


@asynccontextmanager
async def application_lifespan(app):
    state = app.state
    engine = state.engine
    try:
        # Startup verifies explicit migrations; requests never create or upgrade tables.
        init_db(state.engine)
        # System credentials and legacy migration are deferred until managed startup.
        state.user_settings.initialize()
        values = state.user_settings.values()
        if state.configure_saved_provider:
            state.chat.provider = LangChainProvider(values)
            state.experiments.provider = state.chat.provider
        state.experiments.settings = values
        with Session(state.engine) as db:
            IngestionService(db).backfill(missing_only=True)
            db.commit()
        state.jobs.recover()
        state.experiments.recover()
        async with state.chat.lifespan(app):
            try:
                yield
            finally:
                async with state.chat.lock:
                    for job_id in list(state.experiments.tasks):
                        await state.experiments.stop(job_id)
                await state.jobs.shutdown()
    finally:
        engine.dispose()
