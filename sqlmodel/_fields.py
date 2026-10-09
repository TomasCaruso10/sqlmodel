from __future__ import annotations

import ipaddress
import uuid
import warnings
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    TypeAlias,
    cast,
    get_args,
    get_origin,
    overload,
)

from pydantic import (
    AwareDatetime,
    Discriminator,
    EmailStr,
    NaiveDatetime,
)
from pydantic.fields import FieldInfo as PydanticFieldInfo
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Interval,
    Numeric,
)
from sqlalchemy import Enum as sa_Enum
from sqlalchemy.sql.sqltypes import LargeBinary, Time, Uuid
from sqlalchemy.types import TypeEngine
from typing_extensions import deprecated

from ._compat import (
    NoneType,
    Undefined,
    UndefinedType,
    _is_union_type,
    get_field_metadata,
)
from .sql.sqltypes import AutoString, UTCDateTime

if TYPE_CHECKING:
    from pydantic_core import PydanticUndefined as Undefined
    from pydantic_core import PydanticUndefinedType as UndefinedType

NoArgAnyCallable = Callable[[], Any]
SaTypeOrInstance: TypeAlias = TypeEngine[Any] | type[TypeEngine[Any]]
OnDeleteType = Literal["CASCADE", "SET NULL", "RESTRICT"]

MIN_ITEMS_DEPRECATION_MSG = (
    "`min_items` is deprecated and will be removed, use `min_length` instead"
)
MAX_ITEMS_DEPRECATION_MSG = (
    "`max_items` is deprecated and will be removed, use `max_length` instead"
)


class FieldInfo(PydanticFieldInfo):  # ty: ignore[subclass-of-final-class]
    # mypy - ignore that PydanticFieldInfo is @final
    def __init__(self, default: Any = Undefined, **kwargs: Any) -> None:
        primary_key = kwargs.pop("primary_key", False)
        nullable = kwargs.pop("nullable", Undefined)
        foreign_key = kwargs.pop("foreign_key", Undefined)
        ondelete = kwargs.pop("ondelete", Undefined)
        unique = kwargs.pop("unique", False)
        index = kwargs.pop("index", Undefined)
        sa_type = kwargs.pop("sa_type", Undefined)
        sa_column = kwargs.pop("sa_column", Undefined)
        sa_column_args = kwargs.pop("sa_column_args", Undefined)
        sa_column_kwargs = kwargs.pop("sa_column_kwargs", Undefined)
        if sa_column is not Undefined:
            if sa_column_args is not Undefined:
                raise RuntimeError(
                    "Passing sa_column_args is not supported when "
                    "also passing a sa_column"
                )
            if sa_column_kwargs is not Undefined:
                raise RuntimeError(
                    "Passing sa_column_kwargs is not supported when "
                    "also passing a sa_column"
                )
            if primary_key is not Undefined:
                raise RuntimeError(
                    "Passing primary_key is not supported when also passing a sa_column"
                )
            if nullable is not Undefined:
                raise RuntimeError(
                    "Passing nullable is not supported when also passing a sa_column"
                )
            if foreign_key is not Undefined:
                raise RuntimeError(
                    "Passing foreign_key is not supported when also passing a sa_column"
                )
            if ondelete is not Undefined:
                raise RuntimeError(
                    "Passing ondelete is not supported when also passing a sa_column"
                )
            if unique is not Undefined:
                raise RuntimeError(
                    "Passing unique is not supported when also passing a sa_column"
                )
            if index is not Undefined:
                raise RuntimeError(
                    "Passing index is not supported when also passing a sa_column"
                )
            if sa_type is not Undefined:
                raise RuntimeError(
                    "Passing sa_type is not supported when also passing a sa_column"
                )
        if sa_column_kwargs is not Undefined and "type_" in sa_column_kwargs:
            raise RuntimeError(
                "Passing type_ is not supported in sa_column_kwargs, "
                "use sa_type instead"
            )
        if ondelete is not Undefined:
            if foreign_key is Undefined:
                raise RuntimeError("ondelete can only be used with foreign_key")
        super().__init__(default=default, **kwargs)
        self.primary_key = primary_key
        self.nullable = nullable
        self.foreign_key = foreign_key
        self.ondelete = ondelete
        self.unique = unique
        self.index = index
        self.sa_type = sa_type
        self.sa_column = sa_column
        self.sa_column_args = sa_column_args
        self.sa_column_kwargs = sa_column_kwargs


