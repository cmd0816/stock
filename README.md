# Eastmoney Stock to Database

Small toolkit to:

- Fetch Eastmoney stock data by stock page URL
- Store snapshot and 1-year daily K-line data into SQLite
- Visualize multiple stocks in a local web dashboard (stock list, K-line, MA lines, chip diagram)

## Files

- `eastmoney_to_sqlite.py`: importer
- `view_quotes.py`: local dashboard server
- `stocks.db`: SQLite database (created/updated by importer)

## Requirements

- Python 3.10+
- `sqlite3` (built into Python)
- Optional for browser mode:
  - Playwright
  - Browser binaries (`chromium` / `firefox`)

## Setup (optional but recommended)

```bash
cd /Users/cmd/workspace/stock
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium firefox
```

## Import Data

### 1) Latest Snapshot

```bash
python3 eastmoney_to_sqlite.py \
  --url "https://quote.eastmoney.com/concept/sh688343.html" \
  --db stocks.db
```

### 2) 1-Year Daily K-line (direct HTTP)

```bash
python3 eastmoney_to_sqlite.py \
  --url "https://quote.eastmoney.com/concept/sz301071.html" \
  --db stocks.db \
  --history-1y
```

### 3) 1-Year Daily K-line (browser fallback)

Use this when direct mode is blocked by gateway/session checks.

```bash
/Users/cmd/workspace/stock/.venv/bin/python eastmoney_to_sqlite.py \
  --url "https://quote.eastmoney.com/concept/sz301071.html" \
  --db stocks.db \
  --history-1y-browser \
  --browser-engine firefox \
  --browser-headed \
  --browser-wait-login
```

Notes:

- `--browser-engine`: `chromium` / `firefox` / `webkit`
- `--browser-headed`: show browser window
- `--browser-wait-login`: pause script so you can log in manually, then press Enter
- `--browser-user-data-dir`: optional profile reuse path
- `--browser-profile-directory`: Chromium profile name (e.g. `Default`)

## Verify Imported Data

Example for code `301071`:

```bash
sqlite3 stocks.db \
"SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM eastmoney_stock_daily_klines WHERE code='301071';"
```

## Run Dashboard

```bash
python3 view_quotes.py --db stocks.db --port 8000
```

Open:

- `http://127.0.0.1:8000/daily`
- `http://127.0.0.1:8000/daily?code=301071`
- `http://127.0.0.1:8000/daily?code=688343`

## Dashboard Features

- Stock list sidebar
- Click stock to switch symbol
- K-line candlesticks
- MA overlays: `MA5`, `MA10`, `MA20`, `MA30`, `MA60`, `MA120`, `MA250`
- Crosshair + tooltip for hovered day details
- Chip diagram (estimated volume-by-price distribution)

## SQLite Tables

- `eastmoney_stock_quotes`: latest snapshot rows
- `eastmoney_stock_daily_klines`: daily historical rows (upsert by `code + trade_date`)

## Troubleshooting

- If `--history-1y` fails with `Empty reply` / `socket hang up`, use `--history-1y-browser`.
- If browser mode still fails, run headed + wait-login mode and log in manually before continuing.
