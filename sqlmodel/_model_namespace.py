"""Separate Pydantic fields from SQLAlchemy relationships in a declaration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from functools import cached_property
from typing import Annotated, Any, ClassVar, get_origin

from pydantic.fields import FieldInfo
from typing_extensions import Self

from ._compat import Undefined, get_annotations, get_model_fields, is_table_model_class
from ._fields import FieldMapping
from ._mapper_config import MapperConfig
from ._relationships import RelationshipInfo


@dataclass
class InheritedField:
    """Expose a parent's field definition while Pydantic collects child fields.

    Pydantic removes this descriptor after collecting the field. SQLAlchemy can
    then inherit the parent's mapped attribute without an intervening proxy.
    """

    field: FieldInfo

    def __get__(self, instance: Any, owner: type[Any] | None = None) -> FieldInfo:
        return self.field


@dataclass
class ModelNamespace:
    """Keep the declaration and the views used while constructing its model."""

    namespace: dict[str, Any]
    annotations: dict[str, Any]
    inherited_fields: dict[str, FieldInfo] = field(default_factory=dict)
    inherited_relationships: dict[str, RelationshipInfo] = field(default_factory=dict)
    mapper_config: MapperConfig | None = None

    @classmethod
    def from_declaration(
        cls, namespace: dict[str, Any], bases: tuple[type[Any], ...] = ()
    ) -> Self:
        declaration = cls(
            namespace=dict(namespace), annotations=get_annotations(namespace)
        )
        if "__tablename__" in namespace:
            for base in reversed(bases):
                if is_table_model_class(base):
                    declaration.inherited_fields.update(get_model_fields(base))
                    declaration.inherited_relationships.update(
                        base.__sqlmodel_relationships__
                    )
        return declaration

    def prepare_mapper(self, name: str, bases: tuple[type[Any], ...]) -> None:
        """Resolve field options once, before Pydantic compiles its defaults."""
        fields = {
            key: FieldInfo.from_annotated_attribute(
                annotation, self.namespace.get(key, FieldInfo())
            )
            for key, annotation in self.annotations.items()
            if isinstance(self.namespace.get(key), FieldInfo)
            or get_origin(annotation) is Annotated
        }
        available = {
            key: info
            for base in reversed(bases)
            for key, info in getattr(base, "model_fields", {}).items()
        }
        available.update(fields)
        if not any(
            FieldMapping(info).option("polymorphic", False)
            or FieldMapping(info).option("polymorphic_identity") is not Undefined
            for info in available.values()
        ):
            return
        parent = next(
            (base.__mapper__ for base in bases if is_table_model_class(base)), None
        )
        self.mapper_config = MapperConfig.from_fields(
            name,
            fields if parent is not None else available,
            parent,
            self.namespace.get("__mapper_args__", {}),
        )
        if self.mapper_config is None:
            return
        discriminator = self.mapper_config.discriminator
        self.namespace[discriminator] = self.mapper_config.prepare_field(
            available[discriminator]
        )
        self.namespace["__mapper_args__"] = self.mapper_config.options

    @cached_property
    def relationships(self) -> dict[str, RelationshipInfo]:
        return {
            **self.inherited_relationships,
            **{
                name: value
                for name, value in self.namespace.items()
                if isinstance(value, RelationshipInfo)
            },
        }

    @cached_property
    def field_annotations(self) -> dict[str, Any]:
        return {
            name: annotation
            for name, annotation in self.annotations.items()
            if name not in self.relationships
        }

    @cached_property
    def relationship_annotations(self) -> dict[str, Any]:
        return {
            name: annotation
            for name, annotation in self.annotations.items()
            if name in self.relationships
        }

    @cached_property
    def pydantic_namespace(self) -> dict[str, Any]:
        # SQLAlchemy replaced inherited defaults with InstrumentedAttributes.
        # Give Pydantic the original fields instead of those ORM descriptors.
        inherited = {
            name: InheritedField(deepcopy(info))
            for name, info in self.inherited_fields.items()
            if name not in self.annotations and name not in self.namespace
        }
        if (
            self.mapper_config is not None
            and self.mapper_config.discriminator not in self.annotations
        ):
            name = self.mapper_config.discriminator
            inherited[name] = InheritedField(self.namespace[name])
        return {
            **{
                name: value
                for name, value in self.namespace.items()
                if name not in self.relationships
            },
            **inherited,
            "__weakref__": None,
            "__sqlmodel_relationships__": self.relationships,
            "__annotations__": {
                # Inherited ORM relationships remain outside Pydantic's fields.
                **dict.fromkeys(self.inherited_relationships, ClassVar[Any]),
                **self.field_annotations,
            },
        }

    def annotations_for(self, model: type[Any]) -> dict[str, Any]:
        """Restore relationship annotations, preserving Pydantic's final values."""
        return {
            **self.relationship_annotations,
            **{name: model.__annotations__[name] for name in self.field_annotations},
        }
