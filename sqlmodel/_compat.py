import sys
import types
from typing import (
    TYPE_CHECKING,
    Any,
    TypeAlias,
    TypeVar,
    Union,
)

from annotated_types import MaxLen
from pydantic import VERSION as P_VERSION
from pydantic import BaseModel
from pydantic import ConfigDict as ConfigDict
from pydantic._internal._fields import PydanticMetadata
from pydantic._internal._model_construction import ModelMetaclass as ModelMetaclass
from pydantic._internal._repr import Representation as Representation
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined, PydanticUndefinedType

BaseConfig = ConfigDict
Undefined = PydanticUndefined
UndefinedType = PydanticUndefinedType
PYDANTIC_MINOR_VERSION = tuple(int(i) for i in P_VERSION.split(".")[:2])


if TYPE_CHECKING:
    from .main import SQLModel

UnionType = getattr(types, "UnionType", Union)
NoneType = type(None)
T = TypeVar("T")
InstanceOrType: TypeAlias = T | type[T]


class FakeMetadata:
    max_length: int | None = None
    max_digits: int | None = None
    decimal_places: int | None = None


def _is_union_type(t: Any) -> bool:
    return t is UnionType or t is Union


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


def get_field_metadata(field: Any) -> Any:
    for meta in field.metadata:
        if isinstance(meta, (PydanticMetadata, MaxLen)):
            return meta
    return FakeMetadata()
