from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.main import _config
from app.models import Base


config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", _config()[0].replace("%", "%%"))
target_metadata = Base.metadata


def include_object(object_, name, type_, reflected, compare_to):
    # PostgreSQL-specific trigram indexes are maintained by migration SQL.
    return not (type_ == "index" and reflected and name.endswith("_trgm"))


def run_migrations_offline():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True, include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
