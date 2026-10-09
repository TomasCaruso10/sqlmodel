from __future__ import annotations

import builtins
from collections.abc import Callable, Mapping, Sequence
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    TypeAlias,
    TypeVar,
    Union,
    cast,
)

from pydantic import BaseModel, GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema
from sqlalchemy.orm import (
    declared_attr,
    registry,
)
from sqlalchemy.orm.decl_api import DeclarativeMeta
from sqlalchemy.sql.schema import MetaData
from typing_extensions import dataclass_transform, deprecated

from ._compat import (
    PYDANTIC_MINOR_VERSION,
    BaseConfig,
    ModelMetaclass,
    SQLModelConfig,
    Undefined,
    _pydantic,
    get_annotations,
    get_model_fields,
)
from ._construction import (
    ObjectWithUpdateWrapper,
)
from ._fields import MAX_ITEMS_DEPRECATION_MSG as MAX_ITEMS_DEPRECATION_MSG
from ._fields import MIN_ITEMS_DEPRECATION_MSG as MIN_ITEMS_DEPRECATION_MSG
from ._fields import Field as Field
from ._fields import FieldInfo as FieldInfo
from ._fields import FieldInfoMetadata as FieldInfoMetadata
from ._fields import NoArgAnyCallable as NoArgAnyCallable
from ._fields import OnDeleteType as OnDeleteType
from ._fields import SaTypeOrInstance as SaTypeOrInstance
from ._fields import _get_sqlmodel_field_metadata as _get_sqlmodel_field_metadata
from ._fields import _get_sqlmodel_field_value as _get_sqlmodel_field_value
from ._fields import get_column_from_field as get_column_from_field
from ._fields import get_sqlalchemy_type as get_sqlalchemy_type
from ._model import ModelMixin
from ._relationships import Relationship as Relationship
from ._relationships import RelationshipInfo as RelationshipInfo
from ._table_model import TableMixin

if TYPE_CHECKING:
    from pydantic._internal._model_construction import ModelMetaclass as ModelMetaclass
    from pydantic_core import PydanticUndefined as Undefined

IncEx: TypeAlias = (
    set[int]
    | set[str]
    | Mapping[int, Union["IncEx", bool]]
    | Mapping[str, Union["IncEx", bool]]
)


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
        bases = cls._model_bases(bases, class_dict, kwargs)
        namespace, relationship_annotations = cls._prepare_pydantic_namespace(
            class_dict
        )
        config_kwargs = cls._pydantic_config_kwargs(kwargs)
        new_cls = cast(
            "type[SQLModel]",
            super().__new__(cls, name, bases, namespace, **config_kwargs),
        )
        new_cls.__annotations__ = {
            **relationship_annotations,
            **namespace["__annotations__"],
            **new_cls.__annotations__,
        }

        config_table = cls._get_config(new_cls, "table", kwargs)
        new_cls._configure_model(table=config_table is True)

        config_registry = cls._get_config(new_cls, "registry", kwargs)
        if config_registry is not Undefined:
            cls._configure_registry(
                new_cls, cast(registry, config_registry), config_table
            )
        return new_cls

    @staticmethod
    def _model_bases(
        bases: tuple[type[Any], ...],
        class_dict: dict[str, Any],
        kwargs: dict[str, Any],
    ) -> tuple[type[Any], ...]:
        """Select table behavior before Pydantic builds the class schema."""
        config = {}
        for base in bases:
            config.update(getattr(base, "model_config", {}))
        if "Config" in class_dict:
            config["table"] = getattr(
                class_dict["Config"], "table", config.get("table", Undefined)
            )
        config.update(class_dict.get("model_config", {}))
        table = config.get("table", Undefined)
        if table is Undefined:
            table = kwargs.get("table", Undefined)
        if table is not True:
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
    def _pydantic_config_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
        # Duplicate logic from Pydantic to filter config kwargs because if they are
        # passed directly including the registry Pydantic will pass them over to the
        # superclass causing an error
        allowed_config_kwargs: set[str] = {
            key
            for key in dir(BaseConfig)
            if not (
                key.startswith("__") and key.endswith("__")
            )  # skip dunder methods and attributes
        }
        return {key: kwargs[key] for key in kwargs.keys() & allowed_config_kwargs}

    @staticmethod
    def _get_config(model: type[SQLModel], name: str, kwargs: dict[str, Any]) -> Any:
        value = model.model_config.get(name, Undefined)
        if value is not Undefined:
            return value
        return kwargs.get(name, Undefined)

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


