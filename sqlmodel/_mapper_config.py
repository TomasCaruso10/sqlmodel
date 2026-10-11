"""Resolve field-declared polymorphism before constructing the model schema."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pydantic.fields import FieldInfo
from sqlalchemy.orm import Mapper
from typing_extensions import Self

from ._compat import Undefined
from ._fields import FieldMapping


@dataclass
class MapperConfig:
    """Share a resolved discriminator identity between Pydantic and SQLAlchemy."""

    discriminator: str
    identity: Any
    inherited: bool
    options: dict[str, Any]

    @classmethod
    def from_fields(
        cls,
        name: str,
        fields: Mapping[str, FieldInfo],
        parent: Mapper[Any] | None,
        options: Mapping[str, Any],
    ) -> Self | None:
        marked = [
            key
            for key, info in fields.items()
            if FieldMapping(info).option("polymorphic", False)
        ]
        if len(marked) > 1:
            raise ValueError("Only one field can declare polymorphic=True.")
        inherited = parent is not None and parent.polymorphic_on is not None
        discriminator = (
            parent.get_property_by_column(parent.polymorphic_on).key
            if inherited
            else None
        )
        if marked:
            if inherited and marked[0] != discriminator:
                raise ValueError(
                    "A subclass must use its parent's discriminator field."
                )
            discriminator = marked[0]
        if discriminator is None:
            if any(
                FieldMapping(info).option("polymorphic_identity") is not Undefined
                for info in fields.values()
            ):
                raise ValueError("polymorphic_identity requires a discriminator field.")
            return None

        # Native mapper options remain the final authority.
        discriminator = options.get("polymorphic_on", discriminator)
        if not isinstance(discriminator, str):
            discriminator = discriminator.key
        for key, info in fields.items():
            if (
                key != discriminator
                and FieldMapping(info).option("polymorphic_identity") is not Undefined
            ):
                raise ValueError(
                    "polymorphic_identity belongs on the discriminator field."
                )
        info = fields.get(discriminator)
        identity = (
            FieldMapping(info).option("polymorphic_identity") if info else Undefined
        )
        if identity is Undefined:
            identity = name.lower()
        identity = options.get("polymorphic_identity", identity)
        resolved = {"polymorphic_identity": identity, **options}
        if not inherited:
            resolved.setdefault("polymorphic_on", discriminator)
        return cls(discriminator, identity, inherited, resolved)

    def prepare_field(self, field: FieldInfo) -> FieldInfo:
        """Set the resolved identity as a per-class Pydantic default."""
        field = deepcopy(field)
        field.default = self.identity
        field.default_factory = None
        # Pydantic uses explicit attributes when merging Annotated/Field metadata.
        field._attributes_set.update(default=self.identity, default_factory=None)
        return field
