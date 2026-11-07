from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates

from rollout.discovery import discover_models
from rollout.models import ModelId
from rollout.parser import parse_models
from tools.cll.lineage import resolve_model_lineage
from tools.viewer.project import load_project, model_column_lineage

ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=ROOT / "templates")
app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "app_css": (ROOT / "static/app.css").read_text(encoding="utf-8"),
            "app_js": (ROOT / "static/app.js").read_text(encoding="utf-8"),
        },
    )


@app.get("/api/project")
def project():
    try:
        return load_project(Path.cwd())
    except (ValueError, OSError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/cll")
def cll(schema: str, model: str, catalog: str = ""):
    try:
        models = parse_models(discover_models(Path.cwd() / "models"))
        identity = ModelId(schema=schema, name=model, catalog=catalog)
        selected = next((item for item in models if item.id == identity), None)
        if selected is None:
            raise HTTPException(
                status_code=404, detail=f"Model '{identity}' does not exist"
            )
        return model_column_lineage(resolve_model_lineage(selected, models))
    except (ValueError, OSError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
