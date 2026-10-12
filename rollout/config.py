from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


class EngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warehouse_path: Path = Path(".rollout/warehouse.duckdb")


class LoadStrategy(Enum):
    FULL = "FULL"
    APPEND = "APPEND"
    DELETE_INSERT = "DELETE_INSERT"


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


class DeleteInsertConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    where: str

    @field_validator("where")
    @classmethod
    def require_nonempty_predicate(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("delete_insert.where must not be empty")
        return value


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    load_strategy: LoadStrategy = LoadStrategy.FULL
    delete_insert: DeleteInsertConfig | None = None
    quality: tuple[QualityCheck, ...] = ()

    @model_validator(mode="after")
    def validate_strategy_config(self) -> "ModelConfig":
        if (
            self.load_strategy is not LoadStrategy.DELETE_INSERT
            and self.delete_insert is not None
        ):
            raise ValueError(
                f"{self.load_strategy.value} does not allow delete_insert configuration"
            )
        if (
            self.load_strategy is LoadStrategy.DELETE_INSERT
            and self.delete_insert is None
        ):
            raise ValueError("DELETE_INSERT requires delete_insert configuration")
        return self
