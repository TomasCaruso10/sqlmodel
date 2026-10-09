import sys
import types
from contextvars import ContextVar
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ForwardRef,
    TypeAlias,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    overload,
)

from annotated_types import MaxLen
from pydantic import VERSION as P_VERSION
from pydantic import AwareDatetime, BaseModel, NaiveDatetime
from pydantic import ConfigDict as ConfigDict
from pydantic._internal._fields import PydanticMetadata
from pydantic._internal._model_construction import ModelMetaclass as ModelMetaclass
from pydantic._internal._repr import Representation as Representation
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined as Undefined
from pydantic_core import PydanticUndefinedType as PydanticUndefinedType
from sqlalchemy.orm.attributes import set_attribute
from sqlalchemy.orm.instrumentation import is_instrumented

BaseConfig = ConfigDict
UndefinedType = PydanticUndefinedType
PYDANTIC_MINOR_VERSION = tuple(int(i) for i in P_VERSION.split(".")[:2])


if TYPE_CHECKING:
    from ._relationships import RelationshipInfo
    from .main import SQLModel

UnionType = getattr(types, "UnionType", Union)
NoneType = type(None)
T = TypeVar("T")
InstanceOrType: TypeAlias = T | type[T]

# Only this instance is receiving prepared fields without running validation.
instance_from_fields: ContextVar["SQLModel | None"] = ContextVar(
    "instance_from_fields", default=None
)


class FakeMetadata:
    max_length: int | None = None
    max_digits: int | None = None
    decimal_places: int | None = None


@dataclass
class ObjectWithUpdateWrapper:
    obj: Any
    update: dict[str, Any]

    def __getattribute__(self, __name: str) -> Any:
        # Do not masquerade as the wrapped model: Pydantic must read attributes
        # instead of returning this wrapper as an already validated instance.
        if __name == "__class__":
            return type(self)
        update = super().__getattribute__("update")
        obj = super().__getattribute__("obj")
        if __name in update:
            return update[__name]
        return getattr(obj, __name)


def _is_union_type(t: Any) -> bool:
    return t is UnionType or t is Union


class InstanceDictProxy:
    """Preserve ORM state when Pydantic replaces an instance's dictionary.

    An instrumented instance keeps its existing dictionary. Prepared mapped
    values go through SQLAlchemy's setters so it can track changes and backrefs.
    Plain Pydantic instances and dictionary restoration keep normal behavior.
    """

    def __init__(self) -> None:
        self.storage = BaseModel.__dict__["__dict__"]

    @overload
    def __get__(
        self, instance: None, owner: type["SQLModel"] | None = None
    ) -> "InstanceDictProxy": ...

    @overload
    def __get__(
        self, instance: "SQLModel", owner: type["SQLModel"] | None = None
    ) -> dict[str, Any]: ...

    def __get__(
        self, instance: "SQLModel | None", owner: type["SQLModel"] | None = None
    ) -> Any:
        if instance is None:
            return self
        return self.storage.__get__(instance, owner)

    def __set__(self, instance: "SQLModel", values: dict[str, Any]) -> None:
        """Deliver prepared fields without replacing SQLAlchemy's state."""
        current = self.storage.__get__(instance, type(instance))
        # Assignment keeps this instance's state in the validated dictionary.
        # A different state belongs to a dictionary restoration, not an assignment.
        if "_sa_instance_state" not in current or (
            "_sa_instance_state" in values
            and values["_sa_instance_state"] is not current["_sa_instance_state"]
        ):
            self.storage.__set__(instance, values)
            return
        for name, value in values.items():
            if name in current and current[name] is value:
                continue
            if is_instrumented(instance, name):
                set_attribute(instance, name, value)
            else:
                current[name] = value


class SQLModelConfig(BaseConfig, total=False):
    table: bool | None
    registry: Any | None


def get_model_fields(model: InstanceOrType[BaseModel]) -> dict[str, "FieldInfo"]:
    # TODO: refactor the usage of this function to always pass the class
    # not the instance, and then remove this extra check
    # this is for compatibility with Pydantic v3
    if isinstance(model, type):
        use_model = model
    else:
        use_model = model.__class__
    return use_model.model_fields


def init_pydantic_private_attrs(new_object: InstanceOrType["SQLModel"]) -> None:
    object.__setattr__(new_object, "__pydantic_fields_set__", set())
    object.__setattr__(new_object, "__pydantic_extra__", None)
    object.__setattr__(new_object, "__pydantic_private__", None)


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


def is_table_model_class(cls: type[Any]) -> bool:
    config = getattr(cls, "model_config", {})
    if config:
        return config.get("table", False) or False
    return False


def get_relationship_to(
    name: str,
    rel_info: "RelationshipInfo",
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


def is_field_noneable(field: "FieldInfo") -> bool:
    if getattr(field, "nullable", Undefined) is not Undefined:
        return field.nullable  # type: ignore
    origin = get_origin(field.annotation)
    if origin is not None and _is_union_type(origin):
        args = get_args(field.annotation)
        if any(arg is NoneType for arg in args):
            return True
    if not field.is_required():
        if field.default is Undefined:
            return False
        if field.annotation is None or field.annotation is NoneType:
            return True
        return False
    return False


def get_sa_type_from_type_annotation(annotation: Any) -> Any:
    # Resolve Optional fields
    if annotation is None:
        raise ValueError("Missing field type")
    origin = get_origin(annotation)
    if origin is None:
        return annotation
    elif origin is Annotated:
        type_, *metadata = get_args(annotation)
        type_ = get_sa_type_from_type_annotation(type_)
        # Like Pydantic, apply the last timezone constraint in Annotated.
        for meta in metadata:
            if meta is AwareDatetime or isinstance(meta, cast(type, AwareDatetime)):
                type_ = AwareDatetime
            elif meta is NaiveDatetime or isinstance(meta, cast(type, NaiveDatetime)):
                type_ = NaiveDatetime
        return type_
    if _is_union_type(origin):
        bases = get_args(annotation)
        if len(bases) > 2:
            raise ValueError("Cannot have a (non-optional) union as a SQLAlchemy field")
        # Non-optional unions are not allowed
        if bases[0] is not NoneType and bases[1] is not NoneType:
            raise ValueError("Cannot have a (non-optional) union as a SQLAlchemy field")
        # Optional unions are allowed
        use_type = bases[0] if bases[0] is not NoneType else bases[1]
        return get_sa_type_from_type_annotation(use_type)
    return origin


def get_sa_type_from_field(field: Any) -> Any:
    type_: Any = field.rebuild_annotation()
    return get_sa_type_from_type_annotation(type_)


def get_field_metadata(field: Any) -> Any:
    for meta in field.metadata:
        if isinstance(meta, (PydanticMetadata, MaxLen)):
            return meta
    return FakeMetadata()
