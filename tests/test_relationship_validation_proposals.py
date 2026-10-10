"""Separate ORM compatibility from new relationship-validation capabilities.

The upstream baseline is SQLModel 0.0.48 (5ce1a47), before this fork's native
Pydantic validation change. The first proposal restores a constructor use case
that upstream supported by skipping validation, and extends it to explicit
validation. The second adds nested relationship parsing that upstream did not
provide. Neither test asks to restore unchecked construction in general.
"""

import json
from datetime import date
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
    ValidationInfo,
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
        ({"name": "Invalid parent", "parent": {}}, ("parent", "name"), "missing"),
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


def test_foreign_key_deferral_preserves_json_schema_type() -> None:
    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int = Field(foreign_key="parent.id")
        parent: Parent = Relationship()

    schema = Child.model_json_schema()
    assert "parent_id" not in schema.get("required", [])
    assert schema["properties"]["parent_id"]["type"] == "integer"
    with pytest.raises(ValidationError) as error:
        Child.model_validate_json("{}")
    assert error.value.errors()[0]["type"] == "missing_foreign_key"
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
    with patch("sqlmodel.main.inspect", wraps=inspect) as read_mapper:
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


@pytest.mark.parametrize("entry", ["init", "python", "json", "adapter"])
def test_nested_relationship_inputs_are_validated(
    database_engine: Engine, entry: str
) -> None:
    """Parse relationship inputs without changing ORM identity or serialization.

    Relationships are excluded from Pydantic model fields. Upstream did not
    recursively turn their dictionaries into related model instances:
    https://github.com/fastapi/sqlmodel/discussions/1927

    This proposes new behavior, not restoration of an upstream guarantee. Nested
    fields must use Pydantic's coercion, constraints, context, JSON mode, and error
    locations. Existing ORM instances keep their identity and SQLAlchemy manages
    backrefs and persistence. Relationships remain excluded from model_dump().
    An optional FK keeps this test independent of the FK-deferral proposal.
    """
    calls: list[tuple[str, Any]] = []

    class Parent(SQLModel, table=True):
        model_config = {"extra": "forbid"}
        id: int | None = Field(default=None, primary_key=True)
        name: str
        children: list["Child"] = Relationship(back_populates="parent")

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int = Field(gt=0)
        day: date
        # Keep this proposal independent of required-FK deferral.
        parent_id: int | None = Field(default=None, foreign_key="parent.id")
        parent: Parent | None = Relationship(back_populates="children")

        @field_validator("day")
        @classmethod
        def record_validation(cls, value: date, info: ValidationInfo) -> date:
            calls.append((info.mode, info.context))
            return value

    context = {"request": "relationship-test"}

    def build(data: dict[str, Any]) -> Parent:
        if entry == "init":
            return Parent(**data)
        if entry == "python":
            return Parent.model_validate(data, context=context)
        if entry == "json":
            return Parent.model_validate_json(json.dumps(data), context=context)
        return TypeAdapter(Parent).validate_python(data, context=context)

    data = {
        "name": "Parent",
        "children": [{"quantity": "2", "day": "2026-10-10"}],
    }
    parent = build(data)
    child = parent.children[0]
    assert isinstance(child, Child)
    assert child.quantity == 2
    assert child.day == date(2026, 10, 10)
    assert calls == [
        ("json" if entry == "json" else "python", None if entry == "init" else context)
    ]
    assert child.parent is parent
    assert inspect(child).object is child
    assert data == {
        "name": "Parent",
        "children": [{"quantity": "2", "day": "2026-10-10"}],
    }
    assert "children" not in parent.model_dump()

    with pytest.raises(ValidationError) as error:
        build({"name": "Parent", "children": [{"quantity": 0, "day": "2026-10-10"}]})
    assert error.value.errors()[0]["loc"] == ("children", 0, "quantity")

    # Existing ORM instances must retain their identity and ownership.
    if entry != "json":
        existing = Child(quantity=3, day=date(2026, 10, 10))
        owner = build({"name": "Existing", "children": [existing]})
        assert owner.children[0] is existing
        assert existing.parent is owner
    else:
        strict = Parent.model_validate_json(
            '{"name":"Strict","children":[{"quantity":2,"day":"2026-10-10"}]}',
            strict=True,
        )
        assert strict.children[0].day == date(2026, 10, 10)

    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        session.add(parent)
        session.flush()
        assert child.parent_id == parent.id
        child_id = child.id
        session.expunge_all()
        loaded = session.get(Child, child_id)
        assert loaded is not None and loaded.quantity == 2
        assert loaded.parent is not None and loaded.parent.name == "Parent"


