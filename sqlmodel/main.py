from __future__ import annotations

import builtins
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from functools import update_wrapper, wraps
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    TypeAlias,
    TypeVar,
    Union,
    cast,
    get_origin,
)

from pydantic import BaseModel, GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema
from sqlalchemy.orm import (
    Mapped,
    declared_attr,
    registry,
)
from sqlalchemy.orm.attributes import set_attribute
from sqlalchemy.orm.decl_api import DeclarativeMeta
from sqlalchemy.orm.instrumentation import is_instrumented
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
    init_pydantic_private_attrs,
    is_table_model_class,
)
from ._construction import (
    InstanceDictProxy,
    ObjectWithUpdateWrapper,
    instance_from_fields,
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
from ._relationships import Relationship as Relationship
from ._relationships import RelationshipInfo as RelationshipInfo

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
        if is_table_model_class(cls):
            DeclarativeMeta.__setattr__(cls, name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:  # ty: ignore[invalid-method-override]
        if is_table_model_class(cls):
            DeclarativeMeta.__delattr__(cls, name)
        else:
            super().__delattr__(name)

    # From Pydantic
    def __new__(
        cls,
        name: str,
        bases: tuple[type[Any], ...],
        class_dict: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
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
        if config_table is True:
            cls._configure_table(new_cls)

        config_registry = cls._get_config(new_cls, "registry", kwargs)
        if config_registry is not Undefined:
            cls._configure_registry(
                new_cls, cast(registry, config_registry), config_table
            )
        return new_cls

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
    def _configure_table(model: type[SQLModel]) -> None:
        # If it was passed by kwargs, ensure it's also set in config
        model.model_config["table"] = True
        for name, field in get_model_fields(model).items():
            column = get_column_from_field(field)
            setattr(model, name, column)
        # Set a config flag to tell FastAPI that this should be read with a field
        # in orm_mode instead of preemptively converting it to a dict.
        # This could be done by reading model.model_config['table'] in FastAPI, but
        # that's very specific about SQLModel, so let's have another config that
        # other future tools based on Pydantic can use.
        model.model_config["read_from_attributes"] = True  # ty: ignore[invalid-key]
        # For compatibility with older versions
        # TODO: remove this in the future
        model.model_config["read_with_orm_mode"] = True  # ty: ignore[invalid-key]

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
        # Only one of the base classes (or the current one) should be a table model
        # this allows FastAPI cloning a SQLModel for the response_model without
        # trying to create a new SQLAlchemy, for a new table, with the same name, that
        # triggers an error
        base_is_table = any(is_table_model_class(base) for base in bases)
        if is_table_model_class(cls) and not base_is_table:
            for rel_name, rel_info in cls.__sqlmodel_relationships__.items():
                if rel_info.sa_relationship:
                    # There's a SQLAlchemy relationship declared, that takes precedence
                    # over anything else, use that and continue with the next attribute
                    setattr(cls, rel_name, rel_info.sa_relationship)  # Fix #315
                    continue
                raw_ann = cls.__annotations__[rel_name]
                origin: Any = get_origin(raw_ann)
                if origin is Mapped:
                    ann = raw_ann.__args__[0]
                else:
                    ann = raw_ann
                    # Plain forward references, for models not yet defined, are not
                    # handled well by SQLAlchemy without Mapped, so, wrap the
                    # annotations in Mapped here
                    cls.__annotations__[rel_name] = Mapped[ann]
                rel_value = rel_info.from_annotation(ann)
                setattr(cls, rel_name, rel_value)  # Fix #315
            # SQLAlchemy no longer uses dict_
            # Ref: https://github.com/sqlalchemy/sqlalchemy/commit/428ea01f00a9cc7f85e435018565eb6da7af1b77
            # Tag: 1.4.36
            DeclarativeMeta.__init__(cls, classname, bases, dict_, **kw)
        else:
            ModelMetaclass.__init__(cls, classname, bases, dict_, **kw)


default_registry = registry()

_TSQLModel = TypeVar("_TSQLModel", bound="SQLModel")


class SQLModel(BaseModel, metaclass=SQLModelMetaclass, registry=default_registry):
    # SQLAlchemy needs to set weakref(s), Pydantic will set the other slots values
    __slots__ = ("__weakref__",)
    __dict__ = InstanceDictProxy()
    __tablename__: ClassVar[str | Callable[..., str]]
    __sqlmodel_relationships__: ClassVar[builtins.dict[str, RelationshipInfo]]
    __name__: ClassVar[str]
    metadata: ClassVar[MetaData]
    __allow_unmapped__ = True  # https://docs.sqlalchemy.org/en/20/changelog/migration_20.html#migration-20-step-six
    model_config = SQLModelConfig(from_attributes=True)

    # Typing spec says `__new__` returning `Any` overrides normal constructor
    # behavior, but a missing annotation does not:
    def __new__(cls, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        new_object = super().__new__(cls)
        # SQLAlchemy doesn't call __init__ on the base class when querying from DB
        # Ref: https://docs.sqlalchemy.org/en/14/orm/constructors.html
        # Set __fields_set__ here, that would have been set when calling __init__
        # in the Pydantic model so that when SQLAlchemy sets attributes that are
        # added (e.g. when querying from DB) to the __fields_set__, this already exists
        init_pydantic_private_attrs(new_object)
        return new_object

    def __init__(__pydantic_self__, /, **data: Any) -> None:
        # Uses something other than `self` the first arg to allow "self" as a
        # settable attribute
        # SQLAlchemy's generated initializer does not preserve positional-only args.
        if instance_from_fields.get() is __pydantic_self__:
            # SQLAlchemy has prepared its state; deliver fields without validating again.
            __pydantic_self__.__dict__ = data
        else:
            super().__init__(**data)

    # Preserve Pydantic's initializer marker without changing its static signature.
    update_wrapper(__init__, BaseModel.__init__)

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

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        # Wait until Pydantic has prepared model_post_init, including private attributes.
        # Pydantic calls model_post_init from both validation and model_construct.
        # Preserve user overrides while preparing the ORM before they run.
        if post_init := vars(cls).get("model_post_init"):

            @wraps(post_init)
            def model_post_init(self: SQLModel, context: Any) -> None:
                self._initialize_orm()
                post_init(self, context)

            cls.model_post_init = model_post_init  # ty: ignore[invalid-assignment]

    def model_post_init(self, context: Any) -> None:
        self._initialize_orm()

    def _initialize_orm(self) -> None:
        """Deliver prepared fields through SQLAlchemy's native constructor."""
        # Direct construction already ran SQLAlchemy's constructor. Validation
        # and model_construct allocate through __new__ and still need it.
        if (
            is_table_model_class(type(self))
            and "_sa_instance_state" not in self.__dict__
        ):
            values = self.__dict__.copy()
            self.__dict__.clear()
            token = instance_from_fields.set(self)
            try:
                type(self).__init__(self, **values)
            finally:
                instance_from_fields.reset(token)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_sa_instance_state":
            self.__dict__[name] = value
            return
        if is_table_model_class(type(self)) and is_instrumented(self, name):
            if name not in self.__sqlmodel_relationships__ and self.model_config.get(
                "validate_assignment"
            ):
                previous = self.__dict__.get(name, Undefined)
                super().__setattr__(name, value)
                # The proxy delivers changed values. An explicit assignment of
                # the same value must still trigger SQLAlchemy's setter once.
                if self.__dict__[name] is previous:
                    set_attribute(self, name, previous)
                return
            set_attribute(self, name, value)
        # Relationships belong to SQLAlchemy; Pydantic manages other attributes.
        if name not in self.__sqlmodel_relationships__:
            super().__setattr__(name, value)

    def __setstate__(self, state: builtins.dict[Any, Any]) -> None:
        # Restoration replaces all attributes. Clear the old dictionary so the
        # proxy does not interpret restored fields as changes to an existing object.
        if state.get("__dict__") is not self.__dict__:
            self.__dict__.clear()
        super().__setstate__(state)

    def __deepcopy__(
        self: _TSQLModel, memo: builtins.dict[int, Any] | None = None
    ) -> _TSQLModel:
        """Keep ORM back-references attached to the copied instance."""
        if not is_table_model_class(type(self)):
            return super().__deepcopy__(memo)
        memo = {} if memo is None else memo
        copied = type(self).__new__(type(self))
        # ORM state points back to its owner. Register the copy before following
        # that reference, so it resolves to this instance instead of another copy.
        memo[id(self)] = copied
        copied.__setstate__(deepcopy(self.__getstate__(), memo))
        return copied

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
