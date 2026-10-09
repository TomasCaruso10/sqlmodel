from typing import Any

from annotated_types import MaxLen
from pydantic import VERSION as P_VERSION
from pydantic import BaseModel
from pydantic import ConfigDict as ConfigDict
from pydantic._internal._fields import PydanticMetadata
from pydantic._internal._model_construction import ModelMetaclass as ModelMetaclass
from pydantic._internal._repr import Representation as Representation
from pydantic.fields import FieldInfo
from pydantic_core import (
    CoreSchema,
    PydanticUndefined,
    PydanticUndefinedType,
    core_schema,
)

from ._typing import InstanceOrType

BaseConfig = ConfigDict
Undefined = PydanticUndefined
UndefinedType = PydanticUndefinedType
PYDANTIC_MINOR_VERSION = tuple(int(i) for i in P_VERSION.split(".")[:2])


class FakeMetadata:
    max_length: int | None = None
    max_digits: int | None = None
    decimal_places: int | None = None


def get_model_fields(model: InstanceOrType[BaseModel]) -> dict[str, "FieldInfo"]:
    # TODO: refactor the usage of this function to always pass the class
    # not the instance, and then remove this extra check
    # this is for compatibility with Pydantic v3
    if isinstance(model, type):
        use_model = model
    else:
        use_model = model.__class__
    return use_model.model_fields


def init_pydantic_private_attrs(new_object: InstanceOrType[BaseModel]) -> None:
    object.__setattr__(new_object, "__pydantic_fields_set__", set())
    object.__setattr__(new_object, "__pydantic_extra__", None)
    object.__setattr__(new_object, "__pydantic_private__", None)


def get_field_metadata(field: Any) -> Any:
    for meta in field.metadata:
        if isinstance(meta, (PydanticMetadata, MaxLen)):
            return meta
    return FakeMetadata()


def unwrap_validator_schema(
    schema: CoreSchema,
) -> tuple[
    core_schema.BeforeValidatorFunctionSchema
    | core_schema.AfterValidatorFunctionSchema
    | core_schema.WrapValidatorFunctionSchema
    | None,
    CoreSchema,
]:
    """Return the innermost validator parent and its wrapped schema, without mutation."""
    parent = None
    while (
        schema["type"] == "function-before"
        or schema["type"] == "function-after"
        or schema["type"] == "function-wrap"
    ):
        parent = schema
        schema = schema["schema"]
    return parent, schema
