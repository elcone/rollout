# Rollout

Rollout executes Python Seeders and SQL Models from a project directory in DuckDB, validates Model dependencies, and reports execution and quality results in Markdown.

## Quick start

Requires Python 3.12 or newer and uv.

```bash
uv sync
uv run rollout dag
uv run rollout dag --output dag.md
uv run rollout run
uv run pytest
```

The default project directory is `.`. Each project contains required `models/` and optional `seeders/` directories. To use another project:

```bash
uv run rollout dag --project-dir examples/basic --output dag.md
uv run rollout run --project-dir examples/basic
```

Without `--output`, Markdown is written to stdout. Progress messages and errors are written to stderr.

## Seeders and project structure

```text
project/
├── seeders/
│   ├── source.py
│   └── source.yaml
└── models/
    ├── silver/
    │   └── orders.sql
    └── gold/
        └── summary.sql
```

A Seeder is a Python file with an optional sibling `.yaml` declaring its outputs. Without YAML, the filename defines one output with default configuration: `customers.py` produces `staging.customers`. A present YAML file must contain valid output configuration; empty or invalid YAML fails discovery. `.yml` files are not loaded. A missing `seeders/` directory produces no Seeders; an existing path must be a directory. Seeders are discovered recursively in sorted path order; output names must be unique across the project. Each Seeder defines `run(connection, staging_dir)` and returns a Python dict mapping exactly its declared names to DuckDB relations:

```python
def run(connection, staging_dir):
    return {"orders": connection.sql("SELECT 1 AS order_id")}
```

```yaml
outputs:
  orders:
    quality:
      - type: row_count
        min: 1
        severity: ERROR
```

Rollout owns the `staging` schema; Models cannot be placed in `models/staging/`. Seeders always use FULL table replacement, with a separate transaction per output. Built-in and custom SQL quality checks use the same rules as Models: ERROR failures roll back that output, while WARNING failures commit. Successful outputs remain committed when another output fails. Later Seeders and Models continue after Seeder failures; a Python or output-contract failure produces no output results.

`staging_dir` is a fresh temporary filesystem directory, available through Python execution, lazy relation materialization, and quality checks, then removed even on failure. Seeders can use it for downloaded files or Parquet; it is separate from the warehouse's `staging` schema. Seeders and Models share the same open warehouse connection. Seeder output materialization does not create generated SQL artifacts.

The DAG and Mermaid execution lineage remain Model-only. References to `staging.*` appear as dashed external dependencies. Failed Seeder outputs do not automatically block Models; DuckDB determines whether referenced staging tables are available. Seeder aggregate status records whether Python execution and all outputs succeeded; individual dataset results remain authoritative.

## SQL execution

`rollout run` opens one DuckDB connection, discovers and executes Seeders in sorted path order, then discovers Models, builds and validates their DAG once, and executes Models in its stored order. The warehouse is created at `.rollout/warehouse.duckdb`, relative to the current working directory. Use `--project-dir PATH` to select another project.

By default, each model uses FULL materialization: `CREATE OR REPLACE TABLE "schema"."model" AS` followed by its original SQL. Schemas are prepared once before model execution begins. Each table replacement runs in its own transaction. Success commits the transaction; failure rolls it back, preserving any previously materialized table. Unqualified table references resolve in `public` first, then DuckDB's `main` schema. External sources must already be accessible in the warehouse or through the model's SQL.

Non-FULL load strategies initialize a missing target with the full dataset produced by the model SQL, using `CREATE TABLE "schema"."model" AS` followed by the original SQL. Once the target exists, including an empty table, Rollout applies the configured load strategy. Bootstrap and quality checks share the per-model transaction; an ERROR quality failure rolls back the newly created table.

Failed or skipped local dependencies cause a model to be skipped, even if an older table exists. Independent branches continue executing. Results remain in memory and the CLI prints SUCCESS (🟢), FAILED (🔴), and SKIPPED (🟡) results with totals. The command exits with code 1 if a Seeder, Seeder output, or Model fails, or project setup or validation fails. Execution status is not persisted. Each run saves generated SQL under `.rollout/models/<UTC timestamp>/<schema>/<model>.sql`. Files contain the exact materialization SQL sent to DuckDB, including the original model SQL. Replaying an artifact requires its target schema to exist; Rollout prepares schemas separately. SQL is saved before execution, so failed models can be inspected too; skipped models have no file. An error saving generated SQL is logged as a warning and does not prevent execution. When using a custom warehouse path from Python, artifacts are stored beside the warehouse. These debug artifacts are retained; no automatic cleanup is performed.

