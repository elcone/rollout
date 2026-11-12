import json
import math
from datetime import datetime
from pathlib import Path

SUMMARY_FIELDS = (
    "schema_version",
    "run_id",
    "started_at",
    "finished_at",
    "duration",
    "status",
)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO 8601 string")
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return timestamp


def _invalid_constant(value: str):
    raise ValueError(f"invalid JSON numeric constant: {value}")


def _validate_run(document: object) -> None:
    if not isinstance(document, dict):
        raise ValueError("run must be an object")
    version = document.get("schema_version")
    if type(version) is not int or version != 1:
        raise ValueError(f"unsupported schema_version: {version!r}")
    if not isinstance(document.get("run_id"), str) or not document["run_id"]:
        raise ValueError("run_id must be a nonempty string")
    start = _timestamp(document.get("started_at"))
    finish = _timestamp(document.get("finished_at"))
    if finish < start:
        raise ValueError("finished_at precedes started_at")
    duration = document.get("duration")
    if (
        type(duration) not in (int, float)
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise ValueError("duration must be a finite nonnegative number")
    if document.get("status") not in ("SUCCESS", "FAILED"):
        raise ValueError("run status must be SUCCESS or FAILED")
    for field in ("seeders", "models", "lineage", "quality"):
        if not isinstance(document.get(field), list) or any(
            not isinstance(item, dict) for item in document[field]
        ):
            raise ValueError(f"{field} must be an array of objects")


def read_runs(project_dir: Path) -> list[dict]:
    """Return complete runs newest first; reject corruption anywhere in the file."""
    path = project_dir / ".rollout" / "state.jsonl"
    try:
        history = path.open(encoding="utf-8")
    except FileNotFoundError:
        return []
    runs = []
    identifiers = set()
    with history:
        for line_number, line in enumerate(history, 1):
            try:
                document = json.loads(line, parse_constant=_invalid_constant)
                _validate_run(document)
                if document["run_id"] in identifiers:
                    raise ValueError(f"duplicate run_id: {document['run_id']!r}")
            except ValueError as exc:
                raise ValueError(
                    f"Invalid run history at line {line_number}: {exc}"
                ) from exc
            identifiers.add(document["run_id"])
            runs.append(document)
    return sorted(runs, key=lambda run: _timestamp(run["started_at"]), reverse=True)
