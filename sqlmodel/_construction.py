from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, overload

from pydantic import BaseModel
from sqlalchemy.orm.attributes import set_attribute
from sqlalchemy.orm.instrumentation import is_instrumented

# Only this instance is receiving prepared fields without running validation.
instance_from_fields: ContextVar[BaseModel | None] = ContextVar(
    "instance_from_fields", default=None
)


@contextmanager
def initializing_from_fields(instance: BaseModel) -> Iterator[None]:
    """Mark this instance as receiving prepared fields during initialization."""
    token = instance_from_fields.set(instance)
    try:
        yield
    finally:
        instance_from_fields.reset(token)


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
        self, instance: None, owner: type[BaseModel] | None = None
    ) -> "InstanceDictProxy": ...

    @overload
    def __get__(
        self, instance: BaseModel, owner: type[BaseModel] | None = None
    ) -> dict[str, Any]: ...

    def __get__(
        self, instance: BaseModel | None, owner: type[BaseModel] | None = None
    ) -> Any:
        if instance is None:
            return self
        return self.storage.__get__(instance, owner)

    def __set__(self, instance: BaseModel, values: dict[str, Any]) -> None:
        """Deliver prepared fields without replacing SQLAlchemy's state."""
        current = self.storage.__get__(instance, type(instance))
        if not self.has_orm_state(current) or self.receives_different_orm_state(
            current, values
        ):
            self.storage.__set__(instance, values)
            return
        self.deliver_fields(instance, current, values)

    @staticmethod
    def has_orm_state(values: dict[str, Any]) -> bool:
        return "_sa_instance_state" in values

    @staticmethod
    def receives_different_orm_state(
        current: dict[str, Any], values: dict[str, Any]
    ) -> bool:
        # Assignment keeps this instance's state in the validated dictionary.
        # A different state belongs to a dictionary restoration, not an assignment.
        return (
            "_sa_instance_state" in values
            and values["_sa_instance_state"] is not current["_sa_instance_state"]
        )

    @staticmethod
    def deliver_fields(
        instance: BaseModel, current: dict[str, Any], values: dict[str, Any]
    ) -> None:
        for name, value in values.items():
            if name in current and current[name] is value:
                continue
            if is_instrumented(instance, name):
                set_attribute(instance, name, value)
            else:
                current[name] = value
