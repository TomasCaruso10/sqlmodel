"""Preserve relationship-driven FK assignment with native Pydantic validation.

Upstream SQLModel accepts related ORM objects without their required foreign
keys during construction. These tests retain that behavior and extend it to
explicit validation, without allowing unrelated required fields to be omitted.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import (
    JSON,
    Column,
    Engine,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    inspect,
)
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, Relationship, Session, SQLModel


@pytest.mark.parametrize("entry", ["init", "python", "adapter"])
@pytest.mark.parametrize("persisted_parent", [False, True])
def test_relationship_supplies_required_foreign_key(
    database_engine: Engine, entry: str, persisted_parent: bool
) -> None:
    """Allow relationship-driven inserts while retaining field validation.

    Upstream accepts Child(name=..., parent=...) and SQLAlchemy fills parent_id
    during flush. Its model_validate path instead raises a missing-field error:
    https://github.com/fastapi/sqlmodel/discussions/785

    The parent's ID may not exist yet, so copying an existing ID into the input
    is insufficient. Only an omitted FK backed by a supplied, valid relationship
    may be deferred; missing ordinary fields and explicit invalid FKs still fail.
    """

    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        children: list["Child"] = Relationship(back_populates="parent")

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        name: str
        parent_id: int = Field(foreign_key="parent.id")
        parent: Parent = Relationship(back_populates="children")

    def build(data: dict[str, Any]) -> Child:
        if entry == "init":
            return Child(**data)
        if entry == "python":
            return Child.model_validate(data)
        return TypeAdapter(Child).validate_python(data)

    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        parent = Parent(name="Parent")
        if persisted_parent:
            session.add(parent)
            session.flush()
        child = build({"name": "Child", "parent": parent})
        assert child.parent is parent
        assert child.parent_id is None
        assert "parent_id" not in child.model_fields_set
        session.add(child)
        assert any(item is child for item in parent.children)
        session.flush()
        assert isinstance(parent.id, int)
        assert child.parent_id == parent.id
        child_id = child.id
        session.expunge_all()
        loaded = session.get(Child, child_id)
        assert loaded is not None
        assert loaded.parent.name == "Parent"

    # Deferral must not relax unrelated requirements or explicit invalid values.
    for data, location, error_type in [
        ({"name": "Missing parent"}, (), "missing_foreign_key"),
        ({"name": "Null parent", "parent": None}, (), "missing_foreign_key"),
        ({"name": "Invalid parent", "parent": {}}, (), "missing_foreign_key"),
        ({"parent": Parent(name="Parent")}, ("name",), "missing"),
        (
            {"name": "Invalid FK", "parent": Parent(name="Parent"), "parent_id": "bad"},
            ("parent_id",),
            "int_parsing",
        ),
        (
            {"name": "Null FK", "parent": Parent(name="Parent"), "parent_id": None},
            ("parent_id",),
            "int_type",
        ),
    ]:
        with pytest.raises(ValidationError) as error:
            build(data)
        assert [(item["loc"], item["type"]) for item in error.value.errors()] == [
            (location, error_type)
        ]


def test_viewonly_relationship_does_not_supply_required_foreign_key() -> None:
    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int = Field(foreign_key="parent.id")
        parent: Parent = Relationship(sa_relationship_kwargs={"viewonly": True})

    with pytest.raises(ValidationError) as error:
        Child(parent=Parent(id=1))
    assert error.value.errors()[0]["loc"] == ()
    assert error.value.errors()[0]["type"] == "missing_foreign_key"
    assert error.value.errors()[0]["ctx"] == {"fields": "parent_id"}


def test_foreign_key_deferral_preserves_json_schema_requirement() -> None:
    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int = Field(foreign_key="parent.id")
        parent: Parent = Relationship()

    assert "parent_id" in Child.model_json_schema().get("required", [])
    assert "default" not in Child.model_json_schema()["properties"]["parent_id"]
    with pytest.raises(ValidationError) as error:
        Child.model_validate_json("{}")
    assert error.value.errors()[0]["type"] == "missing"
    assert Child.model_validate_json('{"parent_id": 1}').parent_id == 1


@pytest.mark.parametrize("explicit_column", [False, True])
def test_foreign_key_deferral_preserves_database_definition(
    database_engine: Engine, explicit_column: bool
) -> None:
    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)

    class Child(SQLModel, table=True):
        model_config = {"validate_default": True}
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int = (
            Field(
                sa_column=Column(Integer, ForeignKey("parent.id"), nullable=False), gt=0
            )
            if explicit_column
            else Field(foreign_key="parent.id", gt=0)
        )
        parent: Parent = Relationship()

    field = Child.model_fields["parent_id"]
    column = inspect(Child).columns.parent_id
    assert field.annotation is int
    assert field.is_required()
    assert not column.nullable
    assert column.default is None
    assert Child(parent=Parent()).parent_id is None
    for value in [None, 0, "invalid"]:
        with pytest.raises(ValidationError):
            Child(parent=Parent(), parent_id=value)
    assert Child(parent_id="2").parent_id == 2

    SQLModel.metadata.create_all(database_engine)
    with database_engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(inspect(Child).local_table.insert().values())


def test_relationship_only_supplies_its_own_foreign_key() -> None:
    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        children: list["Child"] = Relationship(
            back_populates="parent",
            sa_relationship_kwargs={"foreign_keys": "Child.parent_id"},
        )

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int = Field(foreign_key="parent.id")
        other_id: int = Field(foreign_key="parent.id")
        parent: Parent = Relationship(
            back_populates="children",
            sa_relationship_kwargs={"foreign_keys": "Child.parent_id"},
        )

    parent = Parent()
    assert parent.children == []
    with pytest.raises(ValidationError) as error:
        Child(parent=parent)
    assert [(item["loc"], item["type"]) for item in error.value.errors()] == [
        ((), "missing_foreign_key")
    ]
    assert error.value.errors()[0]["msg"] == (
        "Provide values or relationships for required foreign keys: other_id"
    )
    assert parent.children == []

    # Pydantic locates a model-level error within the enclosing payload.
    class Payload(BaseModel):
        child: Child

    with pytest.raises(ValidationError) as error:
        Payload.model_validate({"child": {"parent": parent}})
    assert error.value.errors()[0]["loc"] == ("child",)
    assert error.value.errors()[0]["type"] == "missing_foreign_key"
    assert parent.children == []

    payload = Payload.model_validate({"child": {"parent": parent, "other_id": 3}})
    assert payload.child.parent is parent
    assert payload.child.parent_id is None
    assert any(child is payload.child for child in parent.children)

    child = Child(parent=Parent(), other_id=3)
    assert child.parent_id is None
    assert child.other_id == 3


@pytest.mark.parametrize("attributes", [False, True])
@pytest.mark.parametrize("alias", ["owner_id", AliasChoices("owner_id", "owner_key")])
def test_pending_foreign_key_respects_aliases_and_input(
    attributes: bool, alias: Any
) -> None:
    calls = []

    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)

    class Child(SQLModel, table=True):
        model_config = {"extra": "forbid"}
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int = Field(foreign_key="parent.id", validation_alias=alias)
        parent: Parent = Relationship()

        @field_validator("parent_id", mode="before")
        @classmethod
        def record(cls, value: Any) -> Any:
            calls.append(value)
            return value

    def build(data: dict[str, Any]) -> Child:
        value = SimpleNamespace(**data) if attributes else data
        return TypeAdapter(Child).validate_python(value)

    parent = Parent()
    data = {"parent": parent}
    child = build(data)
    assert child.parent is parent
    assert child.parent_id is None
    assert data == {"parent": parent}
    assert not calls
    # Runtime alias selection must not introduce extra input keys.
    for by_alias, by_name in [(True, False), (False, True), (True, True)]:
        pending = TypeAdapter(Child).validate_python(
            data, by_alias=by_alias, by_name=by_name
        )
        assert pending.parent_id is None
        assert pending.model_extra is None

    keys = alias.choices if isinstance(alias, AliasChoices) else [alias]
    for key in keys:
        assert build({"parent": parent, key: "42"}).parent_id == 42
        with pytest.raises(ValidationError) as error:
            build({"parent": parent, key: "bad"})
        assert error.value.errors()[0]["type"] == "int_parsing"
    assert calls == ["42", "bad"] * len(keys)


def test_pending_foreign_key_uses_input_after_model_before_validator() -> None:
    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int = Field(foreign_key="parent.id")
        parent: Parent = Relationship()

        @model_validator(mode="before")
        @classmethod
        def rename_relationship(cls, value: Any) -> Any:
            return {"parent": value["owner"]}

    parent = Parent()
    child = Child(owner=parent)
    assert child.parent is parent
    assert child.parent_id is None


def test_pending_foreign_key_with_alias_disabled() -> None:
    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)

    class Child(SQLModel, table=True):
        model_config = {
            "extra": "forbid",
            "validate_by_alias": False,
            "validate_by_name": True,
        }
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int = Field(foreign_key="parent.id", validation_alias="owner_id")
        parent: Parent = Relationship()

    parent = Parent()
    child = Child(parent=parent)
    assert child.parent is parent
    assert child.parent_id is None
    assert child.model_extra is None
    assert Child(parent=parent, parent_id="42").parent_id == 42
    with pytest.raises(ValidationError) as error:
        Child(parent=parent, unexpected=1)
    assert error.value.errors()[0]["type"] == "extra_forbidden"
    assert error.value.errors()[0]["loc"] == ("unexpected",)


def test_relationship_defers_composite_foreign_key(database_engine: Engine) -> None:
    class Parent(SQLModel, table=True):
        region: int = Field(primary_key=True)
        number: int = Field(primary_key=True)

    class Child(SQLModel, table=True):
        __table_args__ = (
            ForeignKeyConstraint(
                ["region", "number"], ["parent.region", "parent.number"]
            ),
        )
        id: int | None = Field(default=None, primary_key=True)
        region: int
        number: int
        parent: Parent = Relationship()

    parent = Parent(region=1, number=2)
    child = Child(parent=parent)
    assert child.region is None
    assert child.number is None
    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        session.add(child)
        session.flush()
        assert (child.region, child.number) == (1, 2)


def test_foreign_key_rules_are_reused_and_refreshed_on_schema_rebuild() -> None:
    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int = Field(foreign_key="parent.id")
        parent: Parent = Relationship()

    class OtherChild(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        owner_id: int = Field(foreign_key="parent.id")
        owner: Parent = Relationship()

    parent = Parent()
    with patch(
        "sqlmodel._relationship_validation.inspect", wraps=inspect
    ) as read_mapper:
        assert Child(parent=parent).parent is parent
        assert Child.model_validate({"parent": parent}).parent is parent
        assert read_mapper.call_count == 1

        # Preparing rules must not preserve any decision from a previous input.
        with pytest.raises(ValidationError):
            Child(parent=None)
        with pytest.raises(ValidationError):
            Child(parent=parent, parent_id="bad")
        assert read_mapper.call_count == 1

        # Each model owns its rules, even when it targets the same parent table.
        assert OtherChild(owner=parent).owner is parent
        assert read_mapper.call_count == 2

        Child.model_fields["parent_id"].validation_alias = "customer_id"
        Child.model_rebuild(force=True)
        assert Child(parent=parent).parent is parent
        assert Child(parent=parent, customer_id="42").parent_id == 42
        assert read_mapper.call_count == 3


def test_pending_foreign_key_in_strict_mode() -> None:
    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int = Field(foreign_key="parent.id")
        parent: Parent = Relationship()

    child = Child.model_validate({"parent": Parent()}, strict=True)
    assert child.parent_id is None
    with pytest.raises(ValidationError):
        Child.model_validate({"parent": Parent(), "parent_id": "1"}, strict=True)


def test_pending_foreign_key_with_alias_path() -> None:
    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int = Field(
            foreign_key="parent.id",
            schema_extra={"validation_alias": AliasPath("owner", "id")},
        )
        parent: Parent = Relationship()

    data = {"parent": Parent(), "owner": {}}
    child = Child.model_validate(data)
    assert child.parent_id is None
    assert data["owner"] == {}


def test_pending_foreign_key_alias_path_preserves_shared_input() -> None:
    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int = Field(
            foreign_key="parent.id", validation_alias=AliasPath("owner", "id")
        )
        owner: dict[str, int] = Field(sa_column=Column(JSON))
        parent: Parent = Relationship()

    owner = {"other": 123}
    child = Child(parent=Parent(), owner=owner)
    assert child.parent_id is None
    assert child.owner == {"other": 123}
    assert owner == {"other": 123}
    with pytest.raises(ValidationError) as error:
        Child(parent=Parent(), owner={"other": "invalid"})
    assert any(item["loc"] == ("owner", "other") for item in error.value.errors())
