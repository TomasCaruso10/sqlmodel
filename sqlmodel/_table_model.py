"""Class mapping and instance behavior for SQLModel table declarations."""

from __future__ import annotations

import builtins
from copy import deepcopy
from functools import update_wrapper, wraps
from typing import Any, cast, get_origin

from pydantic import BaseModel
from sqlalchemy.orm import Mapped
from sqlalchemy.orm.attributes import set_attribute
from sqlalchemy.orm.decl_api import DeclarativeMeta
from sqlalchemy.orm.instrumentation import is_instrumented
from typing_extensions import Self

from ._compat import (
    Undefined,
    get_model_fields,
    init_pydantic_private_attrs,
    is_table_model_class,
)
from ._construction import InstanceDictProxy, instance_from_fields
from ._fields import get_column_from_field
from ._model import ModelMixin


class TableMixin(ModelMixin):
    """Add SQLAlchemy mapping and instrumentation to the common model hooks.

    A descendant can disable ``table`` while inheriting this mixin. Guards keep
    that existing SQLModel behavior without removing its declared parent class.
    """

    __slots__ = ()

    __dict__ = InstanceDictProxy()

    @classmethod
    def _configure_model(cls, *, table: bool) -> None:
        if not table:
            return
        # If it was passed by kwargs, ensure it's also set in config
        cls.model_config["table"] = True
        for name, field in get_model_fields(cls).items():
            column = get_column_from_field(field)
            setattr(cls, name, column)
        # Set a config flag to tell FastAPI that this should be read with a field
        # in orm_mode instead of preemptively converting it to a dict.
        # This could be done by reading cls.model_config['table'] in FastAPI, but
        # that's very specific about SQLModel, so let's have another config that
        # other future tools based on Pydantic can use.
        cls.model_config["read_from_attributes"] = True  # ty: ignore[invalid-key]
        # For compatibility with older versions
        # TODO: remove this in the future
        cls.model_config["read_with_orm_mode"] = True  # ty: ignore[invalid-key]

    @classmethod
    def _initialize_class(
        cls, classname: str, bases: tuple[type, ...], dict_: dict[str, Any], **kw: Any
    ) -> None:
        if not cls._requires_mapping(bases):
            return super()._initialize_class(classname, bases, dict_, **kw)

        cls._install_relationships()
        # SQLAlchemy no longer uses dict_
        # Ref: https://github.com/sqlalchemy/sqlalchemy/commit/428ea01f00a9cc7f85e435018565eb6da7af1b77
        # Tag: 1.4.36
        DeclarativeMeta.__init__(
            cast(DeclarativeMeta, cls), classname, bases, dict_, **kw
        )

    @classmethod
    def _requires_mapping(cls, bases: tuple[type, ...]) -> bool:
        # Only one of the base classes (or the current one) should be a table model
        # this allows FastAPI cloning a SQLModel for the response_model without
        # trying to create a new SQLAlchemy, for a new table, with the same name, that
        # triggers an error
        base_is_table = any(is_table_model_class(base) for base in bases)
        return is_table_model_class(cls) and not base_is_table

    @classmethod
    def _install_relationships(cls) -> None:
        for name, info in cls.__sqlmodel_relationships__.items():
            if info.sa_relationship:
                # There's a SQLAlchemy relationship declared, that takes precedence
                # over anything else, use that and continue with the next attribute
                setattr(cls, name, info.sa_relationship)  # Fix #315
                continue
            annotation = cls._prepare_relationship_annotation(name)
            relationship = info.from_annotation(annotation)
            setattr(cls, name, relationship)  # Fix #315

    @classmethod
    def _prepare_relationship_annotation(cls, name: str) -> Any:
        annotation = cls.__annotations__[name]
        if get_origin(annotation) is Mapped:
            return annotation.__args__[0]
        # Plain forward references, for models not yet defined, are not
        # handled well by SQLAlchemy without Mapped, so, wrap the
        # annotations in Mapped here
        cls.__annotations__[name] = Mapped[annotation]
        return annotation

    @classmethod
    def _set_class_attribute(cls, name: str, value: Any) -> None:
        DeclarativeMeta.__setattr__(cast(DeclarativeMeta, cls), name, value)

    @classmethod
    def _delete_class_attribute(cls, name: str) -> None:
        DeclarativeMeta.__delattr__(cast(DeclarativeMeta, cls), name)

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
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        # Wait until Pydantic has prepared model_post_init, including private attributes.
        # Pydantic calls model_post_init from both validation and model_construct.
        # Preserve user overrides while preparing the ORM before they run.
        post_init = cls.model_post_init
        if post_init is not TableMixin.model_post_init:

            @wraps(post_init)
            def model_post_init(self: TableMixin, context: Any) -> None:
                self._initialize_orm()
                post_init(self, context)

            cls.model_post_init = model_post_init

    def model_post_init(self, context: Any) -> None:
        self._initialize_orm()
        super().model_post_init(context)

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

    def __deepcopy__(self: Self, memo: builtins.dict[int, Any] | None = None) -> Self:
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
