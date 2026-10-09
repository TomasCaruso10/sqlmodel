"""Exercise the boundaries between Pydantic validation and ORM instrumentation."""

import copy
import json
import pickle
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import (
    BaseModel,
    ModelWrapValidatorHandler,
    PrivateAttr,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from sqlalchemy import Engine, event, inspect
from sqlmodel import Field, Relationship, Session, SQLModel


@pytest.mark.parametrize(
    "entry", ["init", "python", "json", "adapter", "list", "nested", "strings"]
)
def test_validation_entry_points_preserve_hooks_and_state(
    entry: str, database_engine: Engine
) -> None:
    calls: list[tuple[str, Any]] = []

    class Item(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int = Field(gt=0)
        _marker: str = PrivateAttr(default="ready")

        @model_validator(mode="wrap")
        @classmethod
        def record_wrap(
            cls,
            data: Any,
            handler: ModelWrapValidatorHandler["Item"],
            info: ValidationInfo,
        ) -> "Item":
            calls.append(("wrap_before", info.context))
            result = handler(data)
            calls.append(("wrap_after", info.context))
            return result

        @model_validator(mode="before")
        @classmethod
        def record_before(cls, data: Any, info: ValidationInfo) -> Any:
            calls.append(("before", info.context))
            return data

        @field_validator("quantity")
        @classmethod
        def record_field(cls, value: int, info: ValidationInfo) -> int:
            assert info.mode == {"json": "json", "strings": "string"}.get(
                entry, "python"
            )
            calls.append(("field", info.context))
            return value

        def model_post_init(self, context: Any) -> None:
            assert inspect(self) is not None
            assert self._marker == "ready"
            calls.append(("post", context))

        @model_validator(mode="after")
        def record_after(self, info: ValidationInfo) -> "Item":
            calls.append(("after", info.context))
            return self

    class Envelope(BaseModel):
        item: Item

    context = None if entry == "init" else {"request": "test"}

    def build(quantity: Any) -> Item:
        data = {"quantity": quantity}
        if entry == "init":
            return Item(**data)
        if entry == "python":
            return Item.model_validate(data, context=context)
        if entry == "json":
            return Item.model_validate_json(json.dumps(data), context=context)
        if entry == "adapter":
            return TypeAdapter(Item).validate_python(data, context=context)
        if entry == "list":
            return TypeAdapter(list[Item]).validate_python([data], context=context)[0]
        if entry == "nested":
            return Envelope.model_validate({"item": data}, context=context).item
        return Item.model_validate_strings({"quantity": str(quantity)}, context=context)

    item = build(2)
    assert calls == [
        ("wrap_before", context),
        ("before", context),
        ("field", context),
        ("post", context),
        ("wrap_after", context),
        ("after", context),
    ]
    assert item.model_dump() == {"id": None, "quantity": 2}
    assert item.model_fields_set == {"quantity"}
    assert "item" in Envelope.model_json_schema()["properties"]
    with pytest.raises(ValidationError):
        build(-1)
    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        session.add(item)
        session.flush()
        key = item.id
        calls.clear()
        session.expunge_all()
        loaded = session.get(Item, key)
        assert loaded is not None and loaded.quantity == 2
        assert not calls  # Database loading uses SQLAlchemy's lifecycle.


@pytest.mark.parametrize("entry", ["python", "json", "adapter"])
def test_strict_validation_is_not_lost(entry: str) -> None:
    class Item(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int

    with pytest.raises(ValidationError) as captured:
        if entry == "python":
            Item.model_validate({"quantity": "2"}, strict=True)
        elif entry == "json":
            Item.model_validate_json('{"quantity": "2"}', strict=True)
        else:
            TypeAdapter(Item).validate_python({"quantity": "2"}, strict=True)
    assert captured.value.errors()[0]["type"] == "int_type"


@pytest.mark.parametrize("expired", [False, True])
def test_rollback_discards_assignment_rejected_by_model_validator(
    database_engine: Engine, expired: bool
) -> None:
    class Item(SQLModel, table=True):
        model_config = {"validate_assignment": True}
        id: int | None = Field(default=None, primary_key=True)
        quantity: int = Field(gt=0)
        ceiling: int = 10

        @model_validator(mode="after")
        def check_ceiling(self) -> "Item":
            if self.quantity > self.ceiling:
                raise ValueError("Quantity exceeds ceiling")
            return self

    writes: list[int] = []
    event.listen(
        Item.quantity, "set", lambda target, value, old, initiator: writes.append(value)
    )
    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        with session.begin():
            item = Item(quantity=2)
            session.add(item)
            session.flush()
            key = item.id

        with pytest.raises(ValidationError, match="Quantity exceeds ceiling"):
            with session.begin():
                item = session.get(Item, key)
                assert item is not None
                if expired:
                    session.expire(item)
                writes.clear()
                try:
                    item.quantity = 11
                except ValidationError:
                    # Pydantic's after validator runs after the field is written.
                    # Raising does not undo that write; the transaction rolls back.
                    assert item.quantity == 11
                    assert writes == [11]
                    raise

        assert item.quantity == 2
        assert item not in session.dirty

    with Session(database_engine) as session:
        loaded = session.get(Item, key)
        assert loaded is not None and loaded.quantity == 2


@pytest.mark.parametrize(
    "operation",
    [
        "copy",
        "deepcopy",
        "model_copy",
        "pickle",
    ],
)
def test_dictionary_restore_preserves_native_orm_identity(
    operation: str, database_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Item(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int

    Item.__qualname__ = "PickledItem"
    monkeypatch.setattr(sys.modules[__name__], "PickledItem", Item, raising=False)
    initialized: list[Any] = []
    event.listen(Item, "init", lambda target, args, kwargs: initialized.append(target))
    assigned: list[Any] = []
    event.listen(
        Item.quantity,
        "set",
        lambda target, value, old, initiator: assigned.append(value),
    )
    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        item = Item(quantity=2)
        session.add(item)
        session.commit()
        assert (
            item.quantity == 2
        )  # Load before copying, without triggering lazy I/O later.
        initialized.clear()
        assigned.clear()
        if operation == "copy":
            restored = copy.copy(item)
        elif operation == "deepcopy":
            restored = copy.deepcopy(item)
        elif operation == "model_copy":
            restored = item.model_copy(deep=True)
        else:
            restored = pickle.loads(pickle.dumps(item))
        assert restored is not item
        assert inspect(restored).key == inspect(item).key
        if operation in {"deepcopy", "model_copy", "pickle"}:
            assert inspect(restored).object is restored
        assert not initialized
        assert not assigned
        assert not inspect(restored).modified
        assert not inspect(restored).attrs.quantity.history.has_changes()
        assert restored.model_dump() == item.model_dump()


@pytest.mark.parametrize("entry", ["init", "python", "json", "adapter"])
def test_init_event_timing(entry: str) -> None:
    calls: list[str] = []

    class Item(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int

        @field_validator("quantity")
        @classmethod
        def record(cls, value: int) -> int:
            calls.append("field")
            return value

        def model_post_init(self, context: Any) -> None:
            calls.append("post")

    event.listen(Item, "init", lambda target, args, kwargs: calls.append("init"))
    if entry == "init":
        Item(quantity=2)
        assert calls == ["init", "field", "post"]
    else:
        if entry == "python":
            Item.model_validate({"quantity": 2})
        elif entry == "json":
            Item.model_validate_json('{"quantity": 2}')
        else:
            TypeAdapter(Item).validate_python({"quantity": 2})
        assert calls == ["field", "init", "post"]


@pytest.mark.parametrize("entry", ["init", "python", "adapter", "nested", "attributes"])
def test_validated_relationships_preserve_identity_and_backrefs(
    entry: str, database_engine: Engine
) -> None:
    class Customer(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        orders: list["Order"] = Relationship(back_populates="customer")

    class Order(SQLModel, table=True):
        model_config = {"extra": "forbid"}
        id: int | None = Field(default=None, primary_key=True)
        quantity: int = Field(gt=0)
        customer_id: int | None = Field(default=None, foreign_key="customer.id")
        customer: Customer | None = Relationship(back_populates="orders")

        @model_validator(mode="before")
        @classmethod
        def normalize(cls, value: Any) -> Any:
            if isinstance(value, dict) and "selected_customer" in value:
                value = dict(value)
                value["customer"] = value.pop("selected_customer")
            return value

        @model_validator(mode="after")
        def verify_backref(self) -> "Order":
            assert self.customer is not None
            assert any(order is self for order in self.customer.orders)
            return self

    class Envelope(BaseModel):
        order: Order

    customer = Customer(name="Ana")
    data = {"quantity": 2, "selected_customer": customer}
    if entry == "init":
        order = Order(**data)
    elif entry == "python":
        order = Order.model_validate(data)
    elif entry == "adapter":
        order = TypeAdapter(Order).validate_python(data)
    elif entry == "nested":
        order = Envelope.model_validate({"order": data}).order
    else:
        order = Order.model_validate(SimpleNamespace(quantity=2, customer=customer))
    assert order.customer is customer
    assert data == {"quantity": 2, "selected_customer": customer}
    assert "customer" not in order.model_dump()
    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        session.add(order)
        session.flush()
        assert order.customer_id == customer.id
        key = order.id
        session.expunge_all()
        loaded = session.get(Order, key)
        assert loaded is not None and loaded.customer.name == "Ana"


def test_model_validate_runs_field_validator_once() -> None:
    validated_values: list[int] = []

    class Item(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int

        @field_validator("quantity")
        @classmethod
        def record_validation(cls, value: int) -> int:
            validated_values.append(value)
            return value

    item = Item.model_validate({"quantity": 2})

    assert inspect(item).object is item
    assert item.quantity == 2
    assert validated_values == [2]


@pytest.mark.parametrize("operation", ["deepcopy", "model_copy"])
def test_deep_copy_preserves_relationship_cycles(operation: str) -> None:
    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        children: list["Child"] = Relationship(back_populates="parent")
        _notes: list[str] = PrivateAttr(default_factory=list)

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int | None = Field(default=None, foreign_key="parent.id")
        parent: Parent | None = Relationship(back_populates="children")

    parent = Parent(children=[Child()])
    parent._notes.append("original")
    copied = (
        copy.deepcopy(parent)
        if operation == "deepcopy"
        else parent.model_copy(deep=True)
    )

    assert copied is not parent
    assert copied.children[0] is not parent.children[0]
    assert copied.children[0].parent is copied
    assert parent.children[0].parent is parent
    assert inspect(copied).object is copied
    assert inspect(copied.children[0]).object is copied.children[0]
    copied._notes.append("copy")
    assert parent._notes == ["original"]
    assert copied._notes == ["original", "copy"]