@dataclass
class FieldInfoMetadata:
    primary_key: bool | UndefinedType = Undefined
    nullable: bool | UndefinedType = Undefined
    foreign_key: Any = Undefined
    ondelete: OnDeleteType | UndefinedType = Undefined
    unique: bool | UndefinedType = Undefined
    index: bool | UndefinedType = Undefined
    sa_type: SaTypeOrInstance | UndefinedType = Undefined
    sa_column: Column[Any] | UndefinedType = Undefined
    sa_column_args: Sequence[Any] | UndefinedType = Undefined
    sa_column_kwargs: Mapping[str, Any] | UndefinedType = Undefined


def _get_sqlmodel_field_metadata(field_info: Any) -> FieldInfoMetadata | None:
    metadata_items = getattr(field_info, "metadata", None)
    if metadata_items:
        for meta in metadata_items:
            if isinstance(meta, FieldInfoMetadata):
                return meta
    return None


def _get_sqlmodel_field_value(
    field_info: Any, attribute: str, default: Any = Undefined
) -> Any:
    metadata = _get_sqlmodel_field_metadata(field_info)
    if metadata is not None and hasattr(metadata, attribute):
        return getattr(metadata, attribute)
    return getattr(field_info, attribute, default)


# include sa_type, sa_column_args, sa_column_kwargs
@overload
def Field(
    default: Any = Undefined,
    *,
    default_factory: NoArgAnyCallable | None = None,
    alias: str | None = None,
    validation_alias: str | None = None,
    serialization_alias: str | None = None,
    title: str | None = None,
    description: str | None = None,
    exclude: Set[int | str] | Mapping[int | str, Any] | Any = None,
    include: Set[int | str] | Mapping[int | str, Any] | Any = None,
    const: bool | None = None,
    gt: float | None = None,
    ge: float | None = None,
    lt: float | None = None,
    le: float | None = None,
    multiple_of: float | None = None,
    max_digits: int | None = None,
    decimal_places: int | None = None,
    min_items: Annotated[
        int | None,
        deprecated(MIN_ITEMS_DEPRECATION_MSG),
    ] = None,
    max_items: Annotated[
        int | None,
        deprecated(MAX_ITEMS_DEPRECATION_MSG),
    ] = None,
    unique_items: bool | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    allow_mutation: bool = True,
    regex: str | None = None,
    discriminator: str | Discriminator | None = None,
    repr: bool = True,
    primary_key: bool | UndefinedType = Undefined,
    foreign_key: Any = Undefined,
    unique: bool | UndefinedType = Undefined,
    nullable: bool | UndefinedType = Undefined,
    index: bool | UndefinedType = Undefined,
    sa_type: SaTypeOrInstance | UndefinedType = Undefined,
    sa_column_args: Sequence[Any] | UndefinedType = Undefined,
    sa_column_kwargs: Mapping[str, Any] | UndefinedType = Undefined,
    schema_extra: dict[str, Any] | None = None,
) -> Any: ...


# When foreign_key is str, include ondelete
# include sa_type, sa_column_args, sa_column_kwargs
@overload
def Field(
    default: Any = Undefined,
    *,
    default_factory: NoArgAnyCallable | None = None,
    alias: str | None = None,
    validation_alias: str | None = None,
    serialization_alias: str | None = None,
    title: str | None = None,
    description: str | None = None,
    exclude: Set[int | str] | Mapping[int | str, Any] | Any = None,
    include: Set[int | str] | Mapping[int | str, Any] | Any = None,
    const: bool | None = None,
    gt: float | None = None,
    ge: float | None = None,
    lt: float | None = None,
    le: float | None = None,
    multiple_of: float | None = None,
    max_digits: int | None = None,
    decimal_places: int | None = None,
    min_items: Annotated[
        int | None,
        deprecated(MIN_ITEMS_DEPRECATION_MSG),
    ] = None,
    max_items: Annotated[
        int | None,
        deprecated(MAX_ITEMS_DEPRECATION_MSG),
    ] = None,
    unique_items: bool | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    allow_mutation: bool = True,
    regex: str | None = None,
    discriminator: str | Discriminator | None = None,
    repr: bool = True,
    primary_key: bool | UndefinedType = Undefined,
    foreign_key: str,
    ondelete: OnDeleteType | UndefinedType = Undefined,
    unique: bool | UndefinedType = Undefined,
    nullable: bool | UndefinedType = Undefined,
    index: bool | UndefinedType = Undefined,
    sa_type: SaTypeOrInstance | UndefinedType = Undefined,
    sa_column_args: Sequence[Any] | UndefinedType = Undefined,
    sa_column_kwargs: Mapping[str, Any] | UndefinedType = Undefined,
    schema_extra: dict[str, Any] | None = None,
) -> Any: ...


