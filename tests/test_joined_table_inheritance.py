"""Persist specialized SQLModel resources through native joined inheritance."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError, create_model
from sqlalchemy import Engine, inspect
from sqlalchemy.exc import NoForeignKeysError
from sqlmodel import Field, Relationship, Session, SQLModel, select

# Inheriting a field must not be reported as redeclaring it.
pytestmark = pytest.mark.filterwarnings(
    "error:Field name .* shadows an attribute in parent:UserWarning"
)


@pytest.fixture
def models() -> tuple[type[SQLModel], type[SQLModel]]:
    class Resource(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        kind: str
        name: str = Field(min_length=2)
        enabled: bool = True
        __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "resource"}

    class Webhook(Resource, table=True):
        __tablename__ = "webhook"
        resource_id: int | None = Field(
            default=None, primary_key=True, foreign_key="resource.id"
        )
        path: str = Field(min_length=2)
        __mapper_args__ = {"polymorphic_identity": "webhook"}

    return Resource, Webhook


@pytest.mark.parametrize(
    "entry", ["init", "python", "json", "adapter", "adapter_json", "construct"]
)
def test_joined_resource_round_trip(
    models: tuple[type[SQLModel], type[SQLModel]], database_engine: Engine, entry: str
) -> None:
    Resource, Webhook = models
    data = {"kind": "webhook", "name": "Orders", "path": "/orders"}
    constructors = {
        "init": lambda: Webhook(**data),
        "python": lambda: Webhook.model_validate(data),
        "json": lambda: Webhook.model_validate_json(
            '{"kind":"webhook","name":"Orders","path":"/orders"}'
        ),
        "adapter": lambda: TypeAdapter(Webhook).validate_python(data),
        "adapter_json": lambda: TypeAdapter(Webhook).validate_json(
            '{"kind":"webhook","name":"Orders","path":"/orders"}'
        ),
        "construct": lambda: Webhook.model_construct(**data),
    }
    instance = constructors[entry]()
    assert instance.enabled is True
    assert inspect(instance).mapper is inspect(Webhook)
    assert set(Webhook.__table__.c.keys()) == {"resource_id", "path"}
    assert inspect(Webhook).inherits is inspect(Resource)
    SQLModel.metadata.create_all(database_engine)

    with Session(database_engine) as session:
        session.add(instance)
        session.flush()
        identity = instance.id
        assert instance.resource_id == identity
        assert len(session.execute(select(Resource.__table__)).all()) == 1
        assert len(session.execute(select(Webhook.__table__)).all()) == 1
        session.commit()

    with Session(database_engine) as session:
        loaded = session.exec(select(Resource)).one()
        assert type(loaded) is Webhook
        assert loaded.path == "/orders"
        assert loaded.name == "Orders"
        loaded.name = "Updated"
        loaded.path = "/updated"
        session.commit()

    with Session(database_engine) as session:
        loaded = session.get(Webhook, identity)
        assert loaded.name == "Updated"
        assert loaded.path == "/updated"
        session.delete(loaded)
        session.commit()
        assert session.execute(select(Resource.__table__)).all() == []
        assert session.execute(select(Webhook.__table__)).all() == []


def test_inherited_validation_and_defaults(
    models: tuple[type[SQLModel], type[SQLModel]],
) -> None:
    Resource, Webhook = models
    assert Resource.model_fields["name"].is_required()
    assert Webhook.model_fields["name"].is_required()
    assert Webhook.model_fields["enabled"].default is True
    with pytest.raises(ValidationError) as missing:
        Webhook(path="/orders")
    assert {error["loc"] for error in missing.value.errors()} == {("kind",), ("name",)}
    with pytest.raises(ValidationError):
        Webhook(kind="webhook", name="x", path="/orders")
    assert Resource.model_fields["name"].default is Webhook.model_fields["name"].default


def test_multilevel_and_inherited_relationship(database_engine: Engine) -> None:
    class Project(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        name: str

    class Resource(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        project_id: int | None = Field(default=None, foreign_key="project.id")
        project: Project | None = Relationship()

    class Webhook(Resource, table=True):
        __tablename__ = "webhook"
        resource_id: int | None = Field(
            default=None, primary_key=True, foreign_key="resource.id"
        )
        path: str

    class SignedWebhook(Webhook, table=True):
        __tablename__ = "signed_webhook"
        webhook_id: int | None = Field(
            default=None, primary_key=True, foreign_key="webhook.resource_id"
        )
        header: str

    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        project = Project(name="Orders")
        webhook = SignedWebhook.model_validate(
            {"path": "/orders", "header": "X-Signature", "project": project}
        )
        assert webhook.project is project
        session.add(webhook)
        session.commit()
        identity = webhook.id
    with Session(database_engine) as session:
        webhook = session.get(SignedWebhook, identity)
        assert webhook.project.name == "Orders"
        assert webhook.id == webhook.resource_id == webhook.webhook_id
        assert set(SignedWebhook.__table__.c.keys()) == {"webhook_id", "header"}


def test_response_descendants_do_not_create_tables(
    models: tuple[type[SQLModel], type[SQLModel]],
) -> None:
    _, Webhook = models

    class WebhookData(Webhook):
        model_config = {"table": False}

    clone = create_model("WebhookClone", __base__=Webhook)
    assert "__mapper__" not in vars(WebhookData)
    assert "__mapper__" not in vars(clone)
    assert set(SQLModel.metadata.tables) == {"resource", "webhook"}
    app = FastAPI()

    @app.get("/webhook", response_model=Webhook)
    def read() -> Any:
        return Webhook(kind="webhook", name="Orders", path="/orders")

    with TestClient(app) as client:
        response = client.get("/webhook")
    assert response.status_code == 200
    assert response.json()["path"] == "/orders"
    assert set(SQLModel.metadata.tables) == {"resource", "webhook"}


def test_sqlalchemy_rejects_child_without_parent_foreign_key() -> None:
    class Resource(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)

    with pytest.raises(NoForeignKeysError):

        class Webhook(Resource, table=True):
            __tablename__ = "webhook"
            webhook_id: int | None = Field(default=None, primary_key=True)
            path: str


def test_redeclared_primary_key_and_inherited_factory(database_engine: Engine) -> None:
    class Resource(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        name: str = Field(default_factory=lambda: "generated")

    # A deliberate redeclaration still receives Pydantic's normal warning.
    with pytest.warns(UserWarning, match='Field name "id" .* shadows an attribute'):

        class Webhook(Resource, table=True):
            __tablename__ = "webhook"
            id: int | None = Field(
                default=None, primary_key=True, foreign_key="resource.id"
            )
            path: str

    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        instance = Webhook(path="/orders")
        assert instance.name == "generated"
        session.add(instance)
        session.commit()
        identity = instance.id
    with Session(database_engine) as session:
        loaded = session.get(Webhook, identity)
        assert loaded.path == "/orders"
        assert loaded.name == "generated"
        assert set(Webhook.__table__.c.keys()) == {"id", "path"}
