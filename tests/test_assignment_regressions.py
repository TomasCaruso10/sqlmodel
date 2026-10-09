"""Rejected field assignments must not reach the ORM or database."""

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, event, inspect
from sqlmodel import Field, Session, SQLModel


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


@pytest.mark.parametrize("expired", [False, True])
def test_valid_assignment_delivers_only_the_validated_change(
    database_engine: Engine, expired: bool
) -> None:
    class Order(SQLModel, table=True):
        model_config = {"validate_assignment": True}
        id: int | None = Field(default=None, primary_key=True)
        quantity: int = Field(gt=0)
        label: str = "unchanged"

    writes: list[tuple[str, object]] = []
    event.listen(
        Order.quantity,
        "set",
        lambda target, value, old, initiator: writes.append(("quantity", value)),
    )
    event.listen(
        Order.label,
        "set",
        lambda target, value, old, initiator: writes.append(("label", value)),
    )
    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        order = Order(quantity=2)
        session.add(order)
        session.flush()
        key = order.id
        if expired:
            session.expire(order)
        writes.clear()

        with pytest.raises(ValidationError):
            order.quantity = -1
        assert not writes
        assert order not in session.dirty

        order.quantity = "3"  # Pydantic converts before SQLAlchemy sees the value.
        assert writes == [("quantity", 3)]
        history = inspect(order).attrs.quantity.history
        assert history.added == [3]
        if not expired:
            assert history.deleted == [2]
        session.flush()
        session.expunge_all()
        loaded = session.get(Order, key)
        assert loaded is not None and loaded.quantity == 3

        writes.clear()
        loaded.quantity = 3
        assert writes == [("quantity", 3)]
