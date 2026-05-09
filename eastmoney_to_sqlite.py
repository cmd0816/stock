#!/usr/bin/env python3
import argparse
import configparser
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
FIREFOX_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) "
    "Gecko/20100101 Firefox/128.0"
)


def detect_firefox_default_profile_dir() -> Optional[Path]:
    home = Path.home()
    candidates = [
        home / "Library/Application Support/Firefox/profiles.ini",  # macOS
        home / ".mozilla/firefox/profiles.ini",  # Linux
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Mozilla/Firefox/profiles.ini")  # Windows

    ini_path = next((p for p in candidates if p.exists()), None)
    if ini_path is None:
        return None

    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="utf-8")

    profile_section = None
    for section in parser.sections():
        if not section.startswith("Profile"):
            continue
        if parser.get(section, "Default", fallback="0") == "1":
            profile_section = section
            break
    if profile_section is None:
        for section in parser.sections():
            if section.startswith("Profile"):
                profile_section = section
                break
    if profile_section is None:
        return None

    path_value = parser.get(profile_section, "Path", fallback="").strip()
    if not path_value:
        return None
    is_relative = parser.get(profile_section, "IsRelative", fallback="1") == "1"
    base_dir = ini_path.parent
    profile_dir = (base_dir / path_value) if is_relative else Path(path_value).expanduser()
    return profile_dir if profile_dir.exists() else None


