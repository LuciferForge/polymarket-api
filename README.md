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
| `GET /docs` | Auto-generated OpenAPI / Swagger docs |

Full OpenAPI spec at `/docs`. Try the live endpoints at `api.protodex.io`.

## Pricing

| Tier | Price | Limit | Use case |
|------|-------|-------|----------|
| Free | $0 | 100 req/day | Hobbyists, evals |
| Pro | $19/mo | 10,000 req/day | Backtests, dashboards |
| Premium | $99/mo | Unlimited | Production trading, AI agents |

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
