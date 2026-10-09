"""Construction regressions across Pydantic validation and ORM instrumentation."""

from typing import Any

import pytest
from pydantic import (
    AliasPath,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic import Field as PydanticField
from sqlalchemy import Engine, event, inspect
from sqlmodel import Field, Session, SQLModel
from typing_extensions import Self


def test_self_is_a_valid_field_name(database_engine: Engine) -> None:
    class Item(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        self: str

    item = Item(self="value")
    assert item.self == "value"
    assert item.model_dump()["self"] == "value"
    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        session.add(item)
        session.flush()
        key = item.id
        session.expunge_all()
        loaded = session.get(Item, key)
        assert loaded is not None and loaded.self == "value"


def test_table_constructor_requires_declared_fields() -> None:
    class Item(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    with pytest.raises(ValidationError) as captured:
        Item()

    assert captured.value.errors()[0]["loc"] == ("name",)
    assert captured.value.errors()[0]["type"] == "missing"


def test_failed_model_validation_does_not_disable_later_constructors() -> None:
    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)

    class Audit(SQLModel):
        message: str

    @event.listens_for(Order, "init")
    def reject(target: Any, args: Any, kwargs: Any) -> None:
        raise RuntimeError("constructor rejected")

    with pytest.raises(RuntimeError, match="constructor rejected"):
        Order.model_validate({"id": 1})

    assert Audit(message="created").model_dump() == {"message": "created"}


def test_model_validation_allows_other_models_to_initialize_in_callbacks() -> None:
    class Audit(SQLModel):
        message: str

    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int

    audits: list[Audit] = []

    @event.listens_for(Order, "init")
    def audit(target: Any, args: Any, kwargs: Any) -> None:
        audits.append(Audit(message="created"))

    order = Order.model_validate({"quantity": 2})

    assert order.quantity == 2
    assert len(audits) == 1
    assert audits[0].model_dump() == {"message": "created"}


def test_table_constructor_resolves_alias_path() -> None:
    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        name: str = PydanticField(validation_alias=AliasPath("customer", "name"))

    order = Order(customer={"name": "Ana"})

    assert order.model_dump() == {"id": None, "name": "Ana"}


def test_table_constructor_supplies_earlier_fields_to_default_factory() -> None:
    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int
        label: str = PydanticField(default_factory=lambda data: f"q{data['quantity']}")

    order = Order(quantity=2)

    assert order.label == "q2"


def test_model_construct_returns_an_instrumented_table_instance() -> None:
    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int

    order = Order.model_construct(quantity=2)
    order.quantity = 3

    assert order.quantity == 3
    assert inspect(order) is not None


def test_model_construct_allows_post_init_to_modify_mapped_fields() -> None:
    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int

        def model_post_init(self, context: Any) -> None:
            self.quantity += 1

    order = Order.model_construct(quantity=2)

    assert order.quantity == 3
    assert inspect(order) is not None


def test_model_validate_allows_post_init_to_modify_mapped_fields() -> None:
    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int

        def model_post_init(self, context: Any) -> None:
            self.quantity += 1

    order = Order.model_validate({"quantity": 2})

    assert order.quantity == 3
    assert inspect(order) is not None


def test_model_validate_allows_after_validators_to_modify_mapped_fields() -> None:
    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int

        @model_validator(mode="after")
        def adjust_quantity(self) -> Self:
            self.quantity += 1
            return self

    order = Order.model_validate({"quantity": 2})

    assert order.quantity == 3
    assert inspect(order) is not None


@pytest.mark.parametrize("fail_nested", [False, True])
def test_initialization_context_is_scoped_to_one_instance(fail_nested: bool) -> None:
    validated: list[int] = []

    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int

        @field_validator("quantity")
        @classmethod
        def record_validation(cls, value: int) -> int:
            validated.append(value)
            return value

    children: list[Order] = []

    @event.listens_for(Order, "init")
    def create_nested_orders(target: Any, args: Any, kwargs: Any) -> None:
        if kwargs["quantity"] == 3 and fail_nested:
            raise RuntimeError("nested constructor rejected")
        if kwargs["quantity"] == 1:
            children.append(Order(quantity=2))
            if fail_nested:
                with pytest.raises(RuntimeError, match="nested constructor rejected"):
                    Order.model_validate({"quantity": 3})
            else:
                children.append(Order.model_validate({"quantity": 3}))

    order = Order.model_validate({"quantity": 1})

    # Other instances validate normally, and the outer instance stays marked
    # after nested initialization returns or raises.
    assert validated == [1, 2, 3]
    assert order.quantity == 1
    assert [child.quantity for child in children] == ([2] if fail_nested else [2, 3])


@pytest.mark.parametrize("table", [False, True])
def test_model_construct_preserves_trusted_data_and_inherited_hooks(
    table: bool, database_engine: Engine
) -> None:
    calls: list[str] = []

    class OrderBase(SQLModel):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int = Field(gt=0, alias="amount")
        label: str = PydanticField(default_factory=lambda data: f"q{data['quantity']}")
        _marker: str = PrivateAttr(default="ready")

        @field_validator("quantity")
        @classmethod
        def reject_validation(cls, value: int) -> int:
            raise AssertionError("model_construct must not run field validators")

        def model_post_init(self, context: Any) -> None:
            assert context is None
            assert self._marker == "ready"
            calls.append("parent")

    class Order(OrderBase, table=table):
        def model_post_init(self, context: Any) -> None:
            super().model_post_init(context)
            self.quantity += 1
            calls.append("child")

    if table:
        event.listen(Order, "init", lambda target, args, kwargs: calls.append("orm"))

    order = Order.model_construct(amount=-2, _fields_set={"quantity"})

    assert order.quantity == -1  # Trusted data is not checked against gt=0.
    assert order.label == "q-2"
    assert order.model_fields_set == {"quantity"}
    assert calls == (["orm", "parent", "child"] if table else ["parent", "child"])
    if table:
        SQLModel.metadata.create_all(database_engine)
        with Session(database_engine) as session:
            session.add(order)
            session.flush()
            key = order.id
            session.expunge_all()
            loaded = session.get(Order, key)
            assert loaded is not None and loaded.quantity == -1
            assert calls == ["orm", "parent", "child"]
