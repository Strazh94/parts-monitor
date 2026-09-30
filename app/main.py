"""Веб-интерфейс системы (ТЗ п.22)."""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routers import (
    analytics,
    competitors,
    dashboard,
    export,
    products,
    report,
    runs,
    settings as settings_router,
)
from app.templating import BASE_DIR

app = FastAPI(title="Мониторинг запчастей конкурентов")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

app.include_router(dashboard.router)
app.include_router(competitors.router, prefix="/competitors", tags=["competitors"])
app.include_router(products.router, prefix="/products", tags=["products"])
app.include_router(analytics.router, prefix="/analytics", tags=["analytics"])
app.include_router(runs.router, prefix="/runs", tags=["runs"])
app.include_router(report.router, prefix="/report", tags=["report"])
app.include_router(settings_router.router, prefix="/settings", tags=["settings"])
app.include_router(export.router, prefix="/export", tags=["export"])


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
