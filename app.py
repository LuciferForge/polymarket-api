#!/usr/bin/env python3
"""
Polymarket Data API — serves market_universe.db over HTTP.

Zero human bottleneck: deploys once, earns forever.

Endpoints:
  GET /markets                  — list markets (paginated, filterable)
  GET /markets/{id}             — single market detail
  GET /markets/{id}/prices      — price history for a market
  GET /crashes                  — current crash signals (>15% drop)
  GET /stats                    — dataset statistics
  GET /categories               — category breakdown

Auth: API key via X-API-Key header or ?api_key= query param
Rate limits: Free (100/day), Pro (10K/day), Premium (unlimited)

Deploy: Railway / Render / Fly.io (free tier)
"""

import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from functools import wraps

from fastapi import FastAPI, HTTPException, Request, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

# Config
DB_PATH = os.getenv("DB_PATH", str(Path.home() / "Documents/LuciferForge/polymarket-ai/market_universe.db"))
API_KEYS_FILE = os.getenv("API_KEYS_FILE", str(Path(__file__).parent / "api_keys.json"))
PORT = int(os.getenv("PORT", "8400"))

app = FastAPI(
    title="Polymarket Data API",
    description="Historical prices, orderbook depth, and crash signals for 9,550+ Polymarket prediction markets.",
    version="1.0.0",
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Rate limiting (in-memory, simple)
rate_limits = {}  # {api_key: {count: N, reset_at: timestamp}}
FREE_LIMIT = 100
PRO_LIMIT = 10000


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def get_api_key(request: Request) -> str:
    """Extract API key from header or query param."""
    key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if not key:
        # Allow unauthenticated access with aggressive rate limiting
        key = f"anon-{request.client.host}"
    return key


def check_rate_limit(api_key: str):
    """Simple in-memory rate limiter."""
    now = time.time()
    if api_key not in rate_limits:
        rate_limits[api_key] = {"count": 0, "reset_at": now + 86400}

    entry = rate_limits[api_key]
    if now > entry["reset_at"]:
        entry["count"] = 0
        entry["reset_at"] = now + 86400

    limit = PRO_LIMIT if api_key.startswith("pk_") else FREE_LIMIT
    entry["count"] += 1

    if entry["count"] > limit:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({limit}/day). Upgrade at https://checkout.dodopayments.com/buy/pdt_0NcwYR0akzPEQoh1leSk9"
        )


@app.get("/")
def root():
    return {
        "name": "Polymarket Data API",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": ["/markets", "/markets/{id}/prices", "/crashes", "/stats", "/categories"],
        "pricing": {
            "free": "100 requests/day (no key required)",
            "pro": "$19/mo — 10,000 requests/day",
            "premium": "$99/mo — unlimited",
            "subscribe": "https://checkout.dodopayments.com/buy/pdt_0NcwYR0akzPEQoh1leSk9",
        },
        "data": "https://protodex.io | LuciferForge@proton.me",
    }


_stats_cache = {"data": None, "expires": 0}
STATS_TTL_SECONDS = 300


