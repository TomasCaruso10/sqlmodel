from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, overload

from sqlalchemy.orm import RelationshipProperty

from ._compat import Representation

if TYPE_CHECKING:
    from pydantic._internal._repr import Representation as Representation


class RelationshipInfo(Representation):
    def __init__(
        self,
        *,
        back_populates: str | None = None,
        cascade_delete: bool | None = False,
        passive_deletes: bool | Literal["all"] | None = False,
        link_model: Any | None = None,
        sa_relationship: RelationshipProperty | None = None,
        sa_relationship_args: Sequence[Any] | None = None,
        sa_relationship_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        if sa_relationship is not None:
            if sa_relationship_args is not None:
                raise RuntimeError(
                    "Passing sa_relationship_args is not supported when "
                    "also passing a sa_relationship"
                )
            if sa_relationship_kwargs is not None:
                raise RuntimeError(
                    "Passing sa_relationship_kwargs is not supported when "
                    "also passing a sa_relationship"
                )
        self.back_populates = back_populates
        self.cascade_delete = cascade_delete
        self.passive_deletes = passive_deletes
        self.link_model = link_model
        self.sa_relationship = sa_relationship
        self.sa_relationship_args = sa_relationship_args
        self.sa_relationship_kwargs = sa_relationship_kwargs


@overload
def Relationship(
    *,
    back_populates: str | None = None,
    cascade_delete: bool | None = False,
    passive_deletes: bool | Literal["all"] | None = False,
    link_model: Any | None = None,
    sa_relationship_args: Sequence[Any] | None = None,
    sa_relationship_kwargs: Mapping[str, Any] | None = None,
) -> Any: ...


@overload
def Relationship(
    *,
    back_populates: str | None = None,
    cascade_delete: bool | None = False,
    passive_deletes: bool | Literal["all"] | None = False,
    link_model: Any | None = None,
    sa_relationship: RelationshipProperty[Any] | None = None,
) -> Any: ...


def Relationship(
    *,
    back_populates: str | None = None,
    cascade_delete: bool | None = False,
    passive_deletes: bool | Literal["all"] | None = False,
    link_model: Any | None = None,
    sa_relationship: RelationshipProperty[Any] | None = None,
    sa_relationship_args: Sequence[Any] | None = None,
    sa_relationship_kwargs: Mapping[str, Any] | None = None,
) -> Any:
    relationship_info = RelationshipInfo(
        back_populates=back_populates,
        cascade_delete=cascade_delete,
        passive_deletes=passive_deletes,
        link_model=link_model,
        sa_relationship=sa_relationship,
        sa_relationship_args=sa_relationship_args,
        sa_relationship_kwargs=sa_relationship_kwargs,
    )
    return relationship_info
