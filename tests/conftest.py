import os

import pytest
from sqlalchemy import text

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://scheduler:scheduler@localhost:5432/scheduler_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL  # before the app's engine is created

from keyloop_test import db  # noqa: E402
from keyloop_test.models import Base  # noqa: E402
from keyloop_test.seed import seed  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    Base.metadata.drop_all(db.engine)
    db.init_db(db.engine)
    return db.engine


@pytest.fixture
def session(engine):
    s = db.SessionLocal()
    yield s
    s.close()
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def seeded(session):
    seed(session)
