"""PostgreSQL runtime policy. Schema changes are always explicit Alembic work."""
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory


def create_db_engine(database_url):
    url = make_url(database_url)
    if url.get_backend_name() != 'postgresql':
        raise ValueError('PostgreSQL is required; migrate legacy SQLite data before starting')
    if url.drivername == 'postgresql':
        url = url.set(drivername='postgresql+psycopg')
    return create_engine(url, pool_pre_ping=True)


def init_db(engine):
    cfg = Config()
    cfg.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'alembic'))
    expected = set(ScriptDirectory.from_config(cfg).get_heads())
    with engine.connect() as connection:
        actual = set(MigrationContext.configure(connection).get_current_heads())
    if actual != expected:
        raise RuntimeError('Database schema is not current; run alembic upgrade head before starting')