@pytest.mark.parametrize("entry", ["init", "python", "json", "adapter", "attributes"])
def test_nested_relationship_supplies_required_foreign_key(
    database_engine: Engine, entry: str
) -> None:
    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        name: str = Field(min_length=1)

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int = Field(foreign_key="parent.id")
        parent: Parent = Relationship()

    def build(data: dict[str, Any]) -> Child:
        if entry == "init":
            return Child(**data)
        if entry == "json":
            return Child.model_validate_json(json.dumps(data))
        if entry == "adapter":
            return TypeAdapter(Child).validate_python(data)
        if entry == "attributes":
            return Child.model_validate(SimpleNamespace(**data))
        return Child.model_validate(data)

    data = {"parent": {"name": "Parent"}}
    child = build(data)
    assert isinstance(child.parent, Parent)
    assert child.parent_id is None
    assert data == {"parent": {"name": "Parent"}}
    assert "parent" not in Child.model_fields
    assert "parent" not in child.model_fields_set
    assert "parent" not in child.model_dump()
    assert "parent" not in Child.model_json_schema(mode="serialization")["properties"]
    assert "parent" in Child.model_json_schema(mode="validation")["properties"]
    with pytest.raises(ValidationError) as error:
        build({"parent": {"name": ""}})
    assert error.value.errors()[0]["loc"] == ("parent", "name")
    with pytest.raises(ValidationError) as error:
        build({})
    assert error.value.errors()[0]["type"] == "missing_foreign_key"

    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        session.add(child)
        session.flush()
        assert child.parent_id == child.parent.id
        assert isinstance(child.parent_id, int)


def test_nested_relationship_rejects_cyclic_input() -> None:
    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        children: list["Child"] = Relationship(back_populates="parent")

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int | None = Field(default=None, foreign_key="parent.id")
        parent: Parent | None = Relationship(back_populates="children")

    data: dict[str, Any] = {"children": [{}]}
    data["children"][0]["parent"] = data
    with pytest.raises(ValidationError) as error:
        Parent.model_validate(data)
    assert error.value.errors()[0]["type"] == "recursion_loop"
    assert error.value.errors()[0]["loc"] == ("children", 0, "parent")


@pytest.mark.parametrize("keyed", [False, True])
def test_nested_relationship_with_custom_collection(keyed: bool) -> None:
    from sqlalchemy.orm import attribute_keyed_dict, relationship

    class ChildList(list):
        pass

    factory = attribute_keyed_dict("name") if keyed else lambda: ChildList()

    class Parent(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        children: list["Child"] = Relationship(
            sa_relationship=relationship("Child", collection_class=factory)
        )

    class Child(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        parent_id: int | None = Field(default=None, foreign_key="parent.id")
        name: str
        quantity: int = Field(gt=0)

    item = {"name": "child", "quantity": "2"}
    children = {"child": item} if keyed else [item]
    parent = Parent(children=children)
    child = parent.children["child"] if keyed else parent.children[0]
    assert isinstance(child, Child)
    assert child.quantity == 2
    assert isinstance(parent.children, dict if keyed else ChildList)
    assert children == ({"child": item} if keyed else [item])
