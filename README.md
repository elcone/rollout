# Rollout

Rollout discovers SQL models from the filesystem, validates their dependencies, and generates a DAG in Markdown with a Mermaid diagram.
The current scope is static dependency analysis; SQL is not executed.

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

## Python API

The CLI uses the same public API available to Python applications:

```python
from rollout import Rollout

project = Rollout(models_dir="models")
result = project.dag()

print(result.execution_id)
print(result.markdown)
```

`DAGResult` contains `execution_id`, `graph`, `order`, and `markdown`.
The API returns the document without writing files.
Errors propagate as exceptions.
Each call generates a new execution ID; callers can supply an existing ID with `project.dag(execution_id="pipeline-123")`.

## CLI

`rollout dag` validates SQL dependencies and generates a Markdown DAG.

| Option | Description | Default |
| --- | --- | --- |
| `--models-dir PATH` | Directory containing SQL models. | `models/` |
| `--output PATH`, `-o PATH` | Save Markdown to a file. | stdout |
| `--verbose`, `-v` | Show per-model debug messages. | Disabled |
| `--help` | Show command usage and options. | — |

```bash
uv run rollout --help
uv run rollout dag --help
```

## Logging and execution context

CLI progress messages go to stderr, keeping Markdown on stdout clean.
Use `uv run rollout dag --verbose` for per-model discovery and parsing details.

```text
2026-10-08T18:42:31.123Z INFO [2026-10-08_Thu_18-42-31Z_a7c9e2] rollout.discovery (dag): Found 21 models
```

Each CLI log message includes its own UTC timestamp with millisecond precision, so timestamps can be compared across execution steps.
The timestamp in the execution ID identifies the operation's start, not the time of each message.

Generated IDs use `YYYY-MM-DD_Ddd_HH-mm-ssZ_hex`: a UTC timestamp, an English weekday abbreviation, and six random hexadecimal characters.
The random suffix reduces collision risk; IDs do not guarantee global uniqueness.
Calls within the same second have no guaranteed ordering.

All package modules use the `rollout` logger hierarchy.
Records include `execution_id` and `operation` fields, populated from a `ContextVar` context shared during each operation and restored on success or failure.
Outside an operation these fields are `-`.
Separate threads and async task contexts can maintain independent IDs; new worker threads need explicit context propagation if they are part of an existing operation.

Importing Rollout installs only a `NullHandler`; it does not configure console output or change the root logger.
Applications choose their own logging setup:

```python
import logging
import time
from rollout import Rollout

handler = logging.StreamHandler()
formatter = logging.Formatter(
    "%(asctime)s.%(msecs)03dZ %(levelname)s [%(execution_id)s] "
    "%(name)s (%(operation)s): %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
formatter.converter = time.gmtime
handler.setFormatter(formatter)
logger = logging.getLogger("rollout")
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

result = Rollout("models").dag()
```
