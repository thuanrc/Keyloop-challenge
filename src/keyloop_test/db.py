import os
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from keyloop_test.models import Base

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://scheduler:scheduler@localhost:5432/scheduler"
)

engine = create_engine(
    DATABASE_URL,
    pool_size=int(os.environ.get("DB_POOL_SIZE", "10")),
    max_overflow=10,
    pool_timeout=5,
    pool_pre_ping=True,
    # fail fast instead of hanging: unreachable DB host, a concurrent booking's lock, slow queries
    connect_args={
        "connect_timeout": 3,
        "options": "-c lock_timeout=2000 -c statement_timeout=5000",
    },
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def init_db(engine: Engine) -> None:
    # shortcut: create_all instead of migrations; move to Alembic when the schema first changes after release
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
    Base.metadata.create_all(engine)
