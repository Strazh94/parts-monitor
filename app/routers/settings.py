"""Settings: schedule, demand index weights (spec §3, 12)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.templating import templates
from app.models import AppSetting

router = APIRouter()

# Default values for the configurable index (spec §12)
DEFAULT_WEIGHTS = {
    "w_sales_volume": 30,     # estimated sales volume
    "w_frequency": 20,        # sales frequency
    "w_competitors": 20,      # number of competitors
    "w_stock_dynamics": 15,   # stock dynamics
    "w_price_change": 10,     # price change
    "w_days_observed": 5,     # number of days observed
}


def get_weights(db: Session) -> dict:
    row = db.get(AppSetting, "demand_index_weights")
    return row.value if row else DEFAULT_WEIGHTS


@router.get("", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    weights = get_weights(db)
    saved = db.get(AppSetting, "parse_schedule")
    schedule = saved.value if saved else {"hour": 21, "minute": 0}
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"weights": weights, "schedule": schedule},
    )


@router.post("/weights")
def save_weights(
    hour: int = Form(...),
    minute: int = Form(...),
    w_sales_volume: int = Form(...),
    w_frequency: int = Form(...),
    w_competitors: int = Form(...),
    w_stock_dynamics: int = Form(...),
    w_price_change: int = Form(...),
    w_days_observed: int = Form(...),
    db: Session = Depends(get_db),
):
    """Save index weights: "The formula must be configurable" (spec §12)."""
    weights = {
        "w_sales_volume": w_sales_volume,
        "w_frequency": w_frequency,
        "w_competitors": w_competitors,
        "w_stock_dynamics": w_stock_dynamics,
        "w_price_change": w_price_change,
        "w_days_observed": w_days_observed,
    }
    w_row = db.get(AppSetting, "demand_index_weights")
    if w_row is None:
        w_row = AppSetting(key="demand_index_weights")
        db.add(w_row)
    w_row.value = weights

    s_row = db.get(AppSetting, "parse_schedule")
    if s_row is None:
        s_row = AppSetting(key="parse_schedule")
        db.add(s_row)
    s_row.value = {"hour": hour, "minute": minute}
    db.commit()
    return RedirectResponse("/settings", status_code=303)
