"""Separate Pydantic fields from SQLAlchemy relationships in a declaration."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Any

from typing_extensions import Self

from ._compat import get_annotations
from ._relationships import RelationshipInfo


@dataclass
class ModelNamespace:
    """Keep the declaration and the views used while constructing its model."""

    namespace: dict[str, Any]
    annotations: dict[str, Any]

    @classmethod
    def from_declaration(cls, namespace: dict[str, Any]) -> Self:
        return cls(namespace=namespace, annotations=get_annotations(namespace))

    @cached_property
    def relationships(self) -> dict[str, RelationshipInfo]:
        return {
            name: value
            for name, value in self.namespace.items()
            if isinstance(value, RelationshipInfo)
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
        return {
            **{
                name: value
                for name, value in self.namespace.items()
                if name not in self.relationships
            },
            "__weakref__": None,
            "__sqlmodel_relationships__": self.relationships,
            "__annotations__": self.field_annotations,
        }

    def annotations_for(self, model: type[Any]) -> dict[str, Any]:
        """Restore relationship annotations, preserving Pydantic's final values."""
        return {
            **self.relationship_annotations,
            **self.pydantic_namespace["__annotations__"],
            **model.__annotations__,
        }
