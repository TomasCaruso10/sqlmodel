"""Allow omitted required foreign keys only when an ORM relationship supplies them."""

from collections.abc import Mapping
from typing import Any

from pydantic_core import PydanticCustomError, core_schema
from sqlalchemy import inspect
from sqlalchemy.orm import MANYTOONE

from ._compat import get_model_fields


class ForeignKeyRules:
    """Keep field requirements separate from relationship-driven FK assignment."""

    def __init__(self, model: Any, foreign_keys: set[str]) -> None:
        self._model = model
        self._required = {
            name
            for name, field in get_model_fields(model).items()
            if name in foreign_keys and field.is_required()
        }
        self._relationships: dict[str, tuple[type[Any], set[str]]] | None = None

    def apply(self, schema: core_schema.ModelFieldsSchema) -> core_schema.CoreSchema:
        if not self._required:
            return schema
        fields = dict(schema["fields"])
        for name in self._required:
            field = fields[name]
            # Bypass Pydantic's unconditional missing-field error, then check
            # requiredness against supplied relationships in validate(). Change
            # only the core schema: FieldInfo still defines the original database
            # type and NOT NULL constraint. Explicit values keep their validation;
            # an omitted FK stays None until SQLAlchemy fills it during flush.
            fields[name] = {
                **field,
                "schema": core_schema.with_default_schema(
                    field["schema"], default=None, validate_default=False
                ),
            }
        # JSON cannot carry ORM instances, so retain its original requirements
        # and the corresponding JSON Schema.
        return core_schema.json_or_python_schema(
            json_schema=schema, python_schema={**schema, "fields": fields}
        )

    def _collect_relationships(self) -> dict[str, tuple[type[Any], set[str]]]:
        mapper = inspect(self._model)
        rules = {}
        for relation in mapper.relationships:
            if relation.direction is not MANYTOONE or relation.viewonly:
                continue
            keys = {
                mapper.get_property_by_column(column).key
                for _, column in relation.synchronize_pairs
            }
            rules[relation.key] = (relation.mapper.class_, keys & self._required)
        return rules

    def validate(
        self,
        fields: Mapping[str, Any],
        fields_set: set[str],
        relationships: Mapping[str, Any],
    ) -> None:
        missing = (self._required & fields.keys()) - fields_set
        if not missing:
            return
        # Mappers are ready at instance validation, after class/schema creation.
        # Resolve these rules once per schema, not once per field or instance.
        if self._relationships is None:
            self._relationships = self._collect_relationships()
        for name, (target, keys) in self._relationships.items():
            if isinstance(relationships.get(name), target):
                missing -= keys
        if missing:
            raise PydanticCustomError(
                "missing_foreign_key",
                "Provide values or relationships for required foreign keys: {fields}",
                {"fields": ", ".join(sorted(missing))},
            )
