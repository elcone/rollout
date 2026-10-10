from dataclasses import dataclass, field
from pathlib import Path

from rollout.config import ModelConfig


@dataclass(frozen=True, order=True)
class ModelId:
    schema: str
    name: str
    catalog: str = ""

    def __str__(self) -> str:
        return ".".join(part for part in (self.catalog, self.schema, self.name) if part)


@dataclass(frozen=True)
class Model:
    id: ModelId
    path: Path
    sql: str
    dependencies: frozenset[ModelId] = field(default_factory=frozenset)
    config: ModelConfig = field(default_factory=ModelConfig)
