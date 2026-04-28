# Polymarket Data API

Free, fast HTTP API over a 10M-row Polymarket prediction market dataset. Live at **[api.protodex.io](https://api.protodex.io)**.

Built for backtesting prediction-market strategies, building dashboards, and feeding data to LLM agents (MCP-compatible).

## Endpoints

| Endpoint | Returns |
|----------|---------|
| `GET /stats` | Database size, row counts, last update |
| `GET /markets?category=&limit=&offset=` | Market metadata (slug, question, category, end date, volume) |
| `GET /markets/{slug}` | Single market details + outcomes |
| `GET /prices/{slug}?since=&until=&interval=` | OHLCV-style price snapshots for a market |
| `GET /orderbook/{slug}` | Latest orderbook depth (bids/asks per outcome) |
| `GET /crashes?threshold=0.20&hours=24` | Markets that dropped ≥X% in last Y hours |
| `WS /ws/crashes?threshold=0.15&category=politics` | **Live crash stream** — push notification when any market drops past your threshold. 30s scan cadence. |
| `GET /ws/demo` | Browser demo of the live crash stream (zero-code, just open in a browser) |
| `GET /docs` | Auto-generated OpenAPI / Swagger docs |

Full OpenAPI spec at `/docs`. Try the live endpoints at `api.protodex.io`.

### WebSocket crash stream (the headline feature)

```python
import asyncio, websockets, json

async def main():
    uri = "wss://api.protodex.io/ws/crashes?threshold=0.15"
    async with websockets.connect(uri) as ws:
        while True:
            msg = json.loads(await ws.recv())
            if msg["type"] == "crash":
                for c in msg["crashes"]:
                    print(f"-{c['drop_pct']*100:.0f}% {c['question']} ({c['category']})")

asyncio.run(main())
```

Or just open `https://api.protodex.io/ws/demo` in a browser. No auth needed for the free tier.

**What you receive:**
- `hello` — on connect, confirms your filters
- `crash` — when any matching market is newly past threshold OR its drop deepens by ≥2 points

**Use cases:** automated trade signals, dashboard live-update, prediction-market alerting bot, AI-agent context push.

## Pricing

| Tier | Price | Limit | What you get | Buy |
|------|-------|-------|--------------|-----|
| Free | $0 | 100 req/day | API + WS access, no auth | (just use it) |
| Sample | $1 one-time | — | 1-day full SQLite dataset | [Gumroad](https://manja8.gumroad.com/l/polymarket-data) |
| Standard | $9 one-time | 10K req/day for 30d | 30-day SQLite dataset + API key | [Gumroad](https://manja8.gumroad.com/l/agyjd) |
| Cross-Signal | $29 one-time | 30K req/day for 30d | Cross-signal dataset (BTC/ETH/SOL + Gold + Polymarket) + API key | [Gumroad](https://manja8.gumroad.com/l/cross-signal-dataset) |

**Key issuance is manual for now** (email `LuciferForge@proton.me` with Gumroad purchase ID — typically issued within 24h). Self-serve via webhook coming Q2 once Lemon Squeezy onboarding clears.

> Why Gumroad and not Stripe: Stripe doesn't onboard Indian sellers without a US/UK entity. Gumroad is the practical choice for a solo Indian dev shipping a global product. We pay the 10% friction tax for now.

Free tier needs no signup. Pro/Premium via Stripe (coming).

## Data

- **Markets:** ~9,500 markets across politics, sports, crypto, science, weather, more
- **Prices:** ~10M+ snapshots, 15-min cadence, 30-day rolling window (older data archived weekly)
- **Updates:** Every 15 minutes from Polymarket's public CLOB
- **No proprietary data, no scraping** — all data is public Polymarket CLOB

## Quick start (local)

```bash
git clone https://github.com/LuciferForge/polymarket-api.git
cd polymarket-api
pip install -r requirements.txt
# Place market_universe.db in this directory (sample available on request)
python app.py
# → http://127.0.0.1:8000/docs
```

The production deployment uses [protodex.io](https://protodex.io) infra — Cloudflare Tunnel + macOS launchd. Self-hosting is supported but not required.

## Why this exists

Backtesting prediction-market strategies typically requires either (a) scraping Polymarket's CLOB yourself (rate-limited, fragile) or (b) paying $24K/year for a Bloomberg-style data feed. Neither works for solo quant developers.

This API is the missing middle: a free entry-tier with paid scaling for serious users. Built to fund itself, not to fundraise.

## Built by

[LuciferForge](https://github.com/LuciferForge) — solo operator shipping data infrastructure for prediction markets and AI agents.

Live trading audit (the strategy this data feeds): [polymarket-historical-data](https://github.com/LuciferForge/polymarket-historical-data) · 79.8% WR over 302 closed trades.

## License

MIT
