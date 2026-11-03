from sqlalchemy import func, select

from keyloop_test import seed as seed_module
from keyloop_test.models import Dealership


def test_seed_cli_is_idempotent(session):
    assert seed_module.main() == "seeded"
    assert seed_module.main() == "already seeded"
    assert session.scalar(select(func.count()).select_from(Dealership)) == 2
