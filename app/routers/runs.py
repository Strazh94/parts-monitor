"""Parser monitoring: run statuses (spec §24)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.templating import templates
from app.models import ParseRun

router = APIRouter()


@router.get("", response_class=HTMLResponse)
def runs_list(request: Request, db: Session = Depends(get_db)):
    """All runs: time, status, pages, products, errors."""
    runs = db.scalars(
        select(ParseRun)
        .options(joinedload(ParseRun.competitor))
        .order_by(ParseRun.started_at.desc())
        .limit(200)
    ).unique().all()
    return templates.TemplateResponse(request, "runs.html", {"runs": runs})
