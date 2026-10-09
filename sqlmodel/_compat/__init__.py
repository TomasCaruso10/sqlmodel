from typing import Any

from ._pydantic import PYDANTIC_MINOR_VERSION as PYDANTIC_MINOR_VERSION
from ._pydantic import BaseConfig as BaseConfig
from ._pydantic import ConfigDict as ConfigDict
from ._pydantic import FakeMetadata as FakeMetadata
from ._pydantic import ModelMetaclass as ModelMetaclass
from ._pydantic import Representation as Representation
from ._pydantic import Undefined as Undefined
from ._pydantic import UndefinedType as UndefinedType
from ._pydantic import get_field_metadata as get_field_metadata
from ._pydantic import get_model_fields as get_model_fields
from ._pydantic import init_pydantic_private_attrs as init_pydantic_private_attrs
from ._typing import InstanceOrType as InstanceOrType
from ._typing import NoneType as NoneType
from ._typing import UnionType as UnionType
from ._typing import _is_union_type as _is_union_type
from ._typing import get_annotations as get_annotations


class SQLModelConfig(BaseConfig, total=False):
    table: bool | None
    registry: Any | None


def is_table_model_class(cls: type[Any]) -> bool:
    config = getattr(cls, "model_config", {})
    if config:
        return config.get("table", False) or False
    return False
