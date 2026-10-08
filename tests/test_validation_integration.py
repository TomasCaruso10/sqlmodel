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
@pytest.mark.xfail(
    strict=True,
    reason="Upstream validation entry points do not preserve all field/post-init hooks and ORM state.",
)
def test_validation_entry_points_preserve_hooks_and_state(
    entry: str, database_engine: Engine
) -> None:
    calls: list[tuple[str, Any]] = []

    class Item(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int = Field(gt=0)
        _marker: str = PrivateAttr(default="ready")

        @field_validator("quantity")
        @classmethod
        def record_field(cls, value: int, info: ValidationInfo) -> int:
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
    assert calls == [("field", context), ("post", context), ("after", context)]
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


@pytest.mark.parametrize(
    "entry",
    [
        "python",
        pytest.param(
            "json",
            marks=pytest.mark.xfail(
                strict=True,
                reason="Upstream JSON construction bypasses strict field validation.",
            ),
        ),
        pytest.param(
            "adapter",
            marks=pytest.mark.xfail(
                strict=True,
                reason="Upstream TypeAdapter construction bypasses strict field validation.",
            ),
        ),
    ],
)
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
@pytest.mark.xfail(
    strict=True,
    reason="Upstream constructor triggers assignment validation before ceiling has its default.",
)
def test_rejected_model_assignment_does_not_publish_orm_changes(
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
        item = Item(quantity=2)
        session.add(item)
        session.flush()
        fields_set = item.model_fields_set.copy()
        if expired:
            session.expire(item)
        writes.clear()
        with pytest.raises(ValidationError):
            item.quantity = 11
        assert item.quantity == 2
        assert item.model_fields_set == fields_set
        assert item not in session.dirty
        assert not writes
        if expired:
            session.expire(item)
        item.quantity = "3"  # Runtime validation coerces before the ORM sees it.
        assert writes == [3]
        assert inspect(item).attrs.quantity.history.deleted == [2]
        session.flush()
        key = item.id
        session.expunge_all()
        loaded = session.get(Item, key)
        assert loaded is not None and loaded.quantity == 3


@pytest.mark.parametrize(
    "operation",
    [
        "copy",
        pytest.param(
            "deepcopy",
            marks=pytest.mark.xfail(
                strict=True,
                reason="Upstream deep copy does not bind ORM state to the copied instance.",
            ),
        ),
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
    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        item = Item(quantity=2)
        session.add(item)
        session.commit()
        assert (
            item.quantity == 2
        )  # Load before copying, without triggering lazy I/O later.
        initialized.clear()
        if operation == "copy":
            restored = copy.copy(item)
        elif operation == "deepcopy":
            restored = copy.deepcopy(item)
        else:
            restored = pickle.loads(pickle.dumps(item))
        assert restored is not item
        assert inspect(restored).key == inspect(item).key
        if operation in {"deepcopy", "pickle"}:
            assert inspect(restored).object is restored
        assert not initialized
        assert restored.model_dump() == item.model_dump()


@pytest.mark.parametrize("entry", ["init", "python", "json", "adapter"])
@pytest.mark.xfail(
    strict=True,
    reason="Experiment contract, not an upstream guarantee: field validation precedes post-init and external ORM init.",
)
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
@pytest.mark.xfail(
    strict=True,
    reason="Experiment contract: normalized relationships must be available to after validators across entry points.",
)
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
