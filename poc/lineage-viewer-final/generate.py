"""Generate the closing standalone viewer from the latest state event only."""

import json
import math
from pathlib import Path
import sys


def read_latest(path):
    latest = None
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                if line.strip():
                    latest = line
    except FileNotFoundError:
        raise ValueError(f"State file is missing: {path}") from None
    if latest is None:
        raise ValueError(f"State file is empty: {path}")
    try:
        event = json.loads(latest)
    except json.JSONDecodeError as error:
        raise ValueError(f"Latest state event is invalid JSON: {error}") from None
    validate_event(event)
    return event


def validate_event(event):
    if not isinstance(event, dict):
        raise ValueError("Latest state event must be a JSON object")
    for key in ("schema_version", "run_id", "status", "started_at", "finished_at", "duration"):
        if key not in event:
            raise ValueError(f"Latest state event is missing {key}")
    if not isinstance(event["schema_version"], int) or any(not isinstance(event[key], str) for key in ("run_id", "status")):
        raise ValueError("Run requires an integer schema_version and string run_id/status")
    validate_runtime(event, ("duration",))
    for key in ("seeders", "models", "lineage", "quality"):
        records = event.get(key)
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise ValueError(f"{key} must be an array of objects")
    for record in event["seeders"]:
        validate_runtime(record, ("duration", "execution_duration"))
        if not isinstance(record.get("name"), str):
            raise ValueError("Seeder name must be a string")
    for record in event["models"]:
        validate_model_id(record)
        validate_runtime(record, ("duration", "materialization_duration"))
    for job in event["lineage"]:
        if job.get("job_type") not in ("SEEDER", "MODEL") or not isinstance(job.get("job_name"), str):
            raise ValueError("Lineage jobs require job_type SEEDER/MODEL and a string job_name")
        for key in ("inputs", "outputs"):
            if not isinstance(job.get(key), list):
                raise ValueError(f"Lineage {key} must be an array")
            for record in job[key]:
                validate_model_id(record)
    for result in event["quality"]:
        validate_model_id(result.get("model"))
        checks = result.get("checks")
        if not isinstance(checks, list) or any(not isinstance(check, dict) for check in checks):
            raise ValueError("Quality checks must be an array of objects")
        for check in checks:
            validate_runtime(check, ("duration",))
            if check.get("parameters") is not None and not isinstance(check["parameters"], dict):
                raise ValueError("Quality parameters must be an object or null")


def validate_runtime(record, duration_fields):
    for key in duration_fields:
        value = record.get(key)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError(f"{key} must be a finite number or null")
    for key in ("started_at", "finished_at"):
        if record.get(key) is not None and not isinstance(record[key], str):
            raise ValueError(f"{key} must be a string or null")


def validate_model_id(record):
    if not isinstance(record, dict) or any(not isinstance(record.get(key), str) for key in ("schema", "name")):
        raise ValueError("Model references require string schema and name fields")


def main():
    folder = Path(__file__).resolve().parent
    try:
        event = read_latest(Path.cwd() / ".rollout/state.jsonl")
        payload = json.dumps(event, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        template = (folder / "template.html").read_text(encoding="utf-8")
        if template.count("__DATA__") != 1:
            raise ValueError("Template must contain exactly one __DATA__ placeholder")
        output = folder / "viewer.html"
        output.write_text(template.replace("__DATA__", payload), encoding="utf-8")
    except (ValueError, OSError) as error:
        sys.exit(f"Cannot generate final lineage viewer: {error}")
    print(f"Wrote {output} for run {event['run_id']}")


if __name__ == "__main__":
    main()