Use `rollout --verbose run` to see each model's file, dependencies, generated SQL path, transaction outcome, and blocking dependencies when skipped. The SQL body is not printed in verbose logs. Execution logs start with the model count and warehouse path. The console summary uses green for success, red for failure, and yellow for skipped models. Colors are automatically omitted when output is redirected.

After execution, Rollout saves a Markdown report in `.rollout/runs/` using a UTC filename such as `20261011T000332.123456Z.md`. Reports include the UTC run start and finish times, total elapsed execution duration, summary, Seeder results and Python/total durations, Seeder outputs, Model results, dataset quality details, full failure diagnostics, and execution lineage. Mermaid nodes show execution status and duration; skipped models have no duration.

Use `rollout run --output run.md` (or `-o run.md`) to save to an explicit path instead. Automatic save failures emit a warning and preserve the execution exit status. A failure writing an explicitly requested output returns a CLI error; model results remain unchanged.


## APPEND loading

Configure append-only loading in the model's sibling YAML file:

```yaml
load_strategy: APPEND
```

APPEND requires no additional configuration and rejects `delete_insert` configuration. For an existing target, it executes `INSERT INTO "schema"."model"` followed by the original model SQL. Rollout appends whatever dataset the model SQL produces; the model author decides which rows to produce. Repeated execution appends the rows again without deduplication or incremental state.

Loading and quality checks share the existing per-model transaction. INSERT failures and ERROR quality failures roll back appended rows. WARNING quality failures commit and the model succeeds. Generated SQL artifacts contain the actual CREATE or INSERT statement executed.

## DELETE_INSERT loading

Configure incremental replacement in the model's sibling YAML file:

```yaml
load_strategy: DELETE_INSERT
delete_insert:
  where: |
    EXISTS (
      SELECT 1
      FROM staging.orders AS source
      WHERE target.order_date = source.transaction_date
    )
```

`delete_insert` and its nonempty `where` SQL are required and validated by Pydantic. The stable alias `target` refers to the existing target in the DELETE predicate. Predicates may include subqueries and reference other accessible tables. Rollout parses DELETE_INSERT predicates as DuckDB SQL expressions and rejects malformed or multi-statement input. Model configuration is still treated as trusted developer input. The predicate selects only existing rows to remove; the original model SQL independently determines every row inserted, without additional filtering.

For an existing target, Rollout executes:

```sql
DELETE FROM "schema"."model" AS target
WHERE <delete_insert.where>;

INSERT INTO "schema"."model"
<model.sql>
```

DELETE and INSERT run in the same per-model transaction as quality checks. A SQL failure or failed ERROR check rolls everything back, restoring the previous target; a failed WARNING check commits. Generated SQL artifacts show the actual statements executed.

## Model discovery flow

Discovery starts at the selected directory and scans `.sql` files in sorted path order. Each file becomes a model whose name is its filename without the extension and whose schema comes from its location.

For example:

```text
models/
├── report.sql              → public.report
├── bronze/
│   └── clients.sql         → bronze.clients
└── silver/
    └── active_clients.sql  → silver.active_clients
```

Only files at the root or one schema directory below it are included. More deeply nested SQL files fail discovery with `ValueError`. Explicit `models/public/` and `models/staging/` directories are rejected even when empty; place public models directly under `models/`.

## SQL validation

Empty models, including files containing only comments, are skipped with a warning. Each non-empty model must contain exactly one read-only query producing a single dataset. SELECT, CTEs, UNION, INTERSECT, EXCEPT, VALUES, and PIVOT are supported. Multiple statements and explicit write operations are rejected, including SELECT INTO and data modifications inside CTEs. Function side effects are not analyzed in this milestone. Validation errors include the source file path.

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

Discovery, parsing, DAG construction, and rendering return data without writing output files. `execute_models()` writes the DuckDB warehouse and returns in-memory model results. Model SQL errors become FAILED results; setup and validation errors propagate as exceptions.