# Include sa_column, don't include
# primary_key
# foreign_key
# ondelete
# unique
# nullable
# index
# sa_type
# sa_column_args
# sa_column_kwargs
@overload
def Field(
    default: Any = Undefined,
    *,
    default_factory: NoArgAnyCallable | None = None,
    alias: str | None = None,
    validation_alias: str | None = None,
    serialization_alias: str | None = None,
    title: str | None = None,
    description: str | None = None,
    exclude: Set[int | str] | Mapping[int | str, Any] | Any = None,
    include: Set[int | str] | Mapping[int | str, Any] | Any = None,
    const: bool | None = None,
    gt: float | None = None,
    ge: float | None = None,
    lt: float | None = None,
    le: float | None = None,
    multiple_of: float | None = None,
    max_digits: int | None = None,
    decimal_places: int | None = None,
    min_items: Annotated[
        int | None,
        deprecated(MIN_ITEMS_DEPRECATION_MSG),
    ] = None,
    max_items: Annotated[
        int | None,
        deprecated(MAX_ITEMS_DEPRECATION_MSG),
    ] = None,
    unique_items: bool | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    allow_mutation: bool = True,
    regex: str | None = None,
    discriminator: str | Discriminator | None = None,
    repr: bool = True,
    sa_column: Column[Any] | UndefinedType = Undefined,
    schema_extra: dict[str, Any] | None = None,
) -> Any: ...


def Field(
    default: Any = Undefined,
    *,
    default_factory: NoArgAnyCallable | None = None,
    alias: str | None = None,
    validation_alias: str | None = None,
    serialization_alias: str | None = None,
    title: str | None = None,
    description: str | None = None,
    exclude: Set[int | str] | Mapping[int | str, Any] | Any = None,
    include: Set[int | str] | Mapping[int | str, Any] | Any = None,
    const: bool | None = None,
    gt: float | None = None,
    ge: float | None = None,
    lt: float | None = None,
    le: float | None = None,
    multiple_of: float | None = None,
    max_digits: int | None = None,
    decimal_places: int | None = None,
    min_items: Annotated[
        int | None,
        deprecated(MIN_ITEMS_DEPRECATION_MSG),
    ] = None,
    max_items: Annotated[
        int | None,
        deprecated(MAX_ITEMS_DEPRECATION_MSG),
    ] = None,
    unique_items: bool | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    allow_mutation: bool = True,
    regex: str | None = None,
    discriminator: str | Discriminator | None = None,
    repr: bool = True,
    primary_key: bool | UndefinedType = Undefined,
    foreign_key: Any = Undefined,
    ondelete: OnDeleteType | UndefinedType = Undefined,
    unique: bool | UndefinedType = Undefined,
    nullable: bool | UndefinedType = Undefined,
    index: bool | UndefinedType = Undefined,
    sa_type: SaTypeOrInstance | UndefinedType = Undefined,
    sa_column: Column | UndefinedType = Undefined,
    sa_column_args: Sequence[Any] | UndefinedType = Undefined,
    sa_column_kwargs: Mapping[str, Any] | UndefinedType = Undefined,
    schema_extra: dict[str, Any] | None = None,
) -> Any:
    current_schema_extra = schema_extra or {}

    if min_items is not None:
        warnings.warn(MIN_ITEMS_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)
        if min_length is None:
            min_length = min_items
    if max_items is not None:
        warnings.warn(MAX_ITEMS_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)
        if max_length is None:
            max_length = max_items

    # Extract possible alias settings from schema_extra so we can control precedence
    schema_validation_alias = current_schema_extra.pop("validation_alias", None)
    schema_serialization_alias = current_schema_extra.pop("serialization_alias", None)
    field_info_kwargs = {
        "alias": alias,
        "title": title,
        "description": description,
        "exclude": exclude,
        "include": include,
        "const": const,
        "gt": gt,
        "ge": ge,
        "lt": lt,
        "le": le,
        "multiple_of": multiple_of,
        "max_digits": max_digits,
        "decimal_places": decimal_places,
        "unique_items": unique_items,
        "min_length": min_length,
        "max_length": max_length,
        "allow_mutation": allow_mutation,
        "regex": regex,
        "discriminator": discriminator,
        "repr": repr,
        "primary_key": primary_key,
        "foreign_key": foreign_key,
        "ondelete": ondelete,
        "unique": unique,
        "nullable": nullable,
        "index": index,
        "sa_type": sa_type,
        "sa_column": sa_column,
        "sa_column_args": sa_column_args,
        "sa_column_kwargs": sa_column_kwargs,
        **current_schema_extra,
    }

    # explicit params > schema_extra > alias propagation
    field_info_kwargs["validation_alias"] = (
        validation_alias or schema_validation_alias or alias
    )
    field_info_kwargs["serialization_alias"] = (
        serialization_alias or schema_serialization_alias or alias
    )

    field_info = FieldInfo(
        default,
        default_factory=default_factory,
        **field_info_kwargs,
    )
    field_metadata = FieldInfoMetadata(
        primary_key=primary_key,
        nullable=nullable,
        foreign_key=foreign_key,
        ondelete=ondelete,
        unique=unique,
        index=index,
        sa_type=sa_type,
        sa_column=sa_column,
        sa_column_args=sa_column_args,
        sa_column_kwargs=sa_column_kwargs,
    )
    if hasattr(field_info, "metadata"):
        field_info.metadata.append(field_metadata)
    return field_info


