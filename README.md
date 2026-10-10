# Rollout

Rollout discovers SQL models from the filesystem, validates their dependencies, and generates a DAG in Markdown with a Mermaid diagram.

## Quick start

Requires Python 3.12 or newer and uv.

```bash
uv sync
uv run rollout dag
uv run rollout dag --output dag.md
uv run pytest
```

The default model directory is `models/`.
To use another directory:

```bash
uv run rollout dag --models-dir path/to/models --output dag.md
```

Without `--output`, Markdown is written to stdout.
Progress messages and errors are written to stderr.

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

models = discover_models("models")
models = parse_models(models)
dag = build_dag(models)
markdown = render_markdown(dag)
```

These functions return data without writing output files.
Errors propagate as exceptions.

## CLI

`rollout dag` validates SQL dependencies and generates a Markdown DAG.

| Option | Description | Default |
| --- | --- | --- |
| `--models-dir PATH` | Directory containing SQL models. | `models/` |
| `--output PATH`, `-o PATH` | Save Markdown to a file. | stdout |
| `--verbose`, `-v` (before `dag`) | Show per-model debug messages. | Disabled |
| `--help` | Show command usage and options. | — |

```bash
uv run rollout --help
uv run rollout dag --help
```