`EngineConfig(warehouse_path="path/to/warehouse.duckdb")` selects the database path. `Model.config` defaults to `ModelConfig(load_strategy="FULL")`; FULL, APPEND, and DELETE_INSERT are supported. Both configuration models use Pydantic validation and reject unknown fields. Internal models, DAGs, and execution results remain dataclasses.

## CLI

`rollout dag` validates SQL dependencies and generates a Markdown DAG. `rollout run` executes Seeders before validating and executing the Model DAG in DuckDB.

| Option | Description | Default |
| --- | --- | --- |
| `--project-dir PATH` | Project containing `models/` and optional `seeders/`. | `.` |
| `--output PATH`, `-o PATH` | Save Markdown to a file. | stdout |
| `--verbose`, `-v` (before the command) | Show Seeder and Model debug messages. | Disabled |
| `--help` | Show command usage and options. | — |

```bash
uv run rollout --help
uv run rollout dag --help
uv run rollout run --help
```

## Tests and coverage

Tests are organized by product responsibility: discovery, SQL validation, table lineage, DAGs, rendering, configuration, execution, FULL loading, and CLI integration. Shared fixtures in `tests/conftest.py` only write temporary models, build a test DAG, and provide a temporary warehouse configuration. Execution tests use real DuckDB. Generic SQL examples follow DuckDB's [PIVOT documentation](https://duckdb.org/docs/current/sql/statements/pivot) and [set operation documentation](https://duckdb.org/docs/current/sql/query_syntax/setops).

```bash
uv run pytest
uv run pytest --cov=rollout --cov-report=term-missing
uv run pytest --cov=rollout --cov-report=html
```

Coverage includes branches and uses an 85% minimum when coverage is requested. The HTML report is written to `htmlcov/index.html`. Missing coverage should guide behavioral review; the percentage is not the goal.

## Quality checks

Discovery loads an optional sibling `.yaml` file with the same stem as each SQL model, such as `models/bronze/orders.yaml` for `models/bronze/orders.sql`. Missing or empty YAML uses default configuration; `.yml` files are not loaded. Invalid YAML or configuration fails discovery with the YAML file path. You can also pass quality checks through `ModelConfig(quality=[...])` when constructing a model in Python. Configuration and nested checks are immutable and reject unknown fields. The configuration shape is:

```yaml
load_strategy: FULL
quality:
  - type: row_count
    min: 1
    severity: ERROR
  - type: nullness
    column: customer_id
    max: 0
    severity: WARNING
  - type: uniqueness
    columns: [order_id, line_id]
    max: 0
    severity: ERROR
```

Every check requires a severity. Built-in checks require a nonnegative integer expectation. Uniqueness counts duplicate key groups, including groups containing NULLs, rather than excess rows. Column names are quoted as SQL identifiers.

Checks run after materialization inside its transaction. PASS continues; failed WARNING checks commit and keep the model successful; failed ERROR checks roll back and skip local dependents. Check execution or invalid scalar results always fail and roll back the model, even for a WARNING check. Completed check results remain in memory and appear in the execution report's quality summary and details. Mermaid cards show only quality counts and retain execution-status colors.

Custom checks use a required name, authored SQL, and one or more numeric expectations:

```yaml
quality:
  - type: custom_sql
    name: completion_rate
    sql: |
      SELECT count(*) FILTER (WHERE completed) * 1.0 / count(*)
      FROM silver.orders
    expectations:
      - operator: gte
        value: 0.95
      - operator: lte
        value: 1
    severity: WARNING
```

Custom SQL runs exactly as authored, without Jinja rendering or reformatting. It is validated during quality execution as one read-only query using the same parser as model SQL; data modification, DDL, SELECT INTO, and multiple statements are rejected. The query may access warehouse tables and must return exactly one row and one column containing a numeric value; strings, dates, booleans, and NULL are unsupported.

Expectations support `gt`, `gte`, `eq`, `neq`, `lt`, and `lte`. All expectations must pass (AND semantics). Reports display the check name and SQL-style comparisons, such as `>= 0.95 AND <= 1`. Invalid SQL, execution errors, and invalid scalar results roll back regardless of severity.