def get_sqlalchemy_type(field: Any) -> Any:
    field_info = field
    sa_type = _get_sqlmodel_field_value(field_info, "sa_type", Undefined)  # noqa: B009
    if sa_type is not Undefined:
        return sa_type

    type_ = get_sa_type_from_field(field)
    metadata = get_field_metadata(field)

    # Check enums first as an enum can also be a str, needed by Pydantic/FastAPI
    if issubclass(type_, Enum):
        return sa_Enum(type_)
    if issubclass(
        type_,
        (
            str,
            ipaddress.IPv4Address,
            ipaddress.IPv4Network,
            ipaddress.IPv6Address,
            ipaddress.IPv6Network,
            Path,
            EmailStr,
        ),
    ):
        max_length = getattr(metadata, "max_length", None)
        if max_length:
            return AutoString(length=max_length)
        return AutoString
    if issubclass(type_, float):
        return Float
    if issubclass(type_, bool):
        return Boolean
    if issubclass(type_, int):
        return Integer
    if issubclass(type_, (datetime, AwareDatetime, NaiveDatetime)):
        if issubclass(type_, cast(type, NaiveDatetime)):
            return DateTime(timezone=False)
        return UTCDateTime()
    if issubclass(type_, date):
        return Date
    if issubclass(type_, timedelta):
        return Interval
    if issubclass(type_, time):
        return Time
    if issubclass(type_, bytes):
        return LargeBinary
    if issubclass(type_, Decimal):
        return Numeric(
            precision=getattr(metadata, "max_digits", None),
            scale=getattr(metadata, "decimal_places", None),
        )
    if issubclass(type_, uuid.UUID):
        return Uuid
    raise ValueError(f"{type_} has no matching SQLAlchemy type")


@dataclass
class FieldMapping:
    """Derive a SQLAlchemy column from the original Pydantic field."""

    field: PydanticFieldInfo

    def to_column(self) -> Column:
        sa_column = self.option("sa_column")
        if isinstance(sa_column, Column):
            return sa_column
        sa_type = get_sqlalchemy_type(self.field)
        return Column(*self.column_args, type_=sa_type, **self.column_kwargs)

    def option(self, name: str, default: Any = Undefined) -> Any:
        value = _get_sqlmodel_field_value(self.field, name, Undefined)
        return default if value is Undefined else value

    @property
    def primary_key(self) -> bool:
        return self.option("primary_key", False)

    @property
    def nullable(self) -> bool:
        # Override derived nullability if the nullable property is set explicitly
        # on the field
        field_nullable = self.option("nullable")
        if field_nullable is not Undefined:
            assert not isinstance(field_nullable, UndefinedType)
            return field_nullable
        return not self.primary_key and is_field_noneable(self.field)

    @property
    def column_args(self) -> list[Any]:
        args: list[Any] = []
        foreign_key = self.option("foreign_key", None)
        if foreign_key:
            ondelete_value = self.option("ondelete", None)
            if ondelete_value == "SET NULL" and not self.nullable:
                raise RuntimeError('ondelete="SET NULL" requires nullable=True')
            assert isinstance(foreign_key, str)
            assert isinstance(ondelete_value, (str, type(None)))  # for typing
            args.append(ForeignKey(foreign_key, ondelete=ondelete_value))
        sa_column_args = self.option("sa_column_args")
        if sa_column_args is not Undefined:
            args.extend(list(cast(Sequence[Any], sa_column_args)))
        return args

    @property
    def column_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "primary_key": self.primary_key,
            "nullable": self.nullable,
            "index": self.option("index", False),
            "unique": self.option("unique", False),
        }
        sa_default = self.default
        if sa_default is not Undefined:
            kwargs["default"] = sa_default
        sa_column_kwargs = self.option("sa_column_kwargs")
        if sa_column_kwargs is not Undefined:
            kwargs.update(cast(dict[Any, Any], sa_column_kwargs))
        return kwargs

    @property
    def default(self) -> Any:
        if self.field.default_factory:
            return self.field.default_factory
        return self.field.default


def get_column_from_field(field: Any) -> Column:
    return FieldMapping(field).to_column()


def is_field_noneable(field: PydanticFieldInfo) -> bool:
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
