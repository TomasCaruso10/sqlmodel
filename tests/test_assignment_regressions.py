"""Rejected field assignments must not reach the ORM or database."""

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, inspect
from sqlmodel import Field, Session, SQLModel


@pytest.mark.xfail(
    strict=True,
    reason="Upstream writes to SQLAlchemy before Pydantic rejects the assigned value.",
)
def test_rejected_assignment_keeps_the_value_and_clean_session(
    database_engine: Engine,
) -> None:
    class Order(SQLModel, table=True):
        model_config = {"validate_assignment": True}

        id: int | None = Field(default=None, primary_key=True)
        quantity: int = Field(gt=0)

    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        order = Order(quantity=2)
        session.add(order)
        session.flush()

        with pytest.raises(ValidationError):
            order.quantity = -1

        assert order.quantity == 2
        assert order not in session.dirty
        assert not inspect(order).attrs.quantity.history.has_changes()


@pytest.mark.xfail(
    strict=True,
    reason="Upstream leaves rejected assigned values available for a later flush.",
)
def test_rejected_assignment_is_not_persisted_by_a_later_flush(
    database_engine: Engine,
) -> None:
    class Order(SQLModel, table=True):
        model_config = {"validate_assignment": True}

        id: int | None = Field(default=None, primary_key=True)
        quantity: int = Field(gt=0)

    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        order = Order(quantity=2)
        session.add(order)
        session.flush()
        key = order.id

        with pytest.raises(ValidationError):
            order.quantity = -1

        session.flush()
        session.expunge_all()
        loaded = session.get(Order, key)

        assert loaded is not None
        assert loaded.quantity == 2
