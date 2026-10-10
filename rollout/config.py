from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class EngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warehouse_path: Path = Path(".rollout/warehouse.duckdb")


class LoadStrategy(Enum):
    FULL = "FULL"


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    load_strategy: LoadStrategy = LoadStrategy.FULL
