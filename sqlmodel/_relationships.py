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

from sqlalchemy.orm import RelationshipProperty

from ._compat import NoneType, Representation, _is_union_type

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


def get_relationship_to(
    name: str,
    rel_info: RelationshipInfo,
    annotation: Any,
) -> Any:
    origin = get_origin(annotation)
    use_annotation = annotation
    # Direct relationships (e.g. 'Team' or Team) have None as an origin
    if origin is None:
        if isinstance(use_annotation, ForwardRef):
            use_annotation = use_annotation.__forward_arg__
        else:
            return use_annotation
    # If Union (e.g. Optional), get the real field
    elif _is_union_type(origin):
        use_annotation = get_args(annotation)
        if len(use_annotation) > 2:
            raise ValueError("Cannot have a (non-optional) union as a SQLAlchemy field")
        arg1, arg2 = use_annotation
        if arg1 is NoneType and arg2 is not NoneType:
            use_annotation = arg2
        elif arg2 is NoneType and arg1 is not NoneType:
            use_annotation = arg1
        else:
            raise ValueError(
                "Cannot have a Union of None and None as a SQLAlchemy field"
            )

    # If a list, then also get the real field
    elif origin is list:
        use_annotation = get_args(annotation)[0]

    return get_relationship_to(name=name, rel_info=rel_info, annotation=use_annotation)
