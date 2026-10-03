from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app.models import Base
from tests.conftest import alembic_config

import os


def test_migrations_round_trip_and_match_models() -> None:
    config = alembic_config()
    command.downgrade(config, "base")
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) <= {"alembic_version"}
    command.upgrade(config, "head")
    with engine.connect() as connection:
        # The schema the migrations build is exactly the one the models describe.
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert diff == []
        assert connection.execute(text("SELECT nextval('sync_seq')")).scalar_one() >= 1
    engine.dispose()


def test_audit_events_are_immutable(db) -> None:  # type: ignore[no-untyped-def]
    import pytest
    from sqlalchemy.exc import DBAPIError

    from app import models as m

    event = m.AuditEvent(actor_type="system", action="test.event", data={})
    db.session.add(event)
    db.session.commit()
    with pytest.raises(DBAPIError, match="immutable"):
        db.session.execute(text("UPDATE audit_events SET action = 'changed'"))
    db.session.rollback()
    with pytest.raises(DBAPIError, match="immutable"):
        db.session.execute(text("DELETE FROM audit_events"))
    db.session.rollback()
