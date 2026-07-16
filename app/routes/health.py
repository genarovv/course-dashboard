"""GET /health. I1 (#2): жив ли процесс; счётчики из БД — I2 (#13)."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}
