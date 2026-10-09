import sys
import types
from typing import Any, TypeAlias, TypeVar, Union, get_args, get_origin

UnionType = getattr(types, "UnionType", Union)
NoneType = type(None)
T = TypeVar("T")
InstanceOrType: TypeAlias = T | type[T]


def _is_union_type(t: Any) -> bool:
    return t is UnionType or t is Union


def get_annotations(class_dict: dict[str, Any]) -> dict[str, Any]:
    raw_annotations: dict[str, Any] = class_dict.get("__annotations__", {})
    if sys.version_info >= (3, 14) and "__annotations__" not in class_dict:
        # See https://github.com/pydantic/pydantic/pull/11991
        from annotationlib import (
            Format,
            call_annotate_function,
            get_annotate_from_class_namespace,
        )

        if annotate := get_annotate_from_class_namespace(class_dict):
            raw_annotations = call_annotate_function(annotate, format=Format.FORWARDREF)
    return raw_annotations


def unwrap_optional(annotation: Any) -> Any:
    """Remove None from a union containing exactly one other type."""
    if not _is_union_type(get_origin(annotation)):
        return annotation
    non_null_members = tuple(
        member for member in get_args(annotation) if member is not NoneType
    )
    if len(non_null_members) == 1:
        return non_null_members[0]
    return annotation
