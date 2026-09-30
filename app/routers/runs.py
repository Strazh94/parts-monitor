"""РљРѕРЅС‚СЂРѕР»СЊ СЂР°Р±РѕС‚С‹ РїР°СЂСЃРµСЂР°: СЃС‚Р°С‚СѓСЃС‹ Р·Р°РїСѓСЃРєРѕРІ (РўР— Рї.24)."""
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
    """Р’СЃРµ Р·Р°РїСѓСЃРєРё: РІСЂРµРјСЏ, СЃС‚Р°С‚СѓСЃ, СЃС‚СЂР°РЅРёС†С‹, С‚РѕРІР°СЂС‹, РѕС€РёР±РєРё."""
    runs = db.scalars(
        select(ParseRun)
        .options(joinedload(ParseRun.competitor))
        .order_by(ParseRun.started_at.desc())
        .limit(200)
    ).unique().all()
    return templates.TemplateResponse(request, "runs.html", {"runs": runs})
