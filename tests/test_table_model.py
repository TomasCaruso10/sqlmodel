"""Keep public SQLModel behavior when selecting the internal table base."""

from copy import deepcopy
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import event, inspect
from sqlalchemy.orm import configure_mappers
from sqlmodel import Field, SQLModel
from sqlmodel._compat import SQLModelConfig
from sqlmodel._construction import InstanceDictProxy
from sqlmodel._table_model import TableMixin
from sqlmodel.main import SQLModelMetaclass


def test_plain_models_use_native_pydantic_construction() -> None:
    class Input(SQLModel):
        quantity: int = Field(gt=0)

    descriptor = next(
        vars(base)["__dict__"] for base in Input.__mro__ if "__dict__" in vars(base)
    )
    assert not isinstance(descriptor, InstanceDictProxy)
    assert not issubclass(Input, TableMixin)
    assert not issubclass(TableMixin, BaseModel)
    assert "model_config" not in vars(TableMixin)
    assert Input.__init__ is BaseModel.__init__
    assert Input.__deepcopy__ is BaseModel.__deepcopy__
    assert inspect(Input(quantity=1), raiseerr=False) is None
    with pytest.raises(ValidationError):
        Input(quantity=0)


@pytest.mark.parametrize("entry", ["init", "python", "json", "adapter", "construct"])
def test_table_preserves_inherited_post_init(entry: str) -> None:
    calls = []

    class Input(SQLModel):
        quantity: int

        def model_post_init(self, context: Any) -> None:
            assert inspect(self, raiseerr=False) is not None
            calls.append(self.quantity)

    class Order(Input, table=True):
        id: int | None = Field(default=None, primary_key=True)

    if entry == "init":
        order = Order(quantity=2)
    elif entry == "python":
        order = Order.model_validate({"quantity": 2})
    elif entry == "json":
        order = Order.model_validate_json('{"quantity": 2}')
    elif entry == "adapter":
        order = TypeAdapter(Order).validate_python({"quantity": 2})
    else:
        order = Order.model_construct(quantity=2)

    assert calls == [2]
    assert isinstance(order, SQLModel)
    assert order.model_dump() == {"quantity": 2, "id": None}
    assert Order.__mro__.index(Input) < Order.__mro__.index(TableMixin)
    descriptor = next(
        vars(base)["__dict__"] for base in Order.__mro__ if "__dict__" in vars(base)
    )
    assert isinstance(descriptor, InstanceDictProxy)


def test_table_preserves_user_assignment_and_config() -> None:
    class Input(SQLModel):
        model_config = SQLModelConfig(from_attributes=False, validate_assignment=True)
        quantity: int = Field(gt=0)

        def __setattr__(self, name: str, value: Any) -> None:
            if name == "quantity":
                value *= 2
            super().__setattr__(name, value)

    class Order(Input, table=True):
        id: int | None = Field(default=None, primary_key=True)

    assert Order.model_config["from_attributes"] is False
    assert Order.model_config["validate_assignment"] is True
    values = []
    event.listen(
        Order.quantity, "set", lambda target, value, old, token: values.append(value)
    )
    order = Order(quantity=2)
    values.clear()
    order.quantity = 3
    assert order.quantity == 6
    assert values == [6]
    with pytest.raises(ValidationError):
        order.quantity = -1
    assert order.quantity == 6
    assert values == [6]


@pytest.mark.parametrize("table", [False, True])
def test_model_config_table_takes_precedence_over_keyword(table: bool) -> None:
    class Item(SQLModel, table=not table):
        model_config = SQLModelConfig(table=table)
        id: int | None = Field(default=None, primary_key=True)

    assert issubclass(Item, TableMixin) is table
    assert (inspect(Item(), raiseerr=False) is not None) is table


def test_table_uses_custom_registry() -> None:
    from sqlalchemy.orm import registry

    custom_registry = registry()

    class Base(SQLModel, registry=custom_registry):
        name: str

    class Item(Base, table=True):
        id: int | None = Field(default=None, primary_key=True)

    assert Item.metadata is custom_registry.metadata
    assert inspect(Item).registry is custom_registry
    assert Item(name="one").model_dump() == {"name": "one", "id": None}
    custom_registry.dispose()


@pytest.mark.parametrize("table", [False, True])
def test_model_preserves_user_metaclass(table: bool) -> None:
    initialized = []
    assigned = []

    class UserMetaclass(SQLModelMetaclass):
        def __init__(cls, name, bases, namespace, **kwargs):
            super().__init__(name, bases, namespace, **kwargs)
            initialized.append(name)

        def __setattr__(cls, name, value):
            assigned.append(name)
            super().__setattr__(name, value)

    class Base(SQLModel, metaclass=UserMetaclass):
        marker: ClassVar[str]
        quantity: int = Field(gt=0)

    class Item(Base, table=table):
        id: int | None = Field(default=None, primary_key=True)

    assert type(Item) is UserMetaclass
    assert initialized == ["Base", "Item"]
    Item.marker = "custom"
    assert assigned[-1] == "marker"
    assert Item.marker == "custom"
    del Item.marker
    assert not hasattr(Item, "marker")
    assert (inspect(Item, raiseerr=False) is not None) is table
    item = Item.model_validate({"quantity": "2"})
    assert item.quantity == 2
    with pytest.raises(ValidationError):
        Item(quantity=0)


@pytest.mark.parametrize("validate_assignment", [False, True])
@pytest.mark.parametrize(
    "entry", ["init", "python", "json", "adapter", "adapter_json", "construct"]
)
def test_descendant_can_disable_table(entry: str, validate_assignment: bool) -> None:
    calls = []

    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int = Field(gt=0)

        def model_post_init(self, context: Any) -> None:
            calls.append(type(self).__name__)

    class OrderData(Order):
        model_config = SQLModelConfig(
            table=False, validate_assignment=validate_assignment
        )

    # The descendant still inherits native descriptors from Order, as it did
    # before separating table behavior. Resolve them through SQLAlchemy.
    configure_mappers()
    data = {"id": 1, "quantity": 2}
    if entry == "init":
        order = OrderData(**data)
    elif entry == "python":
        order = OrderData.model_validate(data)
    elif entry == "json":
        order = OrderData.model_validate_json('{"id": 1, "quantity": 2}')
    elif entry == "adapter":
        order = TypeAdapter(OrderData).validate_python(data)
    elif entry == "adapter_json":
        order = TypeAdapter(OrderData).validate_json('{"id": 1, "quantity": 2}')
    else:
        order = OrderData.model_construct(id=1, quantity=2)

    assert OrderData.model_config["table"] is False
    assert Order.model_config["table"] is True
    assert "__mapper__" not in vars(OrderData)
    assert set(SQLModel.metadata.tables) == {"order"}
    assert calls == ["OrderData"]
    assert order.model_dump() == data
    # Preserve the existing distinction: native construction inherits Order's
    # instrumented initializer, but model_construct does not run it.
    assert (inspect(order, raiseerr=False) is not None) is (entry != "construct")
    order.quantity = 3
    assert order.quantity == 3
    copied = deepcopy(order)
    assert copied is not order
    assert copied.model_dump() == {"id": 1, "quantity": 3}
    if validate_assignment:
        with pytest.raises(ValidationError):
            order.quantity = "invalid"  # ty: ignore[invalid-assignment]
        assert order.quantity == 3


def test_table_class_assignments_keep_native_column_mapping() -> None:
    from sqlalchemy import Column, Integer

    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)

    column = Column(Integer)
    Order.quantity = column
    assert inspect(Order).columns.quantity is column
    assert SQLModel.metadata.tables["order"].c.quantity is column
