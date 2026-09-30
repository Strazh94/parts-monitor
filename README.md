# Competitor Parts Price Monitoring

Daily monitoring system for competitors' spare parts prices, stock levels and
sales dynamics. Collects data from websites, stores change history, calculates
estimated sales, demand ratings and purchase recommendations.

The interface is a web application in the browser — no command line required.

## Quick start (one command)

```bash
git clone https://github.com/Strazh94/parts-monitor.git && cd parts-monitor && docker compose up -d --build
```

Done. Open in your browser: **http://localhost:8000**

Only Docker with Docker Compose enabled is required.
The database (PostgreSQL) and the daily schedule are started automatically.

### First-time setup (3 steps)

1. **Competitors** → enter the site name, URL, engine (`http` for a regular site,
   `playwright` for a JavaScript site) → "Add".
2. **Settings** → at the bottom there is an example selector configuration (`parser_config`) —
   copy the block you need to match the site structure.
3. Press **▶ Run** on the "Competitors" page.

The daily collection runs automatically at 21:00 MSK
(the time can be changed on the "Settings" page).

## How to use

| Page | What it gives |
|---|---|
| Dashboard | Daily metrics, TOP-20 by demand and TOP-20 for purchasing |
| Products | Search by SKU/OEM/name, filters, cards with history charts |
| Competitors | Adding/disabling sites, manual runs, statuses |
| Analytics | Rating distribution A/B/C/D/NEW, demand index |
| Top sales | Ranking by estimated sales over 7/14/30/90 days |
| Purchase recommendations | Items with high turnover |
| Price history | Min/max/average/median, prices of each competitor |
| Report | Daily report after parsing |
| Excel export | 4 sheets: current data, history, recommendations, prices |

## Important note about the data

A change in a competitor's stock level is an **estimated sale** based on
a change in the public stock level, not a confirmed sale. Purchasing decisions
are best made using statistics over at least several weeks and data from
several competitors.

## Development

```bash
pip install -r requirements.txt
# a running PostgreSQL is required, the connection string is in .env (see .env.example)
alembic upgrade head
uvicorn app.main:app --reload          # web interface
python -m worker.scheduler             # daily run at 21:00 MSK
python tests/e2e_test.py               # end-to-end tests (32 checks)
```

## Structure

- `app/` — web interface (FastAPI + Jinja2), models, routers
- `app/services/parser/` — parsing engines (HTTP, Playwright), normalization
- `app/services/analytics/` — demand metrics and index calculation
- `app/services/matching.py` — merging products by OEM/SKU
- `worker/scheduler.py` — daily schedule
- `alembic/` — database schema migrations
- `tests/` — end-to-end tests with a local fixture site
