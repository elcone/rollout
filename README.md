# Rollout

Rollout discovers SQL models from the filesystem, validates their dependencies, generates a DAG in Markdown, and materializes SQL models as tables in DuckDB.

## Quick start

Requires Python 3.12 or newer and uv.

```bash
uv sync
uv run rollout dag
uv run rollout dag --output dag.md
uv run rollout run
uv run pytest
```

The default model directory is `models/`.
To use another directory:

```bash
uv run rollout dag --models-dir path/to/models --output dag.md
```

Without `--output`, Markdown is written to stdout.
Progress messages and errors are written to stderr.

## SQL execution

`rollout run` builds and validates the DAG once, then executes models in its stored order.
The warehouse is created at `.rollout/warehouse.duckdb`, relative to the current working directory.
Use `--models-dir PATH` to select another model directory.

Each model uses FULL materialization: `CREATE OR REPLACE TABLE "schema"."model" AS` followed by its original SQL.
Schemas are prepared once before model execution begins.
Each table replacement runs in its own transaction.
Success commits the transaction; failure rolls it back, preserving any previously materialized table.
Unqualified table references resolve in `public` first, then DuckDB's `main` schema.
External sources must already be accessible in the warehouse or through the model's SQL.

Failed or skipped local dependencies cause a model to be skipped, even if an older table exists.
Independent branches continue executing.
Results remain in memory and the CLI prints SUCCESS (🟢), FAILED (🔴), and SKIPPED (🟡) results with totals.
The command exits with code 1 if a model fails or the DAG cannot be validated.
Execution status is not persisted.
Each run saves generated SQL under `.rollout/models/<UTC timestamp>/<schema>/<model>.sql`.
Files contain the exact FULL materialization SQL sent to DuckDB, including the original model SQL.
Replaying an artifact requires its target schema to exist; Rollout prepares schemas separately.
SQL is saved before execution, so failed models can be inspected too; skipped models have no file.
An error saving generated SQL is logged as a warning and does not prevent execution.
When using a custom warehouse path from Python, artifacts are stored beside the warehouse.
These debug artifacts are retained; no automatic cleanup is performed.

Use `rollout --verbose run` to see each model's file,
dependencies, generated SQL path, transaction outcome, and blocking dependencies when skipped.
The SQL body is not printed in verbose logs.
Execution logs start with the model count and warehouse path.
The console summary uses green for success, red for failure, and yellow for skipped models.
Colors are automatically omitted when output is redirected.

## Model discovery flow

Discovery starts at the selected directory and scans `.sql` files in sorted path order.
Each file becomes a model whose name is its filename without the extension and whose schema comes from its location.

For example:

```text
models/
├── report.sql              → public.report
├── bronze/
│   └── clients.sql         → bronze.clients
└── silver/
    └── active_clients.sql  → silver.active_clients
```

Only files at the root or one schema directory below it are included.
More deeply nested SQL files are logged at ERROR level and skipped; discovery continues for the remaining models.
An explicit `models/public/` directory is rejected; place public models directly under `models/`.

## SQL validation

Empty models, including files containing only comments, are skipped with a warning.
Each non-empty model must contain exactly one read-only query producing a single dataset.
SELECT, CTEs, UNION, INTERSECT, EXCEPT, VALUES, and PIVOT are supported.
Multiple statements and explicit write operations are rejected, including SELECT INTO and data modifications inside CTEs.
Function side effects are not analyzed in this milestone.
Validation errors include the source file path.

## Python usage

The CLI coordinates the same functions you can call directly from Python:

```python
from rollout.discovery import discover_models
from rollout.parser import parse_models
from rollout.dag import build_dag
from rollout.renderer import render_markdown
from rollout.execution import execute_models
from rollout.config import EngineConfig

models = discover_models("models")
models = parse_models(models)
dag = build_dag(models)
markdown = render_markdown(dag)
results = execute_models(dag, EngineConfig())
```

Discovery, parsing, DAG construction, and rendering return data without writing output files.
`execute_models()` writes the DuckDB warehouse and returns in-memory model results.
Model SQL errors become FAILED results; setup and validation errors propagate as exceptions.

`EngineConfig(warehouse_path="path/to/warehouse.duckdb")` selects the database path.
`Model.config` defaults to `ModelConfig(load_strategy="FULL")`; FULL is the only supported strategy.
Both configuration models use Pydantic validation and reject unknown fields.
Internal models, DAGs, and execution results remain dataclasses.

## CLI

`rollout dag` validates SQL dependencies and generates a Markdown DAG.
`rollout run` validates the same DAG and executes its SQL models in DuckDB.

| Option | Description | Default |
| --- | --- | --- |
| `--models-dir PATH` | Directory containing SQL models. | `models/` |
| `--output PATH`, `-o PATH` | Save Markdown to a file. | stdout |
| `--verbose`, `-v` (before the command) | Show per-model debug messages. | Disabled |
| `--help` | Show command usage and options. | — |

```bash
uv run rollout --help
uv run rollout dag --help
uv run rollout run --help
```

## Tests and coverage

Tests are organized by product responsibility: discovery, SQL validation, table
lineage, DAGs, rendering, configuration, execution, FULL loading, and CLI integration.
Shared fixtures in `tests/conftest.py` only write temporary models, build a test DAG,
and provide a temporary warehouse configuration. Execution tests use real DuckDB.
Generic SQL examples follow DuckDB's [PIVOT documentation](https://duckdb.org/docs/current/sql/statements/pivot)
and [set operation documentation](https://duckdb.org/docs/current/sql/query_syntax/setops).

```bash
uv run pytest
uv run pytest --cov=rollout --cov-report=term-missing
uv run pytest --cov=rollout --cov-report=html
```

Coverage includes branches and uses an 85% minimum when coverage is requested.
The HTML report is written to `htmlcov/index.html`.
Missing coverage should guide behavioral review; the percentage is not the goal.
