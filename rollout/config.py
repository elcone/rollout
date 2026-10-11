from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class EngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warehouse_path: Path = Path(".rollout/warehouse.duckdb")


class LoadStrategy(Enum):
    FULL = "FULL"


class CheckSeverity(Enum):
    WARNING = "WARNING"
    ERROR = "ERROR"


Count = Annotated[StrictInt, Field(ge=0)]
Column = Annotated[str, Field(strict=True, min_length=1)]


class RowCountCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["row_count"]
    severity: CheckSeverity
    min: Count


class NullnessCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["nullness"]
    severity: CheckSeverity
    column: Column
    max: Count


class UniquenessCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["uniqueness"]
    severity: CheckSeverity
    columns: Annotated[tuple[Column, ...], Field(min_length=1)]
    max: Count


QualityCheck = Annotated[
    RowCountCheck | NullnessCheck | UniquenessCheck, Field(discriminator="type")
]


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    load_strategy: LoadStrategy = LoadStrategy.FULL
    quality: tuple[QualityCheck, ...] = ()
