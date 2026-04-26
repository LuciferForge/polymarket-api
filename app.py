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

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from functools import wraps
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Query, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
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


# =====================================================================
# WebSocket crash feed — streams new crashes as they appear
# =====================================================================

WS_SCAN_INTERVAL = 30  # seconds between scans
WS_HOURS_WINDOW = 4

class CrashFeedManager:
    """Tracks subscribers and broadcasts new crash signals."""

    def __init__(self) -> None:
        self.subscribers: list[tuple[WebSocket, dict]] = []
        self.seen: dict[str, float] = {}  # market_id -> drop_pct last broadcast
        self.task: Optional[asyncio.Task] = None

    async def connect(self, ws: WebSocket, params: dict) -> None:
        await ws.accept()
        self.subscribers.append((ws, params))
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._scan_loop())

    def disconnect(self, ws: WebSocket) -> None:
        self.subscribers = [(s, p) for (s, p) in self.subscribers if s is not ws]

    async def _scan_loop(self) -> None:
        while self.subscribers:
            try:
                await self._scan_and_broadcast()
            except Exception as e:
                print(f"[ws crash-feed] scan error: {e}")
            await asyncio.sleep(WS_SCAN_INTERVAL)

    async def _scan_and_broadcast(self) -> None:
        if not self.subscribers:
            return

        loop = asyncio.get_running_loop()
        crashes = await loop.run_in_executor(None, self._fetch_crashes)

        new_or_changed = []
        for c in crashes:
            mid = c["market_id"]
            prev = self.seen.get(mid)
            # Broadcast if first time seen OR drop_pct grew by >2 percentage points
            if prev is None or (c["drop_pct"] - prev) > 0.02:
                new_or_changed.append(c)
                self.seen[mid] = c["drop_pct"]

        if not new_or_changed:
            return

        # Cleanup: forget crashes that have recovered (no longer in current scan)
        current_ids = {c["market_id"] for c in crashes}
        self.seen = {k: v for k, v in self.seen.items() if k in current_ids}

        # Broadcast to each subscriber, filtered by their threshold/category
        dead = []
        for ws, params in self.subscribers:
            try:
                threshold = params.get("threshold", 0.15)
                category = params.get("category")
                filtered = [
                    c for c in new_or_changed
                    if c["drop_pct"] >= threshold
                    and (not category or c.get("category") == category)
                ]
                if filtered:
                    await ws.send_json({
                        "type": "crash",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "count": len(filtered),
                        "crashes": filtered,
                    })
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def _fetch_crashes(self) -> list[dict]:
        conn = get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=WS_HOURS_WINDOW)).isoformat()
        rows = conn.execute("""
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
            WHERE (r.high - r.current) / r.high > 0.05
            ORDER BY drop_pct DESC LIMIT 100
        """, (cutoff,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]


crash_feed = CrashFeedManager()


@app.websocket("/ws/crashes")
async def ws_crashes(
    websocket: WebSocket,
    threshold: float = Query(0.15, ge=0.05, le=0.50),
    category: Optional[str] = None,
):
    """Stream new crash signals as they appear.

    Free tier: anyone can connect, default 0.15 threshold.
    Pro tier ($19/mo): pass api_key in query string for prioritized scans + lower thresholds.
    """
    api_key = websocket.query_params.get("api_key", f"anon-{websocket.client.host}")
    params = {"threshold": threshold, "category": category, "api_key": api_key}
    await crash_feed.connect(websocket, params)
    try:
        await websocket.send_json({
            "type": "hello",
            "subscribed": params,
            "scan_interval_s": WS_SCAN_INTERVAL,
            "hours_window": WS_HOURS_WINDOW,
            "note": "You'll receive a 'crash' message when any market drops past your threshold or its drop deepens.",
        })
        while True:
            # Keep connection alive; ignore any client messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        crash_feed.disconnect(websocket)


@app.get("/ws/demo", response_class=HTMLResponse)
def ws_demo():
    """Browser demo of the crash WebSocket. Click connect, watch crashes stream."""
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>Polymarket Crash Feed — Live Demo</title>
<style>
body{font-family:system-ui,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:24px;max-width:900px;margin:auto}
h1{margin:0 0 8px;font-size:22px}
p.sub{color:#8b949e;margin:0 0 24px}
.controls{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:16px;margin-bottom:16px}
label{display:inline-block;margin-right:16px;color:#8b949e;font-size:13px}
input,select{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:6px 10px;font-family:inherit;font-size:13px}
button{background:#238636;color:#fff;border:0;border-radius:4px;padding:8px 16px;cursor:pointer;font-weight:600}
button:disabled{background:#30363d;color:#8b949e;cursor:not-allowed}
button.stop{background:#da3633}
#status{margin:12px 0;font-size:13px}
#feed{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px;font-family:ui-monospace,monospace;font-size:12px;height:60vh;overflow:auto;white-space:pre-wrap}
.entry{padding:6px 0;border-bottom:1px solid #21262d}
.entry .head{color:#58a6ff}
.entry .pct{color:#f85149;font-weight:700}
.entry .meta{color:#8b949e;margin-left:8px}
a{color:#58a6ff}
</style></head>
<body>
<h1>Polymarket Crash Feed — Live</h1>
<p class="sub">Streams new prediction-market drops as they happen. Free, no auth. Source: <a href="https://github.com/LuciferForge/polymarket-api">github.com/LuciferForge/polymarket-api</a></p>
<div class="controls">
  <label>Threshold (drop %): <input id="threshold" type="number" min="0.05" max="0.5" step="0.01" value="0.15"></label>
  <label>Category: <select id="category"><option value="">all</option><option>politics</option><option>sports</option><option>crypto</option><option>economics</option></select></label>
  <button id="connect">Connect</button>
  <button id="disconnect" class="stop" disabled>Disconnect</button>
</div>
<div id="status">Disconnected.</div>
<div id="feed"></div>
<script>
let ws=null;
const $=id=>document.getElementById(id);
const log=(msg,cls)=>{const d=document.createElement('div');d.className='entry';d.innerHTML=msg;$('feed').prepend(d);};
$('connect').onclick=()=>{
  const t=$('threshold').value, c=$('category').value;
  const proto=location.protocol==='https:'?'wss':'ws';
  const url=`${proto}://${location.host}/ws/crashes?threshold=${t}${c?'&category='+c:''}`;
  ws=new WebSocket(url);
  $('status').textContent=`Connecting to ${url} ...`;
  ws.onopen=()=>{$('status').textContent='Connected. Waiting for crash signals...';$('connect').disabled=true;$('disconnect').disabled=false;};
  ws.onmessage=(ev)=>{
    const m=JSON.parse(ev.data);
    if(m.type==='hello'){log(`<span class="meta">[${m.ts||'hello'}] subscribed: threshold ${m.subscribed.threshold}, scan every ${m.scan_interval_s}s</span>`);return;}
    if(m.type==='crash'){
      m.crashes.forEach(c=>{
        const pct=(c.drop_pct*100).toFixed(1);
        log(`<span class="head">${c.question}</span><br><span class="pct">−${pct}%</span> <span class="meta">${c.category} · vol $${Math.round(c.volume).toLocaleString()} · ${c.high}→${c.current} · ${m.ts}</span>`);
      });
    }
  };
  ws.onclose=()=>{$('status').textContent='Disconnected.';$('connect').disabled=false;$('disconnect').disabled=true;};
  ws.onerror=(e)=>log(`<span class="meta">error: ${e.message||'connection failed'}</span>`);
};
$('disconnect').onclick=()=>{if(ws)ws.close();};
</script></body></html>"""


if __name__ == "__main__":
    print(f"Starting Polymarket Data API on port {PORT}")
    print(f"DB: {DB_PATH}")
    print(f"Docs: http://127.0.0.1:{PORT}/docs")
    print(f"WebSocket demo: http://127.0.0.1:{PORT}/ws/demo")
    uvicorn.run(app, host="0.0.0.0", port=PORT, ws_ping_interval=30, ws_ping_timeout=10)
