"""Declare ORM discriminators on fields and retain native inheritance behavior."""

import json
from typing import Annotated, Any

import pytest
from pydantic import TypeAdapter
from sqlalchemy import Engine, inspect
from sqlmodel import Field, Session, SQLModel, select


@pytest.mark.parametrize(
    "entry", ["init", "python", "json", "adapter", "adapter_json", "construct"]
)
def test_inferred_identity_round_trip(database_engine: Engine, entry: str) -> None:
    class Resource(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        kind: str = Field(polymorphic=True, polymorphic_identity="resource")
        name: str

    class Webhook(Resource, table=True):
        __tablename__ = "webhook"
        resource_id: int | None = Field(
            default=None, primary_key=True, foreign_key="resource.id"
        )
        path: str

    data = {"name": "Orders", "path": "/orders"}
    constructors = {
        "init": lambda: Webhook(**data),
        "python": lambda: Webhook.model_validate(data),
        "json": lambda: Webhook.model_validate_json(json.dumps(data)),
        "adapter": lambda: TypeAdapter(Webhook).validate_python(data),
        "adapter_json": lambda: TypeAdapter(Webhook).validate_json(json.dumps(data)),
        "construct": lambda: Webhook.model_construct(**data),
    }
    webhook = constructors[entry]()
    assert webhook.kind == "webhook"
    assert Resource(name="Root").kind == "resource"
    assert Resource.model_fields["kind"].default == "resource"
    assert Webhook.model_json_schema()["properties"]["kind"]["default"] == "webhook"
    assert "kind" not in Webhook.__table__.c
    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        session.add(webhook)
        session.add(Resource(name="Root"))
        session.commit()
    with Session(database_engine) as session:
        rows = session.exec(select(Resource).order_by(Resource.id)).all()
        assert [type(row) for row in rows] == [Webhook, Resource]
        assert rows[0].path == "/orders"


def test_explicit_field_identity_and_native_override(database_engine: Engine) -> None:
    class Resource(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        kind: str = Field(polymorphic=True)

    # Explicitly redeclaring a mapped field retains Pydantic's ordinary warning.
    with pytest.warns(UserWarning, match='Field name "kind" .* shadows an attribute'):

        class RenamedWebhook(Resource, table=True):
            __tablename__ = "webhook"
            resource_id: int | None = Field(
                default=None, primary_key=True, foreign_key="resource.id"
            )
            kind: str = Field(polymorphic_identity="webhook")

    class SignedWebhook(RenamedWebhook, table=True):
        __tablename__ = "signed_webhook"
        webhook_id: int | None = Field(
            default=None, primary_key=True, foreign_key="webhook.resource_id"
        )
        __mapper_args__ = {"polymorphic_identity": "signed", "eager_defaults": True}

    assert RenamedWebhook().kind == "webhook"
    assert SignedWebhook().kind == "signed"
    assert inspect(SignedWebhook).eager_defaults is True
    assert set(SignedWebhook.__table__.c.keys()) == {"webhook_id"}
    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        session.add_all([RenamedWebhook(), SignedWebhook()])
        session.commit()
    with Session(database_engine) as session:
        rows = session.exec(select(Resource).order_by(Resource.id)).all()
        assert [type(row) for row in rows] == [RenamedWebhook, SignedWebhook]


def test_discriminator_inherited_from_data_model() -> None:
    class ResourceData(SQLModel):
        kind: str = Field(polymorphic=True, polymorphic_identity="resource")

    class Resource(ResourceData, table=True):
        id: int | None = Field(default=None, primary_key=True)

    assert Resource().kind == "resource"
    assert inspect(Resource).polymorphic_on is Resource.__table__.c.kind
    assert ResourceData.model_fields["kind"].is_required()


def test_annotated_discriminator_and_native_precedence() -> None:
    class Resource(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        kind: Annotated[str, Field(polymorphic=True, polymorphic_identity="field")]
        __mapper_args__ = {"polymorphic_identity": "native"}

    assert Resource().kind == "native"
    assert inspect(Resource).polymorphic_identity == "native"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"polymorphic": True}, "Only one field"),
        ({"polymorphic_identity": "wrong"}, "belongs on the discriminator"),
    ],
)
def test_invalid_discriminator_declarations(
    options: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):

        class Resource(SQLModel, table=True):
            id: int | None = Field(default=None, primary_key=True)
            kind: str = Field(polymorphic=True)
            other: str = Field(**options)
