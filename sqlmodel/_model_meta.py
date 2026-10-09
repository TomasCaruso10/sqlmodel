"""Create SQLModel classes and select their table-specific capabilities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from sqlalchemy.orm import registry
from sqlalchemy.orm.decl_api import DeclarativeMeta
from typing_extensions import dataclass_transform

from ._compat import (
    ModelMetaclass,
    SQLModelConfig,
    Undefined,
    get_annotations,
)
from ._fields import Field, FieldInfo
from ._model_config import ModelConfig
from ._relationships import RelationshipInfo
from ._table_model import TableMixin

if TYPE_CHECKING:
    from pydantic._internal._model_construction import ModelMetaclass as ModelMetaclass
    from pydantic_core import PydanticUndefined as Undefined

    from .main import SQLModel


@dataclass_transform(kw_only_default=True, field_specifiers=(Field, FieldInfo))
class SQLModelMetaclass(ModelMetaclass, DeclarativeMeta):
    __sqlmodel_relationships__: dict[str, RelationshipInfo]
    model_config: SQLModelConfig
    model_fields: ClassVar[dict[str, FieldInfo]]

    # Replicate SQLAlchemy
    def __setattr__(cls, name: str, value: Any) -> None:  # ty: ignore[invalid-method-override]
        cast("type[SQLModel]", cls)._set_class_attribute(name, value)

    def __delattr__(cls, name: str) -> None:  # ty: ignore[invalid-method-override]
        cast("type[SQLModel]", cls)._delete_class_attribute(name)

    # From Pydantic
    def __new__(
        cls,
        name: str,
        bases: tuple[type[Any], ...],
        class_dict: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        config = ModelConfig.from_declaration(bases, class_dict, kwargs)
        bases = cls._model_bases(bases, config)
        namespace, relationship_annotations = cls._prepare_pydantic_namespace(
            class_dict
        )
        new_cls = cast(
            "type[SQLModel]",
            super().__new__(cls, name, bases, namespace, **config.pydantic_kwargs),
        )
        new_cls.__annotations__ = {
            **relationship_annotations,
            **namespace["__annotations__"],
            **new_cls.__annotations__,
        }

        config = config.with_model_config(new_cls.model_config)
        config_table = config.table
        new_cls._configure_model(table=config_table is True)

        config_registry = config.registry
        if config_registry is not Undefined:
            cls._configure_registry(
                new_cls, cast(registry, config_registry), config_table
            )
        return new_cls

    @staticmethod
    def _model_bases(
        bases: tuple[type[Any], ...],
        config: ModelConfig,
    ) -> tuple[type[Any], ...]:
        """Select table behavior before Pydantic builds the class schema."""
        if not config.is_table:
            return bases
        if any(issubclass(base, TableMixin) for base in bases):
            return bases
        # User overrides stay ahead of the table integration in the MRO.
        return (*bases, TableMixin)

    @staticmethod
    def _prepare_pydantic_namespace(
        class_dict: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        relationships: dict[str, RelationshipInfo] = {}
        dict_for_pydantic = {}
        original_annotations = get_annotations(class_dict)
        pydantic_annotations = {}
        relationship_annotations = {}
        for k, v in class_dict.items():
            if isinstance(v, RelationshipInfo):
                relationships[k] = v
            else:
                dict_for_pydantic[k] = v
        for k, v in original_annotations.items():
            if k in relationships:
                relationship_annotations[k] = v
            else:
                pydantic_annotations[k] = v
        namespace = {
            **dict_for_pydantic,
            "__weakref__": None,
            "__sqlmodel_relationships__": relationships,
            "__annotations__": pydantic_annotations,
        }
        return namespace, relationship_annotations

    @staticmethod
    def _configure_registry(
        model: type[SQLModel],
        config_registry: registry,
        config_table: Any,
    ) -> None:
        # If it was passed by kwargs, ensure it's also set in config
        model.model_config["registry"] = config_table
        setattr(model, "_sa_registry", config_registry)  # noqa: B010
        setattr(model, "metadata", config_registry.metadata)  # noqa: B010
        setattr(model, "__abstract__", True)  # noqa: B010

    # Override SQLAlchemy, allow both SQLAlchemy and plain Pydantic models
    def __init__(
        cls, classname: str, bases: tuple[type, ...], dict_: dict[str, Any], **kw: Any
    ) -> None:
        cast("type[SQLModel]", cls)._initialize_class(classname, bases, dict_, **kw)
