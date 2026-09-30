"""Ежедневный отчёт (ТЗ п.20)."""
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.report import build_daily_report
from app.templating import templates

router = APIRouter()


@router.get("", response_class=HTMLResponse)
def daily_report(request: Request, db: Session = Depends(get_db)):
    report = build_daily_report(db, date.today())
    return templates.TemplateResponse(request, "report.html", {"report": report})
