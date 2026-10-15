from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from app.runtime_config import read_settings
from app.models import Base
from app import (  # noqa: F401 — register every domain table in Base.metadata
    chat_models,
    evaluation_models,
    experiment_models,
    ingestion_models,
    job_models,
    vision_models,
)


from app.nmm import models as nmm_models  # noqa: F401 — register NMM tables

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
if not config.attributes.get('connection'):
    config.set_main_option(
        'sqlalchemy.url',
        (
            read_settings().get('DATABASE_URL')
            or 'postgresql+psycopg://modelkb:modelkb@127.0.0.1:5432/modelkb'
        ).replace('%', '%%'),
    )
supplied_connection = config.attributes.get('connection')
backend = (
    supplied_connection.dialect.name
    if supplied_connection is not None
    else make_url(config.get_main_option('sqlalchemy.url')).get_backend_name()
)
if backend != 'postgresql':
    raise ValueError(
        'PostgreSQL is required; copy legacy SQLite data with the explicit migration tool'
    )
target_metadata = Base.metadata
version_table_schema = config.attributes.get('version_table_schema')


def include_object(object_, name, type_, reflected, compare_to):
    # SDK-owned checkpoint schema is frozen in explicit migrations, not ORM models.
    if (
        type_ == 'table'
        and reflected
        and name
        in {'checkpoints', 'checkpoint_blobs', 'checkpoint_writes', 'checkpoint_migrations'}
    ):
        return False
    # PostgreSQL-specific trigram indexes are maintained by migration SQL.
    return not (type_ == 'index' and reflected and name.endswith('_trgm'))


def run_migrations_offline():
    context.configure(
        url=config.get_main_option('sqlalchemy.url'),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=version_table_schema,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    supplied = config.attributes.get('connection')
    if supplied is not None:
        context.configure(
            connection=supplied,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
            version_table_schema=version_table_schema,
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section), prefix='sqlalchemy.', poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
            version_table_schema=version_table_schema,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
