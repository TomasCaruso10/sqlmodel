from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import (
    TYPE_CHECKING,
    Any,
    ForwardRef,
    Literal,
    get_args,
    get_origin,
    overload,
)

from pydantic import ConfigDict, model_validator
from pydantic.dataclasses import dataclass
from sqlalchemy import inspect
from sqlalchemy.orm import RelationshipProperty, relationship
from typing_extensions import Self

from ._compat import Representation, _typing

if TYPE_CHECKING:
    from pydantic._internal._repr import Representation as Representation


@dataclass(
    kw_only=True,
    repr=False,
    eq=False,
    config=ConfigDict(strict=True, arbitrary_types_allowed=True, extra="forbid"),
)
class RelationshipInfo(Representation):
    back_populates: str | None = None
    cascade_delete: bool | None = False
    passive_deletes: bool | Literal["all"] | None = False
    link_model: Any | None = None
    sa_relationship: RelationshipProperty | None = None
    sa_relationship_args: Sequence[Any] | None = None
    sa_relationship_kwargs: Mapping[str, Any] | None = None

    @model_validator(mode="after")
    def validate_relationship_options(self) -> Self:
        if self.sa_relationship is not None:
            if self.sa_relationship_args is not None:
                raise RuntimeError(
                    "Passing sa_relationship_args is not supported when "
                    "also passing a sa_relationship"
                )
            if self.sa_relationship_kwargs is not None:
                raise RuntimeError(
                    "Passing sa_relationship_kwargs is not supported when "
                    "also passing a sa_relationship"
                )
        return self

    def from_annotation(self, annotation: Any) -> RelationshipProperty[Any]:
        """Create the native relationship using this declaration's options."""
        target = self.resolve_target(annotation)
        kwargs = self.relationship_kwargs()
        kwargs.update(self.sa_relationship_kwargs or {})
        return relationship(target, *(self.sa_relationship_args or ()), **kwargs)

    def relationship_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.back_populates:
            kwargs["back_populates"] = self.back_populates
        if self.cascade_delete:
            kwargs["cascade"] = "all, delete-orphan"
        if self.passive_deletes:
            kwargs["passive_deletes"] = self.passive_deletes
        if self.link_model:
            inspected = inspect(self.link_model)
            local_table = getattr(inspected, "local_table")  # noqa: B009
            if local_table is None:
                raise RuntimeError(
                    f"Couldn't find the secondary table for model {self.link_model}"
                )
            kwargs["secondary"] = local_table
        return kwargs

    @staticmethod
    def resolve_target(annotation: Any) -> Any:
        """Resolve the relationship target from its type annotation."""
        origin = get_origin(annotation)
        use_annotation = annotation
        # Direct relationships (e.g. 'Team' or Team) have None as an origin
        if origin is None:
            if isinstance(use_annotation, ForwardRef):
                use_annotation = use_annotation.__forward_arg__
            else:
                return use_annotation
        # If Union (e.g. Optional), get the real field
        elif _typing._is_union_type(origin):
            if len(get_args(annotation)) > 2:
                raise ValueError(
                    "Cannot have a (non-optional) union as a SQLAlchemy field"
                )
            use_annotation = _typing.unwrap_optional(annotation)
            if use_annotation is annotation:
                raise ValueError(
                    "Cannot have a Union of None and None as a SQLAlchemy field"
                )

        # If a list, then also get the real field
        elif origin is list:
            use_annotation = get_args(annotation)[0]

        return RelationshipInfo.resolve_target(use_annotation)


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
