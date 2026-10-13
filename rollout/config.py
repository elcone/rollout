from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
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


class ExpectationOperator(Enum):
    GT = "gt"
    GTE = "gte"
    EQ = "eq"
    NEQ = "neq"
    LT = "lt"
    LTE = "lte"


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operator: ExpectationOperator
    value: StrictInt | StrictFloat


class CustomSqlCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["custom_sql"]
    name: Annotated[str, Field(strict=True, min_length=1)]
    sql: Annotated[str, Field(strict=True, min_length=1)]
    expectations: Annotated[tuple[Expectation, ...], Field(min_length=1)]
    severity: CheckSeverity

    @field_validator("name", "sql")
    @classmethod
    def require_nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


QualityCheck = Annotated[
    RowCountCheck | NullnessCheck | UniquenessCheck | CustomSqlCheck,
    Field(discriminator="type"),
]


class SeederOutputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quality: tuple[QualityCheck, ...] = ()


class SeederConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outputs: Annotated[
        Mapping[Annotated[str, Field(min_length=1)], SeederOutputConfig],
        Field(min_length=1),
    ]

    @field_validator("outputs")
    @classmethod
    def freeze_outputs(
        cls, outputs: Mapping[str, SeederOutputConfig]
    ) -> Mapping[str, SeederOutputConfig]:
        if any(not name.strip() for name in outputs):
            raise ValueError("output names must not be empty")
        return MappingProxyType(dict(outputs))


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
