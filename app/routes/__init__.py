"""FastAPI HTML-роуты (Jinja2). Общий экземпляр шаблонов — здесь."""

from fastapi.templating import Jinja2Templates

from app.config import settings

templates = Jinja2Templates(directory=settings.template_dir)
