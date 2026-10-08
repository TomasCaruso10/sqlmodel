"""Construction regressions across Pydantic validation and ORM instrumentation."""

from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import AliasPath, ValidationError, model_validator
from pydantic import Field as PydanticField
from sqlalchemy import event, inspect
from sqlmodel import Field, SQLModel
from sqlmodel._compat import finish_init
from typing_extensions import Self


@pytest.fixture(autouse=True)
def isolate_initialization_context() -> Iterator[None]:
    """Keep the known partial_init leak from affecting unrelated tests."""
    token = finish_init.set(True)
    try:
        yield
    finally:
        finish_init.reset(token)


@pytest.mark.xfail(
    strict=True,
    reason="Proposed contract: upstream table constructors allow missing required fields.",
)
def test_table_constructor_requires_declared_fields() -> None:
    class Item(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    with pytest.raises(ValidationError) as captured:
        Item()

    assert captured.value.errors()[0]["loc"] == ("name",)
    assert captured.value.errors()[0]["type"] == "missing"


@pytest.mark.xfail(
    strict=True,
    reason="Upstream partial_init does not reset finish_init when construction raises.",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="Upstream partial_init also suppresses constructors called inside init listeners.",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="Upstream table construction does not resolve Pydantic AliasPath.",
)
def test_table_constructor_resolves_alias_path() -> None:
    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        name: str = PydanticField(validation_alias=AliasPath("customer", "name"))

    order = Order(customer={"name": "Ana"})

    assert order.model_dump() == {"id": None, "name": "Ana"}


@pytest.mark.xfail(
    strict=True,
    reason="Upstream table construction does not supply validated_data to default factories.",
)
def test_table_constructor_supplies_earlier_fields_to_default_factory() -> None:
    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int
        label: str = PydanticField(default_factory=lambda data: f"q{data['quantity']}")

    order = Order(quantity=2)

    assert order.label == "q2"


@pytest.mark.xfail(
    strict=True,
    reason="Upstream model_construct does not initialize SQLAlchemy instance state.",
)
def test_model_construct_returns_an_instrumented_table_instance() -> None:
    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int

    order = Order.model_construct(quantity=2)
    order.quantity = 3

    assert order.quantity == 3
    assert inspect(order) is not None


@pytest.mark.xfail(
    strict=True,
    reason="Upstream model_construct runs post-init without prepared ORM instrumentation.",
)
def test_model_construct_allows_post_init_to_modify_mapped_fields() -> None:
    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int

        def model_post_init(self, context: Any) -> None:
            self.quantity += 1

    order = Order.model_construct(quantity=2)

    assert order.quantity == 3
    assert inspect(order) is not None


@pytest.mark.xfail(
    strict=True,
    reason="Upstream model_validate restores ORM state only after post-init.",
)
def test_model_validate_allows_post_init_to_modify_mapped_fields() -> None:
    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int

        def model_post_init(self, context: Any) -> None:
            self.quantity += 1

    order = Order.model_validate({"quantity": 2})

    assert order.quantity == 3
    assert inspect(order) is not None


@pytest.mark.xfail(
    strict=True,
    reason="Upstream model_validate restores ORM state only after model after validators.",
)
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
