"""РЈРїСЂР°РІР»РµРЅРёРµ РєРѕРЅРєСѓСЂРµРЅС‚Р°РјРё (РўР— Рї.4, 23): РґРѕР±Р°РІР»РµРЅРёРµ, РѕС‚РєР»СЋС‡РµРЅРёРµ, Р·Р°РїСѓСЃРє."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.templating import templates
from app.models import Competitor, EngineType
from app.services.runner import run_competitor

router = APIRouter()


@router.get("", response_class=HTMLResponse)
def list_competitors(request: Request, db: Session = Depends(get_db)):
    competitors = db.scalars(
        select(Competitor).order_by(Competitor.created_at.desc())
    ).all()
    return templates.TemplateResponse(
        request, "competitors.html", {"competitors": competitors}
    )


@router.post("/add")
def add_competitor(
    name: str = Form(...),
    url: str = Form(...),
    engine: str = Form("http"),
    db: Session = Depends(get_db),
):
    competitor = Competitor(
        name=name.strip(),
        url=url.strip(),
        engine=EngineType(engine),
        parser_config={},
        categories=[],
    )
    db.add(competitor)
    db.commit()
    return RedirectResponse("/competitors", status_code=303)


@router.post("/{competitor_id}/toggle")
def toggle_competitor(competitor_id: int, db: Session = Depends(get_db)):
    competitor = db.get(Competitor, competitor_id)
    if competitor:
        competitor.enabled = not competitor.enabled
        db.commit()
    return RedirectResponse("/competitors", status_code=303)


@router.post("/{competitor_id}/delete")
def delete_competitor(competitor_id: int, db: Session = Depends(get_db)):
    competitor = db.get(Competitor, competitor_id)
    if competitor:
        db.delete(competitor)
        db.commit()
    return RedirectResponse("/competitors", status_code=303)


@router.post("/{competitor_id}/run")
async def run_now(competitor_id: int, db: Session = Depends(get_db)):
    """Р СѓС‡РЅРѕР№ Р·Р°РїСѓСЃРє: РєРЅРѕРїРєР° В«Р—Р°РїСѓСЃС‚РёС‚СЊ СЃР±РѕСЂ РґР°РЅРЅС‹С…В» (РўР— Рї.3)."""
    competitor = db.get(Competitor, competitor_id)
    if competitor:
        await run_competitor(db, competitor, trigger="manual")
    return RedirectResponse("/competitors", status_code=303)
