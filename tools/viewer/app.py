from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates

from tools.viewer.project import load_project

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
