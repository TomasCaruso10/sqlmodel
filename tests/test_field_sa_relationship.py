from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import relationship
from sqlmodel import Field, Relationship, SQLModel


def test_sa_relationship_no_args() -> None:
    with pytest.raises(RuntimeError):  # pragma: no cover

        class Team(SQLModel, table=True):
            id: int | None = Field(default=None, primary_key=True)
            name: str = Field(index=True)
            headquarters: str

            heroes: list["Hero"] = Relationship(
                back_populates="team",
                sa_relationship_args=["Hero"],
                sa_relationship=relationship("Hero", back_populates="team"),
            )

        class Hero(SQLModel, table=True):
            id: int | None = Field(default=None, primary_key=True)
            name: str = Field(index=True)
            secret_name: str
            age: int | None = Field(default=None, index=True)

            team_id: int | None = Field(default=None, foreign_key="team.id")
            team: Team | None = Relationship(back_populates="heroes")


def test_sa_relationship_no_kwargs() -> None:
    with pytest.raises(RuntimeError):  # pragma: no cover

        class Team(SQLModel, table=True):
            id: int | None = Field(default=None, primary_key=True)
            name: str = Field(index=True)
            headquarters: str

            heroes: list["Hero"] = Relationship(
                back_populates="team",
                sa_relationship_kwargs={"lazy": "selectin"},
                sa_relationship=relationship("Hero", back_populates="team"),
            )

        class Hero(SQLModel, table=True):
            id: int | None = Field(default=None, primary_key=True)
            name: str = Field(index=True)
            secret_name: str
            age: int | None = Field(default=None, index=True)

            team_id: int | None = Field(default=None, foreign_key="team.id")
            team: Team | None = Relationship(back_populates="heroes")


@pytest.mark.parametrize(
    "options",
    [
        {"back_populates": 123},
        {"cascade_delete": "false"},
        {"cascade_delete": 1},
        {"passive_deletes": "false"},
        {"sa_relationship": object()},
        {"sa_relationship_args": 123},
        {"sa_relationship_kwargs": {1: "selectin"}},
    ],
)
def test_relationship_rejects_invalid_option_types(options: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Relationship(**options)


def test_relationship_preserves_native_objects() -> None:
    native_relationship = relationship("Hero")
    info = Relationship(sa_relationship=native_relationship)
    assert info.sa_relationship is native_relationship

    target = object()
    info = Relationship(
        cascade_delete=False,
        passive_deletes="all",
        sa_relationship_args=(target,),
        sa_relationship_kwargs={"primaryjoin": target},
    )
    assert info.cascade_delete is False
    assert info.passive_deletes == "all"
    assert info.sa_relationship_args[0] is target
    assert info.sa_relationship_kwargs["primaryjoin"] is target


@pytest.mark.parametrize("option", ["sa_relationship_args", "sa_relationship_kwargs"])
def test_relationship_conflict_preserves_error(option: str) -> None:
    options = {option: [] if option == "sa_relationship_args" else {}}
    with pytest.raises(RuntimeError) as error:
        Relationship(sa_relationship=relationship("Hero"), **options)
    assert str(error.value) == (
        f"Passing {option} is not supported when also passing a sa_relationship"
    )
