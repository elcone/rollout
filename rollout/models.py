"""Model records and SQL identities shared by discovery and graph analysis."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, order=True)
class ModelId:
    """Case-preserving identity of a local model or an external table.

    Attributes:
        schema: SQL schema name; unqualified references use public.
        name: Model or table name.
        catalog: Optional SQL catalog; an empty string means no catalog.
    """
    schema: str
    name: str
    catalog: str = ""

    def __str__(self) -> str:
        """Format the identity as a dot-separated SQL name.

        Returns:
            Non-empty catalog, schema, and name components joined by dots.
        """
        return ".".join(part for part in (self.catalog, self.schema, self.name) if part)


@dataclass(frozen=True)
class Model:
    """SQL model discovered from a file, optionally enriched by the parser.

    Attributes:
        name: Filename without the .sql extension.
        schema: Parent schema directory, or public for root-level files.
        path: Source file path.
        sql: UTF-8 source text.
        dependencies: Referenced table identities; empty before parsing.
    """
    name: str
    schema: str
    path: Path
    sql: str
    dependencies: frozenset[ModelId] = field(default_factory=frozenset)

    @property
    def id(self) -> ModelId:
        """ModelId: Local model identity, with no catalog qualification."""
        return ModelId(self.schema, self.name)

    @property
    def qualified_name(self) -> str:
        """str: Model identity formatted as schema.name."""
        return str(self.id)
