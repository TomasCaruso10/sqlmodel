"""Resolve SQLModel options without consuming Pydantic's configuration input."""

from __future__ import annotations

from collections import ChainMap
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from typing_extensions import Self

from ._compat import BaseConfig, Undefined

if TYPE_CHECKING:
    from pydantic_core import PydanticUndefined as Undefined


@dataclass(frozen=True)
class ModelConfig:
    """Read declaration options with SQLModel's existing precedence rules."""

    values: Mapping[str, Any]
    kwargs: Mapping[str, Any]

    @classmethod
    def from_declaration(
        cls,
        bases: tuple[type[Any], ...],
        namespace: dict[str, Any],
        kwargs: dict[str, Any],
    ) -> Self:
        legacy = namespace.get("Config")
        legacy_options = {
            name: getattr(legacy, name)
            for name in ("table", "registry")
            if hasattr(legacy, name)
        }
        # Earlier mappings take precedence. Among bases, the last base wins.
        values = ChainMap(
            namespace.get("model_config", {}),
            legacy_options,
            *(getattr(base, "model_config", {}) for base in reversed(bases)),
        )
        return cls(values=values, kwargs=kwargs)

    def with_model_config(self, values: Mapping[str, Any]) -> Self:
        """Use Pydantic's completed configuration after constructing the class."""
        return replace(self, values=values)

    @property
    def table(self) -> Any:
        return self.option("table")

    @property
    def is_table(self) -> bool:
        return self.table is True

    @property
    def registry(self) -> Any:
        return self.option("registry")

    @property
    def pydantic_kwargs(self) -> dict[str, Any]:
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
        return {
            key: self.kwargs[key] for key in self.kwargs.keys() & allowed_config_kwargs
        }

    def option(self, name: str) -> Any:
        value = self.values.get(name, Undefined)
        return self.kwargs.get(name, Undefined) if value is Undefined else value