default_registry = registry()

_TSQLModel = TypeVar("_TSQLModel", bound="SQLModel")


class SQLModel(
    ModelMixin, BaseModel, metaclass=SQLModelMetaclass, registry=default_registry
):
    # SQLAlchemy needs to set weakref(s), Pydantic will set the other slots values
    __slots__ = ("__weakref__",)
    __tablename__: ClassVar[str | Callable[..., str]]
    __sqlmodel_relationships__: ClassVar[builtins.dict[str, RelationshipInfo]]
    __name__: ClassVar[str]
    metadata: ClassVar[MetaData]
    __allow_unmapped__ = True  # https://docs.sqlalchemy.org/en/20/changelog/migration_20.html#migration-20-step-six
    model_config = SQLModelConfig(from_attributes=True)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        schema = handler(source)
        # Model validators can wrap the model node. Keep those wrappers intact.
        _, model_schema = _pydantic.unwrap_validator_schema(schema)
        if model_schema["type"] == "model" and cls.__sqlmodel_relationships__:
            # Relationships remain ORM inputs, not Pydantic model fields.
            # Insert below before/wrap validators so their normalized input
            # reaches both field validation and relationship delivery.
            parent, fields_schema = _pydantic.unwrap_validator_schema(
                model_schema["schema"]
            )
            if parent is None:
                parent = model_schema
            parent["schema"] = core_schema.no_info_wrap_validator_function(
                cls._validate_fields_with_relationships, fields_schema
            )
        return schema

    @classmethod
    def _validate_fields_with_relationships(
        cls, value: Any, handler: core_schema.ValidatorFunctionWrapHandler
    ) -> tuple[builtins.dict[str, Any], builtins.dict[str, Any] | None, set[str]]:
        """Preserve supplied ORM objects alongside Pydantic's validated fields."""
        relationships = {}
        for name in cls.__sqlmodel_relationships__:
            related = (
                value.get(name, Undefined)
                if isinstance(value, dict)
                else getattr(value, name, Undefined)
            )
            if related is not Undefined:
                relationships[name] = related
        if isinstance(value, dict):
            value = {
                name: item for name, item in value.items() if name not in relationships
            }
        fields, extra, fields_set = handler(value)
        # The dictionary proxy will deliver these objects through ORM setters.
        # They remain excluded from Pydantic's fields and serialization.
        fields.update(relationships)
        return fields, extra, fields_set

    def __repr_args__(self) -> Sequence[tuple[str | None, Any]]:
        # Don't show SQLAlchemy private attributes
        return [
            (k, v)
            for k, v in super().__repr_args__()
            if not (isinstance(k, str) and k.startswith("_sa_"))
        ]

    @declared_attr  # type: ignore
    def __tablename__(cls) -> str:
        return cls.__name__.lower()

    @classmethod
    def model_validate(  # ty: ignore[invalid-method-override]
        cls: type[_TSQLModel],
        obj: Any,
        *,
        strict: bool | None = None,
        from_attributes: bool | None = None,
        context: builtins.dict[str, Any] | None = None,
        update: builtins.dict[str, Any] | None = None,
    ) -> _TSQLModel:
        # Keep SQLModel's update argument and conversion to the requested class.
        # Pydantic owns instance validation.
        if update or isinstance(obj, cls):
            obj = (
                {**obj, **(update or {})}
                if isinstance(obj, dict)
                else ObjectWithUpdateWrapper(obj=obj, update=update or {})
            )
        return super().model_validate(
            obj,
            strict=strict,
            from_attributes=from_attributes,
            context=context,
        )

    def model_dump(
        self,
        *,
        mode: Literal["json", "python"] | str = "python",
        include: IncEx | None = None,
        exclude: IncEx | None = None,
        context: Any | None = None,  # v2.7
        by_alias: bool | None = None,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        exclude_computed_fields: bool = False,  # v2.12
        round_trip: bool = False,
        warnings: bool | Literal["none", "warn", "error"] = True,
        fallback: Callable[[Any], Any] | None = None,  # v2.11
        serialize_as_any: bool = False,  # v2.7
        polymorphic_serialization: bool | None = None,  # v2.13
    ) -> builtins.dict[str, Any]:
        if PYDANTIC_MINOR_VERSION < (2, 11):
            by_alias = by_alias or False
        extra_kwargs: dict[str, Any] = {}
        extra_kwargs["context"] = context
        extra_kwargs["serialize_as_any"] = serialize_as_any
        if PYDANTIC_MINOR_VERSION >= (2, 11):
            extra_kwargs["fallback"] = fallback
        if PYDANTIC_MINOR_VERSION >= (2, 12):
            extra_kwargs["exclude_computed_fields"] = exclude_computed_fields
        if PYDANTIC_MINOR_VERSION >= (2, 13):
            extra_kwargs["polymorphic_serialization"] = polymorphic_serialization
        return super().model_dump(
            mode=mode,
            include=include,
            exclude=exclude,
            by_alias=by_alias,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
            round_trip=round_trip,
            warnings=warnings,
            **extra_kwargs,
        )

    @deprecated(
        """
        🚨 `obj.dict()` was deprecated in SQLModel 0.0.14, you should
        instead use `obj.model_dump()`.
        """
    )
    def dict(
        self,
        *,
        include: IncEx | None = None,
        exclude: IncEx | None = None,
        by_alias: bool = False,
        exclude_unset: bool = False,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
    ) -> builtins.dict[str, Any]:
        return self.model_dump(
            include=include,
            exclude=exclude,
            by_alias=by_alias,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
        )

    @classmethod
    @deprecated(
        """
        🚨 `obj.from_orm(data)` was deprecated in SQLModel 0.0.14, you should
        instead use `obj.model_validate(data)`.
        """
    )
    def from_orm(
        cls: type[_TSQLModel],
        obj: Any,
        update: builtins.dict[str, Any] | None = None,
    ) -> _TSQLModel:
        return cls.model_validate(obj, update=update)

    @classmethod
    @deprecated(
        """
        🚨 `obj.parse_obj(data)` was deprecated in SQLModel 0.0.14, you should
        instead use `obj.model_validate(data)`.
        """
    )
    def parse_obj(
        cls: type[_TSQLModel],
        obj: Any,
        update: builtins.dict[str, Any] | None = None,
    ) -> _TSQLModel:
        return cls.model_validate(obj, update=update)

    def sqlmodel_update(
        self: _TSQLModel,
        obj: builtins.dict[str, Any] | BaseModel,
        *,
        update: builtins.dict[str, Any] | None = None,
    ) -> _TSQLModel:
        use_update = (update or {}).copy()
        if isinstance(obj, dict):
            for key, value in {**obj, **use_update}.items():
                if key in get_model_fields(self):
                    setattr(self, key, value)
        elif isinstance(obj, BaseModel):
            for key in get_model_fields(obj):
                if key in use_update:
                    value = use_update.pop(key)
                else:
                    value = getattr(obj, key)
                setattr(self, key, value)
            for remaining_key, value in use_update.items():
                if remaining_key in get_model_fields(self):
                    setattr(self, remaining_key, value)
        else:
            raise ValueError(
                "Can't use sqlmodel_update() with something that "
                f"is not a dict, SQLModel, or Pydantic model: {obj}"
            )
        return self