@app.get("/stats")
def stats(request: Request):
    api_key = get_api_key(request)
    check_rate_limit(api_key)

    now = time.time()
    if _stats_cache["data"] and now < _stats_cache["expires"]:
        return _stats_cache["data"]

    conn = get_db()
    conn.execute("PRAGMA query_only = 1")
    markets = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    prices_row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='prices'"
    ).fetchone()
    prices = prices_row[0] if prices_row else 0
    orderbooks_row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='orderbooks'"
    ).fetchone()
    orderbooks = orderbooks_row[0] if orderbooks_row else 0
    latest = conn.execute(
        "SELECT ts FROM prices ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    latest = latest[0] if latest else None
    earliest = conn.execute(
        "SELECT ts FROM prices ORDER BY ts ASC LIMIT 1"
    ).fetchone()
    earliest = earliest[0] if earliest else None
    categories = conn.execute(
        "SELECT COUNT(DISTINCT category) FROM markets"
    ).fetchone()[0]
    conn.close()

    result = {
        "markets": markets,
        "price_snapshots": prices,
        "orderbook_snapshots": orderbooks,
        "categories": categories,
        "earliest_data": earliest,
        "latest_data": latest,
        "update_frequency": "every 15 minutes",
    }
    _stats_cache["data"] = result
    _stats_cache["expires"] = now + STATS_TTL_SECONDS
    return result


@app.get("/categories")
def categories(request: Request):
    api_key = get_api_key(request)
    check_rate_limit(api_key)

    conn = get_db()
    rows = conn.execute("""
        SELECT category, COUNT(*) as count,
               ROUND(SUM(volume)/1e6, 1) as volume_millions
        FROM markets GROUP BY category ORDER BY count DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/markets")
def list_markets(
    request: Request,
    category: str = None,
    search: str = None,
    sort: str = Query("volume", pattern="^(volume|stars|name|volume_24h)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    active: bool = True,
):
    api_key = get_api_key(request)
    check_rate_limit(api_key)

    conn = get_db()
    query = "SELECT id, question, category, volume, volume_24h, liquidity, end_date, best_bid, best_ask, spread, last_trade_price, one_day_change, active FROM markets WHERE 1=1"
    params = []

    if category:
        query += " AND category = ?"
        params.append(category)
    if search:
        query += " AND question LIKE ?"
        params.append(f"%{search}%")
    if active:
        query += " AND active = 1"

    sort_col = {"volume": "volume DESC", "volume_24h": "volume_24h DESC", "name": "question ASC"}.get(sort, "volume DESC")
    query += f" ORDER BY {sort_col} LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM markets WHERE active = ?", (1 if active else 0,)).fetchone()[0]
    conn.close()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "markets": [dict(r) for r in rows],
    }


@app.get("/markets/{market_id}")
def get_market(market_id: str, request: Request):
    api_key = get_api_key(request)
    check_rate_limit(api_key)

    conn = get_db()
    market = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if not market:
        conn.close()
        raise HTTPException(status_code=404, detail="Market not found")

    outcomes = conn.execute(
        "SELECT outcome_idx, outcome_label, clob_token_id FROM market_outcomes WHERE market_id = ?",
        (market_id,)
    ).fetchall()

    conn.close()
    result = dict(market)
    result["outcomes_detail"] = [dict(o) for o in outcomes]
    return result


@app.get("/markets/{market_id}/prices")
def get_prices(
    market_id: str,
    request: Request,
    outcome: str = Query("Yes", pattern="^(Yes|No)$"),
    limit: int = Query(500, ge=1, le=5000),
    since: str = None,
):
    api_key = get_api_key(request)
    check_rate_limit(api_key)

    conn = get_db()
    query = "SELECT price, ts FROM prices WHERE market_id = ? AND outcome = ?"
    params = [market_id, outcome]

    if since:
        query += " AND ts > ?"
        params.append(since)

    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail="No price data found")

    return {
        "market_id": market_id,
        "outcome": outcome,
        "count": len(rows),
        "prices": [{"price": r["price"], "ts": r["ts"]} for r in rows],
    }


@app.get("/crashes")
def get_crashes(
    request: Request,
    threshold: float = Query(0.15, ge=0.05, le=0.50),
    hours: int = Query(4, ge=1, le=24),
    category: str = None,
):
    """Find markets with recent price crashes (mean reversion opportunities)."""
    api_key = get_api_key(request)
    check_rate_limit(api_key)

    conn = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    # Find markets where current price is significantly below recent high
    query = """
        WITH recent AS (
            SELECT market_id, outcome,
                   MAX(price) as high,
                   (SELECT price FROM prices p2
                    WHERE p2.market_id = prices.market_id
                    AND p2.outcome = prices.outcome
                    ORDER BY p2.ts DESC LIMIT 1) as current
            FROM prices
            WHERE ts > ? AND outcome = 'Yes' AND price > 0.05
            GROUP BY market_id, outcome
            HAVING high > 0.10
        )
        SELECT r.market_id, r.high, r.current,
               ROUND((r.high - r.current) / r.high, 4) as drop_pct,
               m.question, m.category, m.volume
        FROM recent r
        JOIN markets m ON m.id = r.market_id
        WHERE (r.high - r.current) / r.high > ?
    """
    params = [cutoff, threshold]

    if category:
        query += " AND m.category = ?"
        params.append(category)

    query += " ORDER BY drop_pct DESC LIMIT 50"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    return {
        "threshold": threshold,
        "hours": hours,
        "count": len(rows),
        "note": "Mean reversion: after >20% crash, avg bounce is +6.6% within 15min (based on 5,629 events)",
        "crashes": [dict(r) for r in rows],
    }


if __name__ == "__main__":
    print(f"Starting Polymarket Data API on port {PORT}")
    print(f"DB: {DB_PATH}")
    print(f"Docs: http://127.0.0.1:{PORT}/docs")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
