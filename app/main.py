from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings

app = FastAPI(title="Course Dashboard")

app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
templates = Jinja2Templates(directory=settings.template_dir)


@app.get("/health")
async def health():
    return {"status": "ok"}