def fetch_text(url: str, timeout: int = 20) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_json(url: str, params: Dict[str, Any], timeout: int = 20, retries: int = 3) -> Dict[str, Any]:
    query = urlencode(params, safe=",")
    full_url = f"{url}?{query}"
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "application/json,text/plain,*/*",
    }
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            req = Request(full_url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
            return json.loads(body)
        except HTTPError as exc:
            last_error = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * attempt)
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * attempt)
            continue

    # Fallback to curl for endpoints that intermittently fail under urllib.
    curl_cmd = [
        "curl",
        "-sS",
        "-L",
        "--retry",
        "4",
        "--retry-delay",
        "1",
        "--retry-all-errors",
        "--max-time",
        str(timeout),
        "-H",
        f"User-Agent: {USER_AGENT}",
        "-H",
        "Referer: https://quote.eastmoney.com/",
        full_url,
    ]
    try:
        result = subprocess.run(curl_cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    except Exception as exc:
        raise RuntimeError(f"Request failed for {full_url}: {last_error}; curl fallback failed: {exc}") from exc


def extract_quotedata(html: str) -> Optional[Dict[str, Any]]:
    match = re.search(r"var\s+quotedata\s*=\s*(\{.*?\});", html, flags=re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def parse_market_code_from_url(url: str) -> Tuple[int, str]:
    path = urlparse(url).path.lower()
    match = re.search(r"/(sh|sz|bj)(\d{6})\.html", path)
    if not match:
        raise ValueError("Could not parse market/code from URL path.")

    market_prefix, code = match.groups()
    market_map = {"sh": 1, "sz": 0, "bj": 0}
    return market_map[market_prefix], code


def resolve_security(url: str) -> Tuple[int, str, Optional[str]]:
    html = fetch_text(url)
    quotedata = extract_quotedata(html)

    if quotedata and "market" in quotedata and "code" in quotedata:
        market = int(quotedata["market"])
        code = str(quotedata["code"])
        name = quotedata.get("name")
        return market, code, name

    market, code = parse_market_code_from_url(url)
    return market, code, None


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eastmoney_stock_quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT NOT NULL,
            market INTEGER NOT NULL,
            code TEXT NOT NULL,
            secid TEXT NOT NULL,
            name TEXT,
            price REAL,
            open REAL,
            high REAL,
            low REAL,
            prev_close REAL,
            volume REAL,
            turnover REAL,
            change_amount REAL,
            change_percent REAL,
            turnover_rate REAL,
            total_market_value REAL,
            circulating_market_value REAL,
            pe_ttm REAL,
            pb REAL,
            raw_json TEXT NOT NULL,
            fetched_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_eastmoney_code_time
        ON eastmoney_stock_quotes(code, fetched_at_utc)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eastmoney_stock_daily_klines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT NOT NULL,
            market INTEGER NOT NULL,
            code TEXT NOT NULL,
            secid TEXT NOT NULL,
            name TEXT,
            trade_date TEXT NOT NULL,
            open REAL,
            close REAL,
            high REAL,
            low REAL,
            volume REAL,
            turnover REAL,
            amplitude_percent REAL,
            change_percent REAL,
            change_amount REAL,
            turnover_rate REAL,
            raw_line TEXT NOT NULL,
            fetched_at_utc TEXT NOT NULL,
            UNIQUE(code, trade_date)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_eastmoney_kline_code_date
        ON eastmoney_stock_daily_klines(code, trade_date)
        """
    )
    conn.commit()


def fetch_stock_data(market: int, code: str) -> Dict[str, Any]:
    api_urls = [
        "https://push2.eastmoney.com/api/qt/stock/get",
        "https://push2delay.eastmoney.com/api/qt/stock/get",
    ]
    fields = ",".join(
        [
            "f57",
            "f58",
            "f43",
            "f46",
            "f44",
            "f45",
            "f60",
            "f47",
            "f48",
            "f116",
            "f117",
            "f169",
            "f170",
            "f168",
            "f162",
            "f167",
        ]
    )
    params = {
        "secid": f"{market}.{code}",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "invt": "2",
        "fltt": "2",
        "fields": fields,
    }
    errors = []
    for api_url in api_urls:
        try:
            payload = fetch_json(api_url, params=params)
            if payload.get("data"):
                return payload
            errors.append(f"{api_url} returned empty data")
        except Exception as exc:
            errors.append(f"{api_url} failed: {exc}")

    raise RuntimeError("All Eastmoney quote endpoints failed: " + " | ".join(errors))


def save_snapshot(db_path: Path, source_url: str, market: int, code: str, payload: Dict[str, Any]) -> None:
    data = payload.get("data", {})
    secid = f"{market}.{code}"
    fetched_at_utc = datetime.now(timezone.utc).isoformat()
    name = data.get("f58")

    row = (
        source_url,
        market,
        code,
        secid,
        name,
        to_float(data.get("f43")),
        to_float(data.get("f46")),
        to_float(data.get("f44")),
        to_float(data.get("f45")),
        to_float(data.get("f60")),
        to_float(data.get("f47")),
        to_float(data.get("f48")),
        to_float(data.get("f169")),
        to_float(data.get("f170")),
        to_float(data.get("f168")),
        to_float(data.get("f116")),
        to_float(data.get("f117")),
        to_float(data.get("f162")),
        to_float(data.get("f167")),
        json.dumps(payload, ensure_ascii=False),
        fetched_at_utc,
    )

    with sqlite3.connect(db_path) as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO eastmoney_stock_quotes (
                source_url, market, code, secid, name,
                price, open, high, low, prev_close,
                volume, turnover, change_amount, change_percent, turnover_rate,
                total_market_value, circulating_market_value, pe_ttm, pb,
                raw_json, fetched_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        conn.commit()


def fetch_stock_history_1y(market: int, code: str) -> Dict[str, Any]:
    api_urls = ["https://push2his.eastmoney.com/api/qt/stock/kline/get"]
    params = build_history_query_params(market, code)
    errors = []
    for api_url in api_urls:
        try:
            payload = fetch_json(api_url, params=params)
            data = payload.get("data", {})
            if data and data.get("klines"):
                return payload
            errors.append(f"{api_url} returned empty klines")
        except Exception as exc:
            errors.append(f"{api_url} failed: {exc}")

    raise RuntimeError("All Eastmoney history endpoints failed: " + " | ".join(errors))


def build_history_query_params(market: int, code: str, end_date: Optional[str] = None) -> Dict[str, str]:
    end_dt = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now()
    end_text = end_dt.strftime("%Y%m%d")
    beg_date = (end_dt - timedelta(days=365)).strftime("%Y%m%d")
    return {
        "secid": f"{market}.{code}",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",  # Daily K-line
        "fqt": "1",    # Forward-adjusted
        "beg": beg_date,
        "end": end_text,
        "lmt": "500",
    }


def fetch_stock_history_1y_via_browser(
    market: int,
    code: str,
    source_url: str,
    user_data_dir: Optional[str] = None,
    profile_directory: Optional[str] = None,
    headed: bool = False,
    browser_engine: str = "chromium",
    browser_channel: Optional[str] = "chrome",
    wait_for_login: bool = False,
) -> Dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError(
            "Playwright is required for browser mode. Install with: "
            "`pip install playwright && playwright install chromium`"
        ) from exc

    api_url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = build_history_query_params(market, code)
    full_url = f"{api_url}?{urlencode(params, safe=',')}"

    with sync_playwright() as playwright:
        browser_engine = (browser_engine or "chromium").lower()
        if browser_engine == "chromium":
            browser_type = playwright.chromium
        elif browser_engine == "firefox":
            browser_type = playwright.firefox
        elif browser_engine == "webkit":
            browser_type = playwright.webkit
        else:
            raise ValueError(
                f"Unsupported browser engine: {browser_engine}. "
                "Use one of: chromium, firefox, webkit."
            )

        effective_user_agent = FIREFOX_USER_AGENT if browser_engine == "firefox" else USER_AGENT
        if browser_engine == "firefox" and not user_data_dir:
            detected_profile = detect_firefox_default_profile_dir()
            if detected_profile:
                user_data_dir = str(detected_profile)
                print(f"Using Firefox default profile: {user_data_dir}")

        browser = None
        context = None
        try:
            if user_data_dir:
                launch_args = []
                if profile_directory and browser_engine == "chromium":
                    launch_args.append(f"--profile-directory={profile_directory}")
                persistent_kwargs: Dict[str, Any] = {
                    "user_data_dir": str(Path(user_data_dir).expanduser()),
                    "headless": not headed,
                    "args": launch_args,
                    "user_agent": effective_user_agent,
                    "locale": "en-US",
                }
                if browser_engine == "chromium" and browser_channel:
                    persistent_kwargs["channel"] = browser_channel
                context = browser_type.launch_persistent_context(**persistent_kwargs)
            else:
                launch_kwargs: Dict[str, Any] = {"headless": not headed}
                if browser_engine == "chromium" and browser_channel:
                    launch_kwargs["channel"] = browser_channel
                browser = browser_type.launch(**launch_kwargs)
                context = browser.new_context(user_agent=effective_user_agent, locale="en-US")

            page = context.new_page()

            # Warm up with the stock page to mimic normal browser navigation.
            try:
                page.goto(source_url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass

            if wait_for_login and headed:
                print("Browser opened. Please complete Eastmoney login in this window, then press Enter here to continue...")
                input()

            text = None
            try:
                response = page.goto(full_url, wait_until="domcontentloaded", timeout=45000)
                text = response.text() if response is not None else page.locator("body").inner_text()
            except Exception:
                # Fallback 1: run fetch in page context to mimic real browser request path.
                try:
                    text = page.evaluate(
                        """async (url) => {
                            const resp = await fetch(url, {
                                method: 'GET',
                                headers: {
                                    'accept': 'application/json,text/plain,*/*'
                                },
                                credentials: 'include'
                            });
                            return await resp.text();
                        }""",
                        full_url,
                    )
                except Exception:
                    # Fallback 2: browser request context API.
                    api_resp = context.request.get(
                        full_url,
                        headers={
                            "Accept": "application/json,text/plain,*/*",
                            "Referer": "https://quote.eastmoney.com/",
                            "User-Agent": effective_user_agent,
                        },
                        timeout=45000,
                    )
                    text = api_resp.text()

            payload = json.loads(text)
            data = payload.get("data", {})
            if not data or not data.get("klines"):
                raise RuntimeError("Browser mode returned empty klines payload.")
            return payload
        finally:
            if context is not None:
                context.close()
            if browser is not None:
                browser.close()


def parse_kline_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = payload.get("data", {})
    lines = data.get("klines", []) or []
    rows: List[Dict[str, Any]] = []

    for line in lines:
        parts = line.split(",")
        if len(parts) < 11:
            continue
        rows.append(
            {
                "trade_date": parts[0],
                "open": to_float(parts[1]),
                "close": to_float(parts[2]),
                "high": to_float(parts[3]),
                "low": to_float(parts[4]),
                "volume": to_float(parts[5]),
                "turnover": to_float(parts[6]),
                "amplitude_percent": to_float(parts[7]),
                "change_percent": to_float(parts[8]),
                "change_amount": to_float(parts[9]),
                "turnover_rate": to_float(parts[10]),
                "raw_line": line,
            }
        )
    return rows


def save_history_klines(
    db_path: Path, source_url: str, market: int, code: str, payload: Dict[str, Any]
) -> int:
    data = payload.get("data", {})
    secid = f"{market}.{code}"
    name = data.get("name")
    fetched_at_utc = datetime.now(timezone.utc).isoformat()
    kline_rows = parse_kline_rows(payload)

    with sqlite3.connect(db_path) as conn:
        init_db(conn)
        conn.executemany(
            """
            INSERT INTO eastmoney_stock_daily_klines (
                source_url, market, code, secid, name, trade_date,
                open, close, high, low, volume, turnover,
                amplitude_percent, change_percent, change_amount, turnover_rate,
                raw_line, fetched_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code, trade_date) DO UPDATE SET
                source_url=excluded.source_url,
                market=excluded.market,
                secid=excluded.secid,
                name=excluded.name,
                open=excluded.open,
                close=excluded.close,
                high=excluded.high,
                low=excluded.low,
                volume=excluded.volume,
                turnover=excluded.turnover,
                amplitude_percent=excluded.amplitude_percent,
                change_percent=excluded.change_percent,
                change_amount=excluded.change_amount,
                turnover_rate=excluded.turnover_rate,
                raw_line=excluded.raw_line,
                fetched_at_utc=excluded.fetched_at_utc
            """,
            [
                (
                    source_url,
                    market,
                    code,
                    secid,
                    name,
                    r["trade_date"],
                    r["open"],
                    r["close"],
                    r["high"],
                    r["low"],
                    r["volume"],
                    r["turnover"],
                    r["amplitude_percent"],
                    r["change_percent"],
                    r["change_amount"],
                    r["turnover_rate"],
                    r["raw_line"],
                    fetched_at_utc,
                )
                for r in kline_rows
            ],
        )
        conn.commit()
    return len(kline_rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Eastmoney stock data from a stock page URL and save to SQLite."
    )
    parser.add_argument("--url", required=True, help="Eastmoney stock URL, e.g. https://quote.eastmoney.com/concept/sh688343.html")
    parser.add_argument("--db", default="stocks.db", help="SQLite DB path (default: stocks.db)")
    parser.add_argument(
        "--history-1y",
        action="store_true",
        help="Fetch and save 1-year daily K-line history instead of one snapshot quote.",
    )
    parser.add_argument(
        "--history-1y-browser",
        action="store_true",
        help="Fetch 1-year history via Playwright browser navigation (useful when direct HTTP is blocked).",
    )
    parser.add_argument(
        "--browser-user-data-dir",
        default=None,
        help="Optional Chrome user data dir for browser mode (can reuse logged-in state).",
    )
    parser.add_argument(
        "--browser-profile-directory",
        default=None,
        help="Optional Chrome profile directory name for browser mode, e.g. 'Default' or 'Profile 1'.",
    )
    parser.add_argument(
        "--browser-headed",
        action="store_true",
        help="Run browser mode in headed (visible) mode instead of headless.",
    )
    parser.add_argument(
        "--browser-channel",
        default="chrome",
        help="Browser channel for Chromium engine (e.g. chrome, chromium, msedge). Default: chrome",
    )
    parser.add_argument(
        "--browser-engine",
        default="chromium",
        help="Browser engine for Playwright mode: chromium, firefox, or webkit. Default: chromium",
    )
    parser.add_argument(
        "--browser-wait-login",
        action="store_true",
        help="In headed browser mode, pause before API fetch so you can log in manually, then press Enter to continue.",
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    market, code, page_name = resolve_security(args.url)
    if args.history_1y_browser:
        payload = fetch_stock_history_1y_via_browser(
            market=market,
            code=code,
            source_url=args.url,
            user_data_dir=args.browser_user_data_dir,
            profile_directory=args.browser_profile_directory,
            headed=args.browser_headed,
            browser_engine=args.browser_engine,
            browser_channel=args.browser_channel,
            wait_for_login=args.browser_wait_login,
        )
        row_count = save_history_klines(db_path, args.url, market, code, payload)
        api_name = payload.get("data", {}).get("name")
        print(
            f"Saved {row_count} daily rows (1 year, browser mode) for {market}.{code} "
            f"({api_name or page_name or 'UNKNOWN'}) to {db_path}"
        )
    elif args.history_1y:
        payload = fetch_stock_history_1y(market, code)
        row_count = save_history_klines(db_path, args.url, market, code, payload)
        api_name = payload.get("data", {}).get("name")
        print(
            f"Saved {row_count} daily rows (1 year) for {market}.{code} "
            f"({api_name or page_name or 'UNKNOWN'}) to {db_path}"
        )
    else:
        payload = fetch_stock_data(market, code)
        save_snapshot(db_path, args.url, market, code, payload)
        api_name = payload.get("data", {}).get("f58")
        print(f"Saved snapshot for {market}.{code} ({api_name or page_name or 'UNKNOWN'}) to {db_path}")


if __name__ == "__main__":
    main()
