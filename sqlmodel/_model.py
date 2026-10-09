"""Common SQLModel behavior, extended by the table mixin when mapping a model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from ._compat import ModelMetaclass, SQLModelConfig
from ._relationships import RelationshipInfo

if TYPE_CHECKING:
    from pydantic import BaseModel as ModelBase
else:
    # These methods cooperate with BaseModel through SQLModel's MRO. The mixin
    # itself must not be a Pydantic model or contribute model configuration.
    ModelBase = object


class ModelMixin(ModelBase):
    """Provide the non-table implementations of the shared model hooks."""

    __slots__ = ()
    model_config: ClassVar[SQLModelConfig]
    __sqlmodel_relationships__: ClassVar[dict[str, RelationshipInfo]]

    @classmethod
    def _configure_model(cls, *, table: bool) -> None:
        """Data models do not need SQLAlchemy columns."""

    @classmethod
    def _initialize_class(
        cls, classname: str, bases: tuple[type, ...], dict_: dict[str, Any], **kw: Any
    ) -> None:
        ModelMetaclass.__init__(cls, classname, bases, dict_, **kw)

    @classmethod
    def _set_class_attribute(cls, name: str, value: Any) -> None:
        type.__setattr__(cls, name, value)

    @classmethod
    def _delete_class_attribute(cls, name: str) -> None:
        type.__delattr__(cls, name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Relationships belong to SQLAlchemy; Pydantic manages other attributes.
        if name not in self.__sqlmodel_relationships__:
            super().__setattr__(name, value)
