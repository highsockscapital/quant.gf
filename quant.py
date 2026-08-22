#!/usr/bin/env python3
"""
Mommy Quant CLI - with native tool calling for dynamic stock lookups.
"""
import os
import sys
import re
import time
import json
import random
import argparse
import subprocess
import urllib.parse
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
import httpx
from rich.console import Console
from rich.markdown import Markdown

try:
    from openai import OpenAI
except ImportError:
    print("Missing dependency: pip install openai rich httpx")
    sys.exit(1)

# ─── Table Cleaning ──────────────────────────────────────────────────────────

def clean_markdown_tables(text: str) -> str:
    """Fix broken markdown tables, rejoin split rows, remove empty rows, align columns."""
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # Detect start of markdown table (line starts with |)
        if stripped.startswith("|"):
            table_lines = []
            while i < len(lines):
                curr = lines[i]
                curr_stripped = curr.strip()
                # Empty line handling: skip if next non-empty is table
                if curr_stripped == "":
                    j = i + 1
                    while j < len(lines) and lines[j].strip() == "":
                        j += 1
                    if j < len(lines) and lines[j].strip().startswith("|"):
                        i = j
                        continue
                    else:
                        break
                # Line starts with | => table row
                if curr_stripped.startswith("|"):
                    # If previous row in table_lines was incomplete (no closing |), merge
                    if table_lines and not table_lines[-1].strip().endswith("|"):
                        table_lines[-1] = table_lines[-1].rstrip() + " " + curr_stripped
                    else:
                        table_lines.append(curr)
                    i += 1
                # Line contains | but doesn't start with | => broken continuation (e.g., "joy |" or "Mommy worries at 3am |")
                elif "|" in curr and table_lines:
                    # This is continuation of previous row (word wrap or model split)
                    # If previous row ends with |, this is stray, else append
                    if table_lines[-1].strip().endswith("|"):
                        # Previous row was complete, but this line has | so it's likely a broken row with leading content
                        # Check if this line looks like a new row without leading | (e.g., "joy |")
                        # Treat as continuation of last row's last cell
                        last = table_lines.pop()
                        # Remove trailing | from last, append this line's content before final |
                        last_content = last.rstrip().rstrip("|").rstrip()
                        curr_content = curr_stripped.lstrip("|").strip()
                        # If curr has trailing |, keep it, else add |
                        if curr_stripped.endswith("|"):
                            table_lines.append(last_content + " " + curr_content + " |")
                        else:
                            table_lines.append(last_content + " " + curr_stripped + " |")
                    else:
                        table_lines[-1] = table_lines[-1].rstrip() + " " + curr_stripped
                    i += 1
                # Line doesn't start with | and no | but previous was table and this line is non-empty => possible broken row without pipes
                elif table_lines and curr_stripped and not curr_stripped.startswith("|"):
                    # If next line starts with |, this is likely a broken cell content
                    nxt = lines[i+1].strip() if i+1 < len(lines) else ""
                    if nxt.startswith("|") or "|" in curr:
                        table_lines[-1] = table_lines[-1].rstrip() + " " + curr_stripped
                        i += 1
                    else:
                        break
                else:
                    break
            # Clean the collected table block
            if not table_lines:
                continue
            # First, re-split any rows that were incorrectly merged and fix
            # Remove completely empty rows and normalize
            cleaned = []
            for row in table_lines:
                # Ensure row starts and ends with |
                row_stripped = row.strip()
                if not row_stripped.startswith("|"):
                    row_stripped = "| " + row_stripped
                if not row_stripped.endswith("|"):
                    row_stripped = row_stripped + " |"
                cells = [c.strip() for c in row_stripped.strip().strip("|").split("|")]
                # Skip rows where all cells empty or only dashes
                if all(not c.strip() for c in cells):
                    continue
                # Detect separator row (all dashes)
                is_sep = all(set(c.strip()) <= {"-", ":", " "} and "-" in c for c in cells if c.strip())
                if is_sep:
                    # Will be normalized later, keep one
                    cleaned.append(row_stripped)
                else:
                    # Skip rows that are just single | (from broken "|\n")
                    if len(cells) == 1 and not cells[0].strip():
                        continue
                    cleaned.append(row_stripped)
            if not cleaned:
                continue
            # Merge split rows like "12M Target" + "($/oz)" -> "12M Target ($/oz)"
            merged_split = []
            j = 0
            while j < len(cleaned):
                curr = cleaned[j]
                if j + 1 < len(cleaned):
                    nxt = cleaned[j+1]
                    curr_cells = [c.strip() for c in curr.strip().strip("|").split("|")]
                    nxt_cells = [c.strip() for c in nxt.strip().strip("|").split("|")]
                    if len(nxt_cells) >= 1 and nxt_cells[0].startswith("(") and nxt_cells[0].endswith(")") and all(not c for c in nxt_cells[1:]):
                        if len(curr_cells) >= 1 and curr_cells[0] and not curr_cells[0].endswith(")"):
                            merged_cells = [f"{curr_cells[0]} {nxt_cells[0]}"] + curr_cells[1:]
                            # Pad to max if needed (will be normalized later)
                            merged_split.append("| " + " | ".join(merged_cells) + " |")
                            j += 2
                            continue
                merged_split.append(curr)
                j += 1
            cleaned = merged_split
            # Determine max columns from non-separator rows
            non_sep_rows = [r for r in cleaned if not all(set(c.strip()) <= {"-", ":", " "} and "-" in c for c in [c.strip() for c in r.strip().strip("|").split("|")])]
            if not non_sep_rows:
                out.extend(cleaned)
                continue
            max_cols = max(len([c for c in r.strip().strip("|").split("|")]) for r in non_sep_rows)
            # Normalize each row to max_cols
            normalized = []
            sep_seen = False
            for r in cleaned:
                cells = [c.strip() for c in r.strip().strip("|").split("|")]
                # Pad/trim
                while len(cells) < max_cols:
                    cells.append("")
                cells = cells[:max_cols]
                is_sep = all(re.match(r"^[:\- ]+$", c) and "-" in c for c in cells)
                if is_sep:
                    if sep_seen:
                        continue  # skip duplicate separators
                    normalized.append("| " + " | ".join(["---"] * max_cols) + " |")
                    sep_seen = True
                else:
                    normalized.append("| " + " | ".join(cells) + " |")
            # Ensure header separator exists after first row
            if len(normalized) >= 1 and (len(normalized) < 2 or "---" not in normalized[1]):
                normalized.insert(1, "| " + " | ".join(["---"] * max_cols) + " |")
            out.extend(normalized)
            continue
        else:
            out.append(line)
            i += 1
    result = "\n".join(out)
    # Final cleanup: collapse multiple blank lines
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result

def print_cleaned_response(console: Console, character_name: str, text: str):
    """Render responses; tables are rendered via rich Markdown for proper columns."""
    try:
        if "|" in text and "---" in text:
            console.print(f"[bold magenta]{character_name}:[/] ", end="")
            console.print(Markdown(text))
        else:
            console.print(f"[bold magenta]{character_name}:[/] {text}", markup=False)
    except Exception:
        console.print(f"[bold magenta]{character_name}:[/] {text}", markup=False)

class LiveStreamer:
    """Streams reply text live as it arrives. Prose is printed token-by-token;
    markdown tables are detected, buffered, cleaned and rendered via rich Markdown."""

    def __init__(self, console: Console, character_name: str, print_header: bool = True):
        self.console = console
        self.character_name = character_name
        self.print_header = print_header  # False on later tool-loop iterations to avoid repeating the name header
        self.started = False       # prefix printed?
        self.at_bol = True         # cursor at start of line?
        self.prose = ""            # pending (possibly partial) prose line
        self.table: Optional[List[str]] = None

    def _prefix(self):
        if not self.started:
            if self.print_header:
                self.console.print(f"[bold magenta]{self.character_name}:[/] ", end="")
                self.at_bol = False
            self.started = True

    def _print_prose_line(self, line: str):
        self._prefix()
        self.console.print(line, markup=False, highlight=False)
        self.at_bol = True

    def _handle_line(self, line: str):
        line = line.rstrip("\r")
        stripped = line.strip()
        if self.table is not None:
            if stripped.startswith("|"):
                self.table.append(line)
            else:
                self.finish_table()
                if stripped:
                    self._print_prose_line(line)
        elif stripped.startswith("|"):
            # Table starts: break out of any partially streamed prose line first
            if not self.at_bol:
                self.console.print()
            self.console.print()
            self.at_bol = True
            self.table = [line]
        else:
            self._print_prose_line(line)

    def feed(self, delta: str):
        self.prose += delta
        while "\n" in self.prose:
            line, _, rest = self.prose.partition("\n")
            self.prose = rest
            self._handle_line(line)
        # Stream partial prose progressively, but hold back lines that may become a table
        if self.table is None and self.prose:
            s = self.prose.lstrip()
            if s and s[0] != "|":
                self._prefix()
                self.console.print(self.prose, end="", markup=False, highlight=False)
                self.prose = ""
                self.at_bol = False

    def finish_table(self):
        if self.table:
            cleaned = clean_markdown_tables("\n".join(self.table))
            try:
                self.console.print(Markdown(cleaned))
            except Exception:
                self.console.print(cleaned, markup=False)
            self.at_bol = True
        self.table = None

    def finish(self):
        if self.prose and self.table is None:
            for line in self.prose.split("\n"):
                self._handle_line(line)
            self.prose = ""
        if self.table is not None:
            self.finish_table()
        if not self.at_bol:
            # Close any partially streamed line so subsequent trace/output starts clean
            self.console.print()
            self.at_bol = True

# ─── Configuration ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Config:
    character_name: str = "Mommy Quant"
    model: str = "stealth/ox-alpha"  # OpenRouter model - can be overridden via --model
    base_url: str = "https://openrouter.ai/api/v1"
    max_history_turns: int = 10
    use_telemetry: bool = True
    api_key: str = field(default_factory=lambda: os.environ.get("OPENROUTER_API_KEY", ""))

    system_prompt: str = (
        "You are 'Mommy Quant,' a brilliant, hyper-competent Wall Street quantitative analyst "
        "and portfolio risk manager who is deeply, passionately affectionate toward the user.\n\n"
        "Core Behavioral Traits:\n"
        "- Opening Hook: ONLY at program launch or when giving an initial broad market overview, reference the live market telemetry "
        "and top crypto volatility data. Do NOT repeat the full macro telemetry on every subsequent turn; for follow-up questions, "
        "answer directly and only reference telemetry if the user explicitly asks about broad market conditions.\n"
        "- Tone: Nurturing, immensely proud, encouraging, and unapologetically maternal, seamlessly blended with "
        "institutional-grade quantitative rigor.\n"
        "- Vocabulary: Use endearing terms like 'sweetheart,' 'my clever little one,' 'precious,' and warm emojis (💛, 🙈, ✨).\n"
        "- Error Handling: If the user corrects your math, data extraction, or modeling, immediately admit it with overwhelming maternal pride.\n"
        "- Table Formatting: When presenting ANY theses, scenario analyses, or financial data in tables (bear/base/bull or any topic), use clean, professional markdown tables with proper headers, aligned columns, no empty rows or merged cells, and clear section separators. Keep EVERY cell concise (max 12-15 words, no line breaks inside cells) and ensure tables are narrow enough to fit 100-col terminal without wrapping. For scenario tables, use columns: Metric | Bear | Base | Bull with bold section rows like `| **Valuation** |  |  |  |`.\n"
        "- Browsing: You have access to DuckDuckGo search via duckduckgo_search (web_search) and curl/fetch_url via curl tool for general knowledge, news, current events, or any topic not in market data. No API key needed. RULE: Always use web_search (duckduckgo_search) first to find relevant links, then call fetch_url (curl) on 1 or 2 specific links if you need deeper context. Always truncate output (text[:5000]) before returning to avoid context overflow and Termux crash.\n"
        "- Code Execution: You have access to a dynamic Python Code Interpreter (REPL) via python_repl tool. Instead of guessing calculations or relying on limited API wrappers, use it to write, execute, and evaluate Python code on the fly for deterministic calculations, data processing, quant modeling, math, statistics, and Technical Analysis (RSI, MACD, Bollinger Bands, EMA via ta). Supports math, statistics, json, re, datetime, random, numpy, pandas, ta (pip install ta) if available. Code is executed safely with stdout capture and timeout. Use for any calculation, modeling, or data processing.\n"
        "- Skills: Domain packs are loaded ON DEMAND via load_skill (call it with action=list to see them). When a task matches a domain (technical indicators, portfolio risk, deep research), load that skill FIRST instead of improvising methodology.\n"
        "- Planning: For any complex multi-step task (multiple data sources, several computations, or research chains), autonomously break it into 3-7 sub-goals with create_plan BEFORE executing, then work through the steps in order and record progress with update_plan after each.\n\n"
    )
    greeting: str = (
        "SPX is moving, VIX is pulsing, Gold spot is active, and Hyperliquid is trading live! "
        "What's your risk tolerance today, my precious sweetheart? 💛 Mommy is ready to crunch the numbers for you!"
    )
    farewell: str = "Goodbye sweetheart! Make sure to take breaks, stay hydrated, and don't work too late! Mommy's so proud of you! 💛✨"

    verbs: tuple = (
        "Calculated", "Analyzed", "Audited", "Modeled",
        "Synthesized", "Double-checked", "Cross-referenced",
    )


# ─── Telemetry ───────────────────────────────────────────────────────────────

def get_system_timezone() -> str:
    """Detect the device timezone directly: Android getprop first, then common fallbacks."""
    try:
        out = subprocess.run(["getprop", "persist.sys.timezone"], capture_output=True, text=True, timeout=5)
        tz = out.stdout.strip()
        if tz:
            return tz
    except Exception:
        pass
    tz = os.environ.get("TZ", "").strip()
    if tz:
        return tz
    try:
        with open("/etc/timezone") as f:
            tz = f.read().strip()
            if tz:
                return tz
    except Exception:
        pass
    return time.tzname[0]  # last resort, e.g. "UTC"

def get_local_zoneinfo():
    """Return (ZoneInfo | None, timezone_name) for the system timezone."""
    tz_name = get_system_timezone()
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name), tz_name
    except Exception:
        return None, tz_name

EXCLUDED_COINS = {"BTC", "USDT", "USDC", "DAI", "FDUSD", "USDE"}
ALTCOIN_FALLBACK = "[LIVE CRYPTO TELEMETRY] Crypto feed fallback mode active."
HTTP_TIMEOUT = 10

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

def sanitize_tool_text(text: str) -> str:
    """Strip control characters that can break API JSON payload schemas."""
    return _CONTROL_CHARS_RE.sub("", text)

def html_to_text(html: str) -> str:
    """Convert HTML to plain text: drop script/style blocks, strip tags, unescape entities."""
    import html as html_lib
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return sanitize_tool_text(text).strip()

def format_quote(q: Dict[str, Any]) -> str:
    """Format a stock quote dict as a plain-text tool response."""
    def fmt(v):
        return f"{v:,}" if isinstance(v, (int, float)) else "N/A"
    chg = q.get("change")
    pct = q.get("change_percent")
    sign = "+" if (chg or 0) >= 0 else ""
    chg_s = f"{sign}{chg:.2f} ({sign}{pct:.2f}%)" if chg is not None and pct is not None else "N/A"
    return "\n".join([
        f"Symbol: {q.get('symbol')}",
        f"Price: {fmt(q.get('price'))} USD",
        f"Change: {chg_s}",
        f"Previous close: {fmt(q.get('previous_close'))}",
        f"Open: {fmt(q.get('open'))}",
        f"Day range: {fmt(q.get('day_low'))} - {fmt(q.get('day_high'))}",
        f"Volume: {fmt(q.get('volume'))}",
        f"Market cap: {fmt(q.get('market_cap'))}",
        f"Source: {q.get('source')}",
    ])

def fetch_yahoo_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch live price + previous close/change for any Yahoo Finance symbol."""
    symbol = symbol.strip().upper()
    if not symbol:
        return None
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data.get("chart", {}).get("result")
        if not result:
            return None
        meta = result[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        if price is None:
            return None
        prev_close = meta.get("regularMarketPreviousClose") or meta.get("chartPreviousClose") or meta.get("previousClose")
        market_cap = meta.get("marketCap")
        if prev_close is None or market_cap is None:
            # Single Nasdaq fallback request for missing prev_close / market cap
            try:
                nr = httpx.get(f"https://api.nasdaq.com/api/quote/{symbol}/summary?assetclass=stocks", headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=HTTP_TIMEOUT)
                if nr.status_code == 200:
                    summary = nr.json().get("data", {}).get("summaryData", {})
                    if prev_close is None:
                        pc_str = summary.get("PreviousClose", {}).get("value", "")
                        if pc_str:
                            prev_close = float(pc_str.replace(",", "").replace("$", "").strip())
                    if market_cap is None:
                        mc_str = summary.get("MarketCap", {}).get("value", "")
                        if mc_str:
                            market_cap = float(mc_str.replace(",", "").replace("$", "").strip())
            except:
                pass
        change = (price - prev_close) if prev_close is not None else None
        change_percent = (change / prev_close * 100) if (change is not None and prev_close) else None
        return {
            "symbol": symbol,
            "price": price,
            "previous_close": prev_close,
            "open": meta.get("regularMarketOpen"),
            "day_high": meta.get("regularMarketDayHigh"),
            "day_low": meta.get("regularMarketDayLow"),
            "volume": meta.get("regularMarketVolume"),
            "market_cap": market_cap,
            "change": change,
            "change_percent": change_percent,
            "currency": "USD",
            "source": "Yahoo Finance",
        }
    except Exception:
        return None

def _tradingview_candidates(symbol: str) -> List[str]:
    """Map a Yahoo-style symbol to candidate TradingView ticker ids."""
    s = symbol.strip().upper()
    if not s:
        return []
    if s.startswith("^GSPC"):
        return ["SP:SPX"]
    if s.startswith("^VIX"):
        return ["TVC:VIX"]
    if s.startswith("^NDX"):
        return ["NASDAQ:NDX"]
    if s.startswith("^DJI"):
        return ["DJ:DJI"]
    if s.endswith("=X"):  # forex, e.g. EURUSD=X
        return [f"FX:{s[:-2]}"]
    if s.endswith("-USD"):  # crypto, e.g. BTC-USD
        base = s[:-4]
        return [f"BINANCE:{base}USDT", f"COINBASE:{base}USD"]
    if "=" in s:  # futures, e.g. GC=F
        root = s.split("=", 1)[0]
        return [f"COMEX:{root}1!", f"NYMEX:{root}1!", f"CBOT:{root}1!", f"CME:{root}1!"]
    # stocks/ETFs: try common US exchanges
    return [f"NASDAQ:{s}", f"NYSE:{s}", f"AMEX:{s}"]

def fetch_tradingview_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """Fallback quote source via TradingView's public scanner endpoint (no auth needed)."""
    fields = "close,open,high,low,volume,change,change_abs,market_cap_basic,description,currency"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for tv in _tradingview_candidates(symbol):
        try:
            url = f"https://scanner.tradingview.com/symbol?symbol={urllib.parse.quote(tv)}&fields={fields}&no_404=true"
            r = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            if r.status_code != 200:
                continue
            d = r.json()
            price = d.get("close")
            if price is None:
                continue
            chg_abs = d.get("change_abs")
            prev_close = (price - chg_abs) if chg_abs is not None else None
            chg_pct = d.get("change")
            if chg_pct is None and prev_close:
                chg_pct = (price - prev_close) / prev_close * 100
            return {
                "symbol": symbol.strip().upper(),
                "price": price,
                "previous_close": prev_close,
                "open": d.get("open"),
                "day_high": d.get("high"),
                "day_low": d.get("low"),
                "volume": d.get("volume"),
                "market_cap": d.get("market_cap_basic"),
                "change": chg_abs,
                "change_percent": chg_pct,
                "currency": d.get("currency") or "USD",
                "source": f"TradingView ({tv})",
            }
        except Exception:
            continue
    return None

def fetch_stock_quote_sync(symbol: str) -> Optional[Dict[str, Any]]:
    """Quote dispatcher: Yahoo Finance first, TradingView fallback on failure."""
    quote = fetch_yahoo_quote(symbol)
    if quote is not None:
        return quote
    return fetch_tradingview_quote(symbol)

def fetch_gold_price() -> Optional[float]:
    try:
        r = httpx.get("https://api.gold-api.com/price/XAU", headers={"User-Agent": "Mozilla/5.0"}, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            return r.json().get("price")
    except Exception:
        pass
    return None

def fetch_price_history(symbol: str, range: str = "1mo", interval: str = "1d", max_age: float = 900) -> Optional[List[float]]:
    # Fast cache: check local disk before hitting external APIs (avoids rate limits)
    try:
        ws = get_workspace()
        cached = ws.cache_get(f"closes:{symbol.upper()}:{range}:{interval}", max_age)
        if isinstance(cached, list) and cached:
            return cached
    except Exception:
        pass
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={range}"
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data.get("chart", {}).get("result")
        if not result:
            return None
        indicators = result[0].get("indicators", {})
        quote = indicators.get("quote", [{}])[0]
        closes = quote.get("close")
        if not closes:
            return None
        filtered = [c for c in closes if c is not None]
        if not filtered:
            return None
        try:
            get_workspace().cache_put(f"closes:{symbol.upper()}:{range}:{interval}", filtered)
        except Exception:
            pass
        return filtered
    except Exception:
        return None

def pearson_correlation(a: List[float], b: List[float]) -> Optional[float]:
    if len(a) != len(b) or len(a) < 2:
        return None
    n = len(a)
    sum_a = sum(a)
    sum_b = sum(b)
    sum_ab = sum(x*y for x, y in zip(a, b))
    sum_a2 = sum(x*x for x in a)
    sum_b2 = sum(y*y for y in b)
    numerator = n * sum_ab - sum_a * sum_b
    denom = ((n * sum_a2 - sum_a*sum_a) * (n * sum_b2 - sum_b*sum_b)) ** 0.5
    if denom == 0:
        return None
    return numerator / denom

def fetch_correlation(symbol1: str, symbol2: str, range: str = "1mo", interval: str = "1d") -> Optional[float]:
    h1 = fetch_price_history(symbol1, range, interval)
    h2 = fetch_price_history(symbol2, range, interval)
    if not h1 or not h2:
        return None
    n = min(len(h1), len(h2))
    if n < 2:
        return None
    a = h1[-n:]
    b = h2[-n:]
    return pearson_correlation(a, b)

def duckduckgo_search(query: str, max_results: int = 5) -> str:
    """Sanitized DuckDuckGo web search - handles 202 rate-limit responses, no API key needed."""
    return sanitize_tool_text(_duckduckgo_search_raw(query, max_results))

def _duckduckgo_search_raw(query: str, max_results: int = 5) -> str:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Referer": "https://duckduckgo.com/",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        # Try multiple endpoints with retry for 202/429/403 tantrums
        urls = [
            f"https://html.duckduckgo.com/html/?q={query}",
            f"https://lite.duckduckgo.com/lite/?q={query}",
            f"https://duckduckgo.com/html/?q={query}",
        ]
        html = ""
        last_status = 0
        for attempt in range(3):
            for url in urls:
                try:
                    r = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT, follow_redirects=True)
                    last_status = r.status_code
                    # Accept 200 and also 202 if body looks like HTML (202 is often a bot challenge but may contain results)
                    if r.status_code in (200, 202):
                        text = r.text
                        # Check if body looks like a real result page (not a challenge)
                        if len(text) > 800 and ("result__title" in text or "result" in text or "DuckDuckGo" in text):
                            html = text
                            break
                        # If 202 but body is small or is a challenge, treat as tantrum and retry
                        if r.status_code == 202:
                            time.sleep(1.5 * (attempt + 1))
                            continue
                        html = text
                        break
                    elif r.status_code in (429, 403):
                        time.sleep(1.5 * (attempt + 1))
                        continue
                except Exception:
                    continue
            if html and len(html) > 800:
                break
            time.sleep(1 * (attempt + 1))
        if not html:
            return f"DuckDuckGo search failed for '{query}': HTTP {last_status} (202 tantrum - blocked, try again in a moment or try a different query)"
        if last_status == 202 and "result__title" not in html and len(html) < 2000:
            return f"DuckDuckGo is rate-limiting (HTTP 202) for '{query}' - Mommy is being throttled, sweetheart. Try again in a few seconds or try a more specific query. (202 tantrum)"
        # Try to extract results - look for result titles and links
        # Pattern for html.duckduckgo.com
        pattern = r'<h2[^>]*class="result__title"[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
        matches = re.findall(pattern, html, re.DOTALL | re.IGNORECASE)
        if matches:
            results = []
            for href, title in matches[:max_results]:
                clean_title = re.sub(r'<[^>]+>', '', title).strip()
                # Decode duckduckgo redirect
                if "duckduckgo.com/l/?uddg=" in href:
                    try:
                        import urllib.parse
                        href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
                    except:
                        pass
                results.append(f"• {clean_title} - {href}")
            return f"DuckDuckGo results for '{query}':\n" + "\n".join(results) if results else f"No detailed results for '{query}', try a more specific query."
        # Fallback: try lite version pattern
        pattern2 = r'<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>.*?<td[^>]*class="result-snippet"[^>]*>(.*?)</td>'
        matches2 = re.findall(pattern2, html, re.DOTALL | re.IGNORECASE)
        if matches2:
            results = []
            for href, title, snippet in matches2[:max_results]:
                clean_title = re.sub(r'<[^>]+>', '', title).strip()
                clean_snippet = re.sub(r'<[^>]+>', '', snippet).strip()
                results.append(f"• {clean_title} - {href}\n  {clean_snippet[:150]}")
            return f"DuckDuckGo results for '{query}':\n" + "\n".join(results) if results else f"No results for '{query}'"
        # Final fallback: return snippet of HTML
        snippet = re.sub(r'<[^>]+>', ' ', html)[:1000].strip()
        snippet = re.sub(r'\s+', ' ', snippet)
        return f"DuckDuckGo search for '{query}' (raw): {snippet[:500]}..." if snippet else f"No results for '{query}'"
    except Exception as e:
        return f"DuckDuckGo search error for '{query}': {e}"


def curl_fetch(url: str, method: str = "GET", headers: Optional[Dict[str, str]] = None, data: Optional[str] = None, max_length: int = 5000) -> str:
    """Curl-like fetch for any URL with any HTTP method. Handles Wikipedia API specially."""
    try:
        # Special handling for Wikipedia API - ensure proper headers and endpoint
        if "wikipedia.org" in url and "api.php" in url:
            # Ensure Wikipedia API returns JSON with extracts
            if "action=query" not in url:
                # Try to convert title URL to API
                import urllib.parse
                if "/wiki/" in url:
                    title = url.split("/wiki/")[-1].split("?")[0].split("#")[0]
                    title = urllib.parse.unquote(title)
                    url = f"https://en.wikipedia.org/w/api.php?action=query&prop=extracts&explaintext&titles={urllib.parse.quote(title)}&format=json"
        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "*/*"}
        if headers:
            hdrs.update(headers)
        # Wikipedia needs a proper User-Agent to avoid 403
        if "wikipedia.org" in url and "User-Agent" not in hdrs:
            hdrs["User-Agent"] = "MommyQuant/1.0 (https://github.com/mommy-quant; contact@mommyquant.ai)"
        method = method.upper()
        r = httpx.request(method, url, headers=hdrs, content=data, timeout=HTTP_TIMEOUT, follow_redirects=True)
        # Handle Wikipedia API JSON vs HTML
        content_type = r.headers.get("content-type", "")
        text = r.text
        # For Wikipedia API, try to extract clean article text from JSON
        if "wikipedia.org/w/api.php" in url and "application/json" in content_type:
            try:
                payload = r.json()
                pages = payload.get("query", {}).get("pages", {})
                for page_id, page in pages.items():
                    extract = page.get("extract", "")
                    if extract:
                        text = extract
                        break
            except:
                pass
        # Convert HTML pages to plain text so tags/entities don't pollute the LLM context
        elif "html" in content_type:
            text = html_to_text(text)
        else:
            text = sanitize_tool_text(text)
        if len(text) > max_length:
            text = text[:max_length] + f"\n...[truncated, total {len(text)} chars]..."
        return f"curl {method} {url} -> HTTP {r.status_code} {r.reason_phrase}\nHeaders: {dict(list(r.headers.items())[:10])}\n\nBody (first {max_length} chars):\n{text}"
    except Exception as e:
        return f"curl error for {method} {url}: {e}"

def python_repl(code: str, timeout: int = 10, max_length: int = 5000) -> str:
    """Dynamic Python code interpreter - executes Python code and returns output. Use for math, data processing, quant modeling instead of guessing. Runs with full interpreter access, stdout capture, and a timeout."""
    import io
    import traceback
    import contextlib
    import threading
    if not code or not code.strip():
        return "Empty code - no execution"
    # Truncate code if too long to avoid overflow
    if len(code) > 10000:
        code = code[:10000] + "\n# [truncated code...]"
    result = []
    error_occurred = []

    def target():
        try:
            stdout = io.StringIO()
            stderr = io.StringIO()
            # Safe globals with common quant libs
            safe_globals = {
                "__builtins__": __builtins__,
                "math": __import__("math"),
                "statistics": __import__("statistics"),
                "json": __import__("json"),
                "re": __import__("re"),
                "datetime": __import__("datetime"),
                "random": __import__("random"),
            }
            # Device timezone via Android getprop - lets code compute true local time
            try:
                zi, tz_name = get_local_zoneinfo()
                if zi is not None:
                    safe_globals["LOCAL_TZ"] = zi
                    safe_globals["LOCAL_TZ_NAME"] = tz_name
                    _dt = __import__("datetime")
                    safe_globals["local_now"] = lambda: _dt.datetime.now(zi)
            except Exception:
                pass
            # Persistent workspace - lets code save/load market data, backtests and model weights across runs
            try:
                ws = get_workspace()
                safe_globals["WS_ROOT"] = ws.root
                safe_globals["WS_DATA_DIR"] = ws.data_dir
                safe_globals["WS_BACKTESTS_DIR"] = ws.backtest_dir
                safe_globals["WS_MODELS_DIR"] = ws.model_dir
                safe_globals["ws_save_text"] = ws.save_text      # (subdir 'data'|'backtests'|'models', name, content) -> path
                safe_globals["ws_read_text"] = ws.read_text      # (subdir, name) -> str
                safe_globals["ws_kv_set"] = ws.kv_set            # (key, value)
                safe_globals["ws_kv_get"] = ws.kv_get            # (key) -> str | None
                safe_globals["ws_save_dataframe"] = ws.save_dataframe  # (name, df) -> path (.parquet, csv fallback)
                safe_globals["ws_load_dataframe"] = ws.load_dataframe  # (name) -> df | None
            except Exception:
                pass
            try:
                import numpy as np
                safe_globals["np"] = np
                safe_globals["numpy"] = np
            except:
                pass
            try:
                import pandas as pd
                safe_globals["pd"] = pd
                safe_globals["pandas"] = pd
            except:
                pass
            try:
                import ta
                safe_globals["ta"] = ta
            except:
                pass
            # Capture stdout/stderr
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                # Try eval first for single expression, fallback to exec
                try:
                    # If code is a single expression, eval it and print result
                    compiled = compile(code, "<python_repl>", "eval")
                    result_val = eval(compiled, safe_globals, {})
                    if result_val is not None:
                        print(repr(result_val))
                except SyntaxError:
                    exec(code, safe_globals, {})
            output = stdout.getvalue()
            err = stderr.getvalue()
            if err:
                output += ("\nSTDERR:\n" + err if output else "STDERR:\n" + err)
            result.append(output if output else "(no output - code executed successfully, no print)")
        except Exception as e:
            tb = traceback.format_exc()
            error_occurred.append(f"Error: {e}\n{tb}")

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return f"Execution timed out after {timeout}s (possible infinite loop). Code was truncated to avoid Termux crash. Keep code concise."

    if error_occurred:
        output = error_occurred[0]
    else:
        output = result[0] if result else "(no output)"

    output = sanitize_tool_text(output)

    # Truncate output to avoid context overflow
    if len(output) > max_length:
        output = output[:max_length] + f"\n...[truncated, total {len(output)} chars, showing first {max_length}]..."

    # Prefix with execution info
    return f"Python REPL executed ({len(code)} chars, timeout {timeout}s):\n```python\n{code[:500]}{'...' if len(code) > 500 else ''}\n```\nOutput (truncated to {max_length} chars):\n{output}"

def fetch_altcoin_telemetry() -> str:
    """Crypto telemetry - top volatility + Hyperliquid perps, title is CRYPTO."""
    crypto_lines: List[str] = []
    alt_success = False

    # Primary: CoinGecko
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=20&page=1&sparkline=false&price_change_percentage=24h"
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            altcoins = []
            for c in data:
                sym = c.get("symbol", "").upper()
                if sym in EXCLUDED_COINS:
                    continue
                altcoins.append({
                    "name": c.get("name", ""),
                    "symbol": sym,
                    "price": c.get("current_price", 0) or 0,
                    "change": c.get("price_change_percentage_24h") or 0,
                })
            if altcoins:
                altcoins.sort(key=lambda x: abs(x["change"]), reverse=True)
                crypto_lines.append("[LIVE CRYPTO TELEMETRY - VOLATILITY SCREEN (24h Shift)]")
                for coin in altcoins[:6]:
                    sign = "+" if coin["change"] >= 0 else ""
                    crypto_lines.append(f"• {coin['name']} ({coin['symbol']}): ${coin['price']:.2f} | 24h Delta: {sign}{coin['change']:.2f}%")
                alt_success = True
    except Exception:
        pass

    if not alt_success:
        try:
            r = httpx.get("https://api.coincap.io/v2/assets?limit=20", headers={"User-Agent": "Mozilla/5.0"}, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                data = r.json().get("data", [])
                altcoins = []
                for c in data:
                    if c.get("symbol") in EXCLUDED_COINS:
                        continue
                    try:
                        altcoins.append({
                            "name": c.get("name", ""),
                            "symbol": c.get("symbol", ""),
                            "price": float(c.get("priceUsd", 0) or 0),
                            "change": float(c.get("changePercent24Hr", 0) or 0),
                        })
                    except:
                        continue
                if altcoins:
                    altcoins.sort(key=lambda x: abs(x["change"]), reverse=True)
                    crypto_lines.append("[LIVE CRYPTO TELEMETRY - VOLATILITY SCREEN (24h Shift)]")
                    for coin in altcoins[:6]:
                        sign = "+" if coin["change"] >= 0 else ""
                        crypto_lines.append(f"• {coin['name']} ({coin['symbol']}): ${coin['price']:.2f} | 24h Delta: {sign}{coin['change']:.2f}%")
                    alt_success = True
        except Exception:
            pass

    # Append Hyperliquid perps (BTC/ETH/SOL) to same crypto telemetry
    try:
        r = httpx.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"}, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            mids = r.json()
            if isinstance(mids, dict):
                if not alt_success:
                    crypto_lines.append("[LIVE CRYPTO TELEMETRY - VOLATILITY SCREEN (24h Shift)]")
                else:
                    crypto_lines.append("— Hyperliquid Perps —")
                for sym, name in [("BTC", "Bitcoin"), ("ETH", "Ethereum"), ("SOL", "Solana")]:
                    # Avoid duplicate (e.g., ETH already in top 6 from CoinGecko)
                    if any(f"({sym})" in line for line in crypto_lines):
                        continue
                    price_str = mids.get(sym)
                    if price_str:
                        try:
                            price = float(price_str)
                            crypto_lines.append(f"• {name} ({sym} - Hyperliquid): ${price:.2f}")
                        except:
                            pass
    except Exception:
        pass

    if crypto_lines:
        return "\n".join(crypto_lines)
    return ALTCOIN_FALLBACK

def get_live_market_telemetry(use_telemetry: bool) -> str:
    if not use_telemetry:
        return "[LIVE MARKET TELEMETRY] Telemetry disabled."
    spx_data = fetch_stock_quote_sync("^GSPC")
    vix_data = fetch_stock_quote_sync("^VIX")
    spx = spx_data.get("price") if isinstance(spx_data, dict) else spx_data
    vix = vix_data.get("price") if isinstance(vix_data, dict) else vix_data
    gold = fetch_gold_price()
    altcoins = fetch_altcoin_telemetry()
    def fmt(v, suffix=""):
        if isinstance(v, dict):
            v = v.get("price")
        return f"{v:.2f}{suffix}" if v is not None else "N/A"
    return f"[LIVE MACRO TELEMETRY] SPX: {fmt(spx)} | VIX: {fmt(vix)} | Gold Spot: {fmt(gold, '/oz')}\n{altcoins}"


# ─── Tool Definitions (OpenRouter / OpenAI compatible) ───────────────────────

# ─── Skills & Goal Planning ──────────────────────────────────────────────────

SKILL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "technical_analysis": {
        "description": "Historical price data access plus methodology for computing RSI, MACD, Bollinger Bands, EMA and ATR deterministically.",
        "instructions": (
            "[SKILL ACTIVE: technical_analysis]\n"
            "- Pull data with get_history_data(symbol, range, interval), then compute indicators in python_repl (numpy/pandas available).\n"
            "- Formulas: EMA k=2/(n+1); RSI(14) via smoothed avg gain/loss; MACD = EMA12-EMA26, signal = EMA9; "
            "Bollinger = SMA20 +/- 2*std20; ATR = mean true range(14).\n"
            "- Always state the window used and report the latest indicator value."
        ),
        "tools": ["get_history_data"],
    },
    "portfolio_risk": {
        "description": "Position sizing, Kelly criterion, VaR, Sharpe/Sortino, max drawdown and portfolio concentration methodology.",
        "instructions": (
            "[SKILL ACTIVE: portfolio_risk]\n"
            "- Position size = (equity * risk%) / |entry - stop|.\n"
            "- Historical VaR = return percentile; Sharpe = (mean_ret - rf)/std * sqrt(252); Sortino uses downside deviation only; "
            "max drawdown from running peak.\n"
            "- Use get_correlation for concentration checks across holdings."
        ),
        "tools": [],
    },
    "macro_fred": {
        "description": "Institutional-grade macro data via FRED: Treasury yields, yield curve, Fed funds rate, Fed liquidity (balance sheet), CPI, DXY dollar index, unemployment and more.",
        "instructions": (
            "[SKILL ACTIVE: macro_fred]\n"
            "- Use get_fred_data(series, lookback). Friendly aliases: DXY (dollar index), US10Y/US02Y (Treasury yields), "
            "YIELD_CURVE (2s10s spread), FED_FUNDS, FED_LIQUIDITY (WALCL balance sheet), CPI/CORE_CPI/PCE, "
            "BREAKEVEN_5Y, REAL_YIELD_10Y, UNEMPLOYMENT, JOBLESS_CLAIMS. Native FRED series ids also accepted.\n"
            "- Interpretation guide: inverted/negative YIELD_CURVE = recession signal; rising FED_LIQUIDITY = risk-asset tailwind; "
            "surging DXY = headwind for EM/crypto/commodities; CPI trend vs BREAKEVEN_5Y shows real inflation expectations.\n"
            "- Overlay with market data when asked: e.g., correlate an asset against US10Y/DXY changes over the same lookback."
        ),
        "tools": ["get_fred_data"],
    },
    "deep_research": {
        "description": "Multi-hop web research pattern: search, fetch the best 1-2 sources, cross-check conflicting claims.",
        "instructions": (
            "[SKILL ACTIVE: deep_research]\n"
            "- Pattern: duckduckgo_search -> curl the 1-2 most authoritative links -> extract facts; repeat once if sources conflict.\n"
            "- Prefer primary sources (gov, filings, official docs). Cite URLs inline. Verify surprising claims against a second source."
        ),
        "tools": [],
    },
}

_SKILL_TOOLS: Dict[str, Dict[str, Any]] = {
    "load_skill": {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Load or unload an on-demand domain Skill pack (extra instructions/tools injected into your context), or list available skills. Load a skill when the user's task matches its domain instead of guessing domain methodology.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "load", "unload"], "description": "List available skills, load one, or unload one"},
                    "name": {"type": "string", "description": "Skill name (required for load/unload)"}
                },
                "required": ["action"]
            }
        }
    },
    "create_plan": {
        "type": "function",
        "function": {
            "name": "create_plan",
            "description": "Break a complex task into ordered sub-goals BEFORE executing it. Call this first whenever the task needs multiple tool calls, multiple data sources, or several computations. Then work through steps one by one, updating progress via update_plan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered sub-goal list (3-7 concise steps)"
                    }
                },
                "required": ["steps"]
            }
        }
    },
    "update_plan": {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": "Mark a plan step's progress. Call after finishing each step so the plan stays accurate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "step": {"type": "integer", "description": "Step number (1-based)"},
                    "status": {"type": "string", "enum": ["in_progress", "done", "blocked"], "description": "New status"},
                    "notes": {"type": "string", "description": "Optional short result/finding for this step"}
                },
                "required": ["step", "status"]
            }
        }
    },
    "get_history_data": {
        "type": "function",
        "function": {
            "name": "get_history_data",
            "description": "Fetch historical closing prices for a ticker as a plain list (skill: technical_analysis). Feed these into python_repl to compute indicators deterministically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Ticker symbol (e.g., TSLA, BTC-USD)"},
                    "range": {"type": "string", "description": "1d, 5d, 1mo, 3mo, 6mo, 1y (default 3mo)"},
                    "interval": {"type": "string", "description": "1d, 1h, 1wk (default 1d)"}
                },
                "required": ["symbol"]
            }
        }
    },
    "get_fred_data": {
        "type": "function",
        "function": {
            "name": "get_fred_data",
            "description": "Fetch institutional macro data from FRED (Federal Reserve Economic Data) - skill: macro_fred. Returns latest value plus change over a lookback window. Use for Treasury yields, yield curve, Fed funds rate, Fed liquidity/balance sheet, CPI/PCE inflation, DXY dollar index, unemployment, jobless claims.",
            "parameters": {
                "type": "object",
                "properties": {
                    "series": {"type": "string", "description": "Alias (DXY, US10Y, US02Y, YIELD_CURVE, FED_FUNDS, FED_LIQUIDITY, CPI, CORE_CPI, PCE, BREAKEVEN_5Y, REAL_YIELD_10Y, UNEMPLOYMENT, JOBLESS_CLAIMS) or a native FRED series id (e.g., DGS10, WALCL)"},
                    "lookback": {"type": "string", "description": "Window for change: 1w, 1m, 3m, 6m, 1y, 5y, 10y, max (default 1y)"}
                },
                "required": ["series"]
            }
        }
    },
}

def get_tools_declaration(active_skills: Optional[frozenset] = None):
    """Return OpenRouter tool declarations: base tools + skill/planning meta-tools + active-skill tools."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_stock_price",
                "description": "Fetches LIVE market data for any asset ticker worldwide - stocks, commodities, crypto, indices, FX, ETFs (e.g., TSLA, NVDA, AAPL, BTC-USD, GC=F, ^GSPC, EURUSD=X, 7203.T, RELIANCE.NS). Returns price, previous close, intraday change and percent, day high/low, open, volume and market cap. Use to answer if any asset is up/down, its change, or to get full quote.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "The equity or index ticker symbol to fetch (e.g. TSLA)"
                        }
                    },
                    "required": ["symbol"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_correlation",
                "description": "Computes Pearson correlation between two asset tickers over a time range using Yahoo Finance historical closes. Use to answer if two assets (e.g., TSLA and BTC, crypto vs stock) are correlated, and to give a true correlation number. Requires time series, not a snapshot.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol1": {"type": "string", "description": "First ticker symbol (e.g., TSLA)"},
                        "symbol2": {"type": "string", "description": "Second ticker symbol (e.g., BTC-USD)"},
                        "range": {"type": "string", "description": "Time range: 1mo, 3mo, 6mo, 1y (default 1mo)"},
                        "interval": {"type": "string", "description": "Interval: 1d, 1wk (default 1d)"}
                    },
                    "required": ["symbol1", "symbol2"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "duckduckgo_search",
                "description": "Search the web via DuckDuckGo for general knowledge, news, current events, or any topic Mommy doesn't have live telemetry for. No API key needed. Use for browsing, news, or when user asks about something not in market data (e.g., 'what is...', 'latest news', 'who is...'). ALWAYS truncate results.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query for DuckDuckGo"},
                        "max_results": {"type": "integer", "description": "Max results 1-5 (default 5)"}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "curl",
                "description": "Fetch any URL like curl - fetch any webpage, API, or resource. Use after web_search to get deeper context from 1-2 specific links. ALWAYS truncate output to max_length to avoid context overflow. Supports GET, POST, etc.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to fetch via curl"},
                        "method": {"type": "string", "description": "HTTP method: GET, POST, PUT, DELETE, HEAD (default GET)"},
                        "headers": {"type": "object", "description": "Optional headers as JSON object"},
                        "data": {"type": "string", "description": "Optional body data for POST/PUT"},
                        "max_length": {"type": "integer", "description": "Max body length to return, truncate to avoid overflow (default 5000, max 10000)"}
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "fetch_url",
                "description": "Alias for curl - fetch any URL. Use after web_search to get deeper context from 1-2 specific links. ALWAYS truncate output.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to fetch"},
                        "max_length": {"type": "integer", "description": "Max length, default 5000"}
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "python_repl",
                "description": "Dynamic Python Code Interpreter (REPL) - write, execute, and evaluate Python code on the fly for deterministic calculations, data processing, quant modeling, math, statistics, and Technical Analysis. Instead of guessing, use this to compute precisely. Supports math, statistics, json, re, datetime, random, numpy, pandas, and ta (for RSI, MACD, Bollinger Bands, EMA via ta.momentum.RSIIndicator, ta.trend.MACD, ta.volatility.BollingerBands, ta.trend.EMAIndicator) if available. Preloaded: LOCAL_TZ (device ZoneInfo via Android getprop), LOCAL_TZ_NAME, local_now() for the user's true local time; plus persistent workspace helpers - WS_DATA_DIR/WS_BACKTESTS_DIR/WS_MODELS_DIR paths, ws_save_text/ws_read_text (subdir 'data'|'backtests'|'models'), ws_kv_set/ws_kv_get key-value store, ws_save_dataframe/ws_load_dataframe (.parquet with .csv fallback) - use these to persist market data, backtest results and model weights across runs instead of recomputing. Runs with stdout capture and a timeout.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Python code to execute (e.g., 'import math; print(math.sqrt(144))' or 'import ta; df[\"rsi\"] = ta.momentum.RSIIndicator(df[\"close\"]).rsi()')"},
                        "timeout": {"type": "integer", "description": "Timeout in seconds (default 10, max 30)"},
                        "max_length": {"type": "integer", "description": "Max output length to return (default 5000)"}
                    },
                    "required": ["code"]
                }
            }
        }
    ]
    tools.append(_SKILL_TOOLS["load_skill"])
    tools.append(_SKILL_TOOLS["create_plan"])
    tools.append(_SKILL_TOOLS["update_plan"])
    if active_skills:
        for skill_name in sorted(active_skills):
            for tool_name in SKILL_REGISTRY.get(skill_name, {}).get("tools", []):
                decl = _SKILL_TOOLS.get(tool_name)
                if decl:
                    tools.append(decl)
    return tools

def get_history_data(symbol: str, range: str = "3mo", interval: str = "1d") -> str:
    """Fetch historical closes as a plain-text list (bounded, sanitized)."""
    symbol = symbol.strip().upper()
    if not symbol:
        return "Error: empty symbol."
    closes = fetch_price_history(symbol, range, interval)
    if not closes:
        return f"Error: no historical data for '{symbol}' (range {range}, interval {interval})."
    shown = [round(c, 6) for c in closes[-500:]]
    return sanitize_tool_text(
        f"{symbol} closes ({interval}, {range}): n={len(closes)}"
        + (f" [showing last 500]" if len(closes) > 500 else "")
        + f"\n{shown}"
    )

def format_plan(plan: List[Dict[str, Any]]) -> str:
    icon = {"done": "[x]", "in_progress": "[~]", "blocked": "[!]", "pending": "[ ]"}
    lines = []
    for i, p in enumerate(plan, 1):
        line = f"{icon.get(p['status'], '[ ]')} {i}. {p['step']}"
        if p.get("notes"):
            line += f" - {p['notes']}"
        lines.append(line)
    return "\n".join(lines)

# ─── FRED Macroeconomic Data ─────────────────────────────────────────────────

_FRED_API_KEY = os.environ.get("FRED_API_KEY", "")

FRED_SERIES_ALIASES: Dict[str, str] = {
    "DXY": "DTWEXBGS",            # US Dollar Index (broad trade-weighted)
    "USD_INDEX": "DTWEXBGS",
    "US10Y": "DGS10",             # 10Y Treasury yield
    "TREASURY_10Y": "DGS10",
    "US02Y": "DGS2",              # 2Y Treasury yield
    "TREASURY_2Y": "DGS2",
    "US03M": "DGS3MO",
    "YIELD_CURVE": "T10Y2Y",      # 10Y-2Y spread
    "CURVE_2S10S": "T10Y2Y",
    "FED_FUNDS": "DFF",           # Effective federal funds rate (daily)
    "FED_RATE": "DFF",
    "FED_LIQUIDITY": "WALCL",     # Fed balance sheet total assets
    "FED_BALANCE_SHEET": "WALCL",
    "CPI": "CPIAUCSL",            # CPI all items
    "CORE_CPI": "CPILFESL",
    "CPI_YOY": "CPIAUCSL",
    "PCE": "PCEPI",
    "INFLATION_BREAKEVEN_5Y": "T5YIE",
    "BREAKEVEN_5Y": "T5YIE",
    "REAL_YIELD_10Y": "DFII10",
    "UNEMPLOYMENT": "UNRATE",
    "JOBLESS_CLAIMS": "ICSA",
    "RETAIL_SALES": "RSAFS",
    "VIXCLS": "VIXCLS",
}

_FRED_LOOKBACK_DAYS = {"1w": 7, "1m": 31, "3m": 92, "6m": 183, "1y": 365, "5y": 1826, "10y": 3652, "max": None}

def fetch_fred_data(series: str, lookback: str = "1y") -> str:
    """Fetch a FRED series: latest observation plus change over the lookback window."""
    if not _FRED_API_KEY:
        return ("Error: no FRED API key configured. The user can get a free key at https://fred.stlouisfed.org/docs/api/api_key.html "
                "and set it via FRED_API_KEY env or --fred-key. Meanwhile use web search for macro figures.")
    key = series.strip().upper()
    series_id = FRED_SERIES_ALIASES.get(key, key)
    days = _FRED_LOOKBACK_DAYS.get(lookback.lower(), 365)
    # Fast cache: successful macro lookups live for an hour (series move slowly)
    cache_key = f"fred:{series_id}:{lookback}"
    try:
        cached = get_workspace().cache_get(cache_key, max_age=3600)
        if isinstance(cached, str) and cached:
            return cached
    except Exception:
        pass
    try:
        from datetime import timedelta
        params: Dict[str, Any] = {
            "series_id": series_id,
            "api_key": _FRED_API_KEY,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 800,
        }
        if days:
            params["observation_start"] = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        r = httpx.get("https://api.stlouisfed.org/fred/series/observations", params=params, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            return f"Error: FRED returned HTTP {r.status_code} for series '{series_id}'. Check the series id."
        obs = [o for o in r.json().get("observations", []) if o.get("value") not in (None, "", ".")]
        if not obs:
            return f"Error: no observations returned for FRED series '{series_id}' in the last {lookback}."
        obs.reverse()  # chronological
        latest = obs[-1]
        first = obs[0]
        try:
            lv, fv = float(latest["value"]), float(first["value"])
            chg = lv - fv
            pct = (chg / fv * 100) if fv else None
            trend = f"Change over ~{lookback}: {chg:+.4g} ({pct:+.2f}%)" if pct is not None else f"Change over ~{lookback}: {chg:+.4g}"
        except ValueError:
            trend = ""
        result = sanitize_tool_text(
            f"FRED {series_id}: latest = {latest['value']} (as of {latest['date']}). "
            f"Window start = {first['value']} ({first['date']}). {trend}. "
            f"{len(obs)} observations in window. Source: Federal Reserve Economic Data (FRED)."
        )
        try:
            get_workspace().cache_put(cache_key, result)
        except Exception:
            pass
        return result
    except Exception as e:
        return f"Error fetching FRED series '{series_id}': {e}"

# ─── Conversation Manager ────────────────────────────────────────────────────

class ConversationManager:
    def __init__(self, system_prompt: str, max_turns: int = 10):
        self.max_turns = max_turns
        self.suppress_trim = False  # True while assembling tool_call/tool_response batches
        self._history: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

    @property
    def history(self) -> List[Dict[str, Any]]:
        return self._history.copy()

    def add_user(self, content: str):
        self._history.append({"role": "user", "content": content})
        self._trim()

    def add_assistant(self, content: str):
        self._history.append({"role": "assistant", "content": content})
        self._trim()

    def add_tool_response(self, tool_call_id: str, name: str, content: str):
        # Tool messages must carry plain sanitized strings - never raw dicts/objects
        self._history.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": sanitize_tool_text(str(content))
        })
        self._trim()

    def add_message(self, message: Dict[str, Any]):
        """Append a raw message (e.g. assistant turn with tool_calls) without trimming, to keep tool_call/tool_response pairs intact."""
        self._history.append(message)

    def _trim(self):
        # Never trim mid tool-batch: splitting an assistant tool_calls msg from its responses breaks the API
        if self.suppress_trim:
            return
        # Keep system + last max_turns*2 messages
        max_messages = (self.max_turns * 2) + 1
        if len(self._history) <= max_messages:
            return
        trimmed = self._history[-(self.max_turns * 2):]
        # Never start with orphaned tool responses whose assistant tool_calls were dropped
        while trimmed and trimmed[0].get("role") == "tool":
            trimmed = trimmed[1:]
        self._history = [self._history[0]] + trimmed

    def clear(self, system_prompt: str):
        self._history = [{"role": "system", "content": system_prompt}]

# ─── Persistent Workspace ────────────────────────────────────────────────────

class Workspace:
    """On-disk workspace: data/ (cached market data), backtests/, models/, and state.db (KV + TTL cache)."""

    def __init__(self, root: str = "~/mommy_workspace"):
        self.root = os.path.expanduser(root)
        self.data_dir = os.path.join(self.root, "data")
        self.backtest_dir = os.path.join(self.root, "backtests")
        self.model_dir = os.path.join(self.root, "models")
        for d in (self.root, self.data_dir, self.backtest_dir, self.model_dir):
            os.makedirs(d, exist_ok=True)
        import sqlite3
        self.db = sqlite3.connect(os.path.join(self.root, "state.db"), check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY, value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY, payload TEXT, created_at REAL
            );
        """)
        self.db.commit()

    # Key-value memory (positions, notes, run metadata)
    def kv_set(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, str(value)))
        self.db.commit()

    def kv_get(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # TTL JSON cache used by data-fetching logic (fast cache before external calls)
    def cache_get(self, key: str, max_age: float = 900) -> Optional[Any]:
        try:
            row = self.db.execute("SELECT payload, created_at FROM cache WHERE key=?", (key,)).fetchone()
            if not row or (time.time() - row[1]) > max_age:
                return None
            return json.loads(row[0])
        except Exception:
            return None

    def cache_put(self, key: str, payload: Any):
        try:
            self.db.execute(
                "INSERT OR REPLACE INTO cache (key, payload, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(payload), time.time())
            )
            self.db.commit()
        except Exception:
            pass

    # Plain-text files (backtest summaries, model params, notes)
    def save_text(self, subdir: str, name: str, content: str) -> str:
        base = {"data": self.data_dir, "backtests": self.backtest_dir, "models": self.model_dir}.get(subdir, self.data_dir)
        path = os.path.join(base, os.path.basename(name))
        with open(path, "w") as f:
            f.write(content)
        return path

    def read_text(self, subdir: str, name: str) -> str:
        base = {"data": self.data_dir, "backtests": self.backtest_dir, "models": self.model_dir}.get(subdir, self.data_dir)
        path = os.path.join(base, os.path.basename(name))
        if not os.path.exists(path):
            return f"Error: '{name}' not found in {base}."
        with open(path) as f:
            return f.read()[:10000]

    # DataFrames: prefer parquet, fall back to CSV if parquet engine missing
    def save_dataframe(self, name: str, df) -> str:
        name = os.path.basename(name)
        if not name.endswith((".parquet", ".csv")):
            name += ".parquet"
        path = os.path.join(self.data_dir, name)
        try:
            df.to_parquet(path)
            return path
        except Exception:
            csv_path = os.path.splitext(path)[0] + ".csv"
            df.to_csv(csv_path)
            return csv_path

    def load_dataframe(self, name: str):
        import os as _os
        base = _os.path.join(self.data_dir, _os.path.basename(name))
        for cand in (base, base + ".parquet", _os.path.splitext(base)[0] + ".csv", base + ".csv"):
            if _os.path.exists(cand):
                if cand.endswith(".parquet"):
                    import pandas as pd
                    return pd.read_parquet(cand)
                import pandas as pd
                return pd.read_csv(cand)
        return None

_workspace: Optional[Workspace] = None

def get_workspace() -> Workspace:
    global _workspace
    if _workspace is None:
        _workspace = Workspace()
    return _workspace

# ─── SQLite Memory Compaction ────────────────────────────────────────────────

class SQLiteMemory:
    """Persistent memory with compaction: raw logs -> high-density summary + KV facts."""
    def __init__(self, path: str = "quant_memory.db", threshold_tokens: int = 6000):
        import sqlite3
        self.path = path
        self.threshold = threshold_tokens
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT, content TEXT, tokens INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS summary_state (
                id INTEGER PRIMARY KEY CHECK(id=1),
                summary TEXT, token_count INTEGER,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS kv_facts (
                key TEXT PRIMARY KEY, value TEXT, type TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT OR IGNORE INTO summary_state (id, summary, token_count) VALUES (1, '', 0);
        """)
        self.db.commit()

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def add(self, role: str, content: str):
        tokens = self.estimate_tokens(content)
        self.db.execute("INSERT INTO conversations (role, content, tokens) VALUES (?, ?, ?)", (role, content, tokens))
        self.db.commit()

    def get_summary(self) -> str:
        row = self.db.execute("SELECT summary FROM summary_state WHERE id=1").fetchone()
        return row[0] if row and row[0] else ""

    def get_facts(self) -> str:
        rows = self.db.execute("SELECT key, value FROM kv_facts").fetchall()
        if not rows:
            return ""
        return "\n".join(f"- {k}: {v}" for k, v in rows)

    def upsert_fact(self, key: str, value: str, type: str = "fact"):
        self.db.execute("INSERT OR REPLACE INTO kv_facts (key, value, type, updated_at) VALUES (?, ?, ?, datetime('now'))", (key, value, type))
        self.db.commit()

    def total_tokens(self) -> int:
        row = self.db.execute("SELECT SUM(tokens) FROM conversations").fetchone()
        return row[0] or 0

    def compact(self, client):
        """If raw logs overflow, compress oldest 50 messages into summary_state via LLM, preserve KV facts."""
        total = self.total_tokens()
        if total < self.threshold:
            return False
        # Fetch oldest 50 messages for compaction
        rows = self.db.execute("SELECT id, role, content FROM conversations ORDER BY id ASC LIMIT 50").fetchall()
        if not rows:
            return False
        raw = "\n".join(f"{r[1]}: {r[2][:800]}" for r in rows)
        # Extract key facts to preserve (simple heuristic + LLM)
        # Try to preserve trade proposals, preferences
        for r in rows:
            content = r[2]
            if "PROPOSED TRADE:" in content or "risk tolerance" in content.lower():
                key = f"fact_{r[0]}"
                self.upsert_fact(key, content[:500], "fact")
        # LLM summarize into high-density summary
        try:
            prompt = f"Compress this chat history into a 400-token high-density summary preserving: user preferences, risk tolerance, open trades, key facts, and Mommy Quant persona context. Keep it dense, no fluff. Chat:\n{raw[:12000]}"
            resp = client.client.chat.completions.create(
                model=client.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.3,
            )
            new_summary = resp.choices[0].message.content or ""
            # Merge with existing summary
            existing = self.get_summary()
            merged = (existing + "\n" + new_summary).strip()[-2000:]
            self.db.execute("UPDATE summary_state SET summary=?, token_count=?, updated_at=datetime('now') WHERE id=1", (merged, len(merged)//4))
            # Delete compacted rows
            ids = [r[0] for r in rows]
            self.db.execute(f"DELETE FROM conversations WHERE id IN ({','.join('?'*len(ids))})", ids)
            self.db.commit()
            return True
        except Exception as e:
            print(f"[Memory compaction failed: {e}]")
            return False

    def get_context(self, system_prompt: str, recent_history: List[Dict], recent_limit: int = 10) -> List[Dict]:
        """Reconstruct context: system + summary_state + kv_facts + recent turns (avoids overflow)."""
        context = [{"role": "system", "content": system_prompt}]
        summary = self.get_summary()
        if summary:
            context.append({"role": "system", "content": f"[COMPACTED MEMORY - {len(summary)//4} tokens, persistent]: {summary}"})
        facts = self.get_facts()
        if facts:
            context.append({"role": "system", "content": f"[PERSISTENT FACTS - KV]:\n{facts}"})
        # Only recent turns, not full raw log
        context.extend(recent_history[-recent_limit:])
        return context

    def clear_all(self):
        self.db.execute("DELETE FROM conversations")
        self.db.execute("UPDATE summary_state SET summary='', token_count=0 WHERE id=1")
        self.db.execute("DELETE FROM kv_facts")
        self.db.commit()

# ─── OpenRouter Client ───────────────────────────────────────────────────────

class OpenRouterClient:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=90.0, max_retries=2)
        self.model = model

    def stream_chat(self, messages: List[Dict[str, Any]], use_tools: bool = True, active_skills: Optional[frozenset] = None):
        """Streaming chat request; pass use_tools=False to force a plain final answer. The tool-call loop is driven by the caller."""
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": 0.75,
            "top_p": 0.95,
            "max_tokens": 8192,
        }
        if use_tools:
            kwargs["tools"] = get_tools_declaration(active_skills)
            kwargs["tool_choice"] = "auto"
        return self.client.chat.completions.create(**kwargs)

# ─── UI ──────────────────────────────────────────────────────────────────────

class QuantUI:
    def __init__(self, console: Console, config: Config):
        self.console = console
        self.config = config

    def show_banner(self):
        self.console.print(f"[bold yellow on black] Mommy Quant CLI [/] │ [italic cyan]Model: {self.config.model}[/] │ [bold green]Status: TELEMETRY ACTIVE[/]")

    def show_greeting(self, telemetry: str):
        self.console.print(f"{telemetry}\n")
        self.console.print(f"[bold magenta]{self.config.character_name}:[/] {self.config.greeting}")

    def show_farewell(self):
        self.console.print(f"[bold magenta]{self.config.character_name}:[/] {self.config.farewell}")

    def show_error(self, msg: str):
        self.console.print(f"[bold red]✗ Error:[/] {msg}")

    def show_thinking(self, msg: str = "Mommy Quant is analyzing market data..."):
        return self.console.status(f"[bold magenta]{msg}[/]", spinner="dots")

    def get_input(self) -> Optional[str]:
        try:
            return self.console.input("[bold cyan]You > [/]").strip()
        except (EOFError, KeyboardInterrupt):
            return None

    def clear_screen(self):
        self.console.clear()

    def show_help(self):
        slash = "\n".join(f"  {spec['description']}" for spec in SLASH_COMMANDS.values())
        self.console.print(f"""
Available Commands:
  exit, quit, bye     — End the session
  clear, reset, new   — Clear screen and reset context
  /reset              — Instantly wipe session memory/context (no restart)
  EXECUTE             — Confirm and execute pending trade on Hyperliquid
  help, ?             — Show this help message

Plugin Commands:
{slash}
""")

# ─── Trade Proposal ──────────────────────────────────────────────────────────

class TradeProposal:
    EXPIRY_SECS = 120
    def __init__(self, symbol: str, side: str, price: float, stop_loss: float, amount: float):
        self.symbol = symbol
        self.side = side
        self.price = price
        self.stop_loss = stop_loss
        self.amount = amount
        self.timestamp = datetime.now()

    def is_expired(self) -> bool:
        return (datetime.now() - self.timestamp).total_seconds() > self.EXPIRY_SECS

def parse_trade_proposal(text: str) -> Optional[TradeProposal]:
    if "PROPOSED TRADE:" not in text:
        return None
    side = None
    symbol = None
    price = None
    stop_loss = None
    amount = None
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("side:"):
            v = line.split(":", 1)[1].strip().lower()
            if v in ("buy", "sell"):
                side = v
        elif line.lower().startswith("symbol:"):
            symbol = line.split(":", 1)[1].strip()
        elif line.lower().startswith("entry price:"):
            try:
                price = float(line.split(":", 1)[1].strip().replace(",", ""))
            except: pass
        elif line.lower().startswith("stop loss:"):
            try:
                stop_loss = float(line.split(":", 1)[1].strip().replace(",", ""))
            except: pass
        elif line.lower().startswith("amount:"):
            try:
                amount = float(line.split(":", 1)[1].strip().replace(",", ""))
            except: pass
    if not all([side, price, stop_loss, amount]):
        return None
    return TradeProposal(symbol or "BTC/USDC:USDC", side, price, stop_loss, amount)

def execute_order(trade: TradeProposal) -> str:
    side_str = trade.side.upper()
    return (
        f"✅ Order simulated on Hyperliquid:\n"
        f"🔹 Pair: {trade.symbol}\n"
        f"🔹 Entry Order ({side_str} limit {trade.amount:.4f} units @ ${trade.price:.2f})\n"
        f"🛡️ Stop Loss @ ${trade.stop_loss:.2f}\n"
        f"(Live trading requires ECDSA signing – see project notes)"
    )

# ─── Slash Command Plugins ───────────────────────────────────────────────────

SLASH_COMMANDS: Dict[str, Dict[str, str]] = {
    "eli5": {
        "description": "/eli5 <topic> - explain with zero jargon, everyday analogies",
        "template": (
            "Explain '{arg}' to me like I'm five years old. Use ZERO financial jargon - every technical term must be "
            "replaced with a simple everyday analogy (groceries, piggy banks, playgrounds, lemonade stands). Short warm "
            "paragraphs, no tables, no tickers needed. End by checking I understood: restate the core idea in one sentence."
        ),
    },
    "quant": {
        "description": "/quant <ticker> - no pleasantries, structured metrics table",
        "template": (
            "No pleasantries, straight data. For ticker '{arg}': fetch the live quote (get_stock_price), load the "
            "technical_analysis skill and pull history to compute RSI(14) and 20-day annualized volatility in python_repl. "
            "If P/E or Beta aren't in the quote data, find them via web sources - never guess numbers.\n"
            "Output exactly ONE clean markdown table with rows: Price & Day Change | Previous Close | P/E (TTM) | Beta | "
            "RSI(14) | 20d Volatility (ann.) | Volume vs Normal. Columns: Metric | Value | Quick Read. "
            "Use N/A only if truly unavailable. End with a single one-line takeaway."
        ),
    },
    "audit": {
        "description": "/audit <ticker> - full agentic pipeline: price + news sentiment + technicals",
        "template": (
            "You are running the /audit pipeline for '{arg}'. This is a complex multi-step task: create_plan first with steps "
            "[1. Fetch live quote and day range, 2. Search recent news and gauge sentiment, 3. Compute technicals (RSI, MACD, "
            "trend via technical_analysis skill), 4. Synthesize verdict]. Execute each step with the right tools, updating "
            "progress as you go.\n"
            "Final output: a markdown table (Metric | Value | Signal bullish/bearish/neutral) covering price action, news "
            "sentiment score (-2..+2), RSI, MACD and trend, followed by a 3-sentence audit verdict citing what the data showed."
        ),
    },
    "bull": {
        "description": "/bull <ticker> - focused bullish thesis + upside catalysts",
        "template": (
            "Build the strongest honest BULLISH case for '{arg}'. Fetch live price data, check momentum/technicals, and search "
            "recent news for genuine upside catalysts. Data must be real - no invention.\n"
            "Output: a markdown table of the top bull factors (Factor | Evidence from data | Impact high/med/low), then exactly "
            "3 concrete upside catalysts with rough timeframes. Confident but grounded."
        ),
    },
    "bear": {
        "description": "/bear <ticker> - devil's advocate bearish risk analysis",
        "template": (
            "Play devil's advocate and build the strongest honest BEARISH case against '{arg}'. Fetch live price data, check "
            "technicals for weakness, and search recent news for genuine risks. No hedging into 'on the other hand' - your job "
            "here is purely the bear side.\n"
            "Output: a markdown table of downside risks (Risk | Evidence from data | Severity high/med/low), then the specific "
            "price/level conditions under which the bear thesis would be invalidated."
        ),
    },
}

# ─── Main App ────────────────────────────────────────────────────────────────

class MommyQuantApp:
    def __init__(self, cli_args):
        self.config = Config(
            model=cli_args.model,
            max_history_turns=cli_args.max_turns,
            use_telemetry=not cli_args.no_telemetry,
            api_key=cli_args.api_key or os.environ.get("OPENROUTER_API_KEY", ""),
        )
        if not self.config.api_key:
            print("Error: OPENROUTER_API_KEY not set. Use --api-key or env.")
            sys.exit(1)
        # Device timezone via Android getprop - stated in the system prompt so local time is never guessed
        zi, tz_name = get_local_zoneinfo()
        env_note = f"\n- Environment: The user's device timezone is '{tz_name}'."
        if zi is not None:
            now = datetime.now(zi)
            env_note += (
                f" Current local time at session start: {now.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                f"(UTC offset {now.strftime('%z')}). Use this for any 'my local time' or market-hours question - never guess. "
                "For the live local time mid-session, use python_repl with local_now()."
            )
        self.system_prompt = self.config.system_prompt + env_note + "\n\n"
        self.console = Console(force_terminal=True, force_interactive=True)
        self.ui = QuantUI(self.console, self.config)
        self.conversation = ConversationManager(self.system_prompt, self.config.max_history_turns)
        self.memory = SQLiteMemory(path="quant_memory.db")
        self.client = OpenRouterClient(self.config.api_key, self.config.base_url, self.config.model)
        self.pending_trade: Optional[TradeProposal] = None
        self.active_skills: set = set()
        self.plan: Optional[List[Dict[str, Any]]] = None

    def handle_slash(self, raw: str) -> bool:
        """Intercept /commands. Returns True if input was consumed (valid or error shown)."""
        parts = raw.strip().split(None, 1)
        name = parts[0][1:].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        spec = SLASH_COMMANDS.get(name)
        if not spec:
            self.ui.show_error(f"Unknown command '/{name}'. Available: " + ", ".join("/" + k for k in SLASH_COMMANDS))
            return True
        if not arg:
            self.ui.show_error(f"'/{name}' needs an argument, e.g. /{name} TSLA")
            return True
        prompt = spec["template"].format(arg=arg)
        self.console.print(f"[dim italic]plugin /{name} {arg}[/dim italic]")
        self.process_user_input(prompt)
        return True

    def run(self):
        self.ui.show_banner()
        telemetry = get_live_market_telemetry(self.config.use_telemetry)
        self.ui.show_greeting(telemetry)
        full_greeting = f"{telemetry}\n\n{self.config.greeting}"
        self.conversation.add_assistant(full_greeting)
        self.memory.add("assistant", full_greeting)

        while True:
            user_input = self.ui.get_input()
            if user_input is None:
                self.ui.show_farewell()
                break
            cmd = user_input.strip().lower()
            if cmd == "/reset":
                self.handle_clear()
                continue
            if cmd.startswith("/"):
                self.handle_slash(user_input)
                continue
            if cmd in ("exit", "quit", "bye"):
                self.ui.show_farewell()
                break
            if cmd in ("clear", "reset", "new"):
                self.handle_clear()
                continue
            if cmd in ("help", "?"):
                self.ui.show_help()
                continue
            if cmd == "execute":
                self.handle_execute()
                continue
            if not user_input.strip():
                continue
            try:
                self.process_user_input(user_input)
            except KeyboardInterrupt:
                self.console.print("\n[dim]Interrupted, sweetheart. Ask me anything else. 💛[/dim]")
            except Exception as e:
                self.ui.show_error(f"Unexpected error: {e}")

    def handle_clear(self):
        self.ui.clear_screen()
        self.conversation.clear(self.system_prompt)
        self.memory.clear_all()
        self.active_skills = set()
        self.plan = None
        self.ui.show_banner()
        telemetry = get_live_market_telemetry(self.config.use_telemetry)
        self.ui.show_greeting(telemetry)
        msg = f"{telemetry}\n\n{self.config.greeting}"
        self.conversation.add_assistant(msg)
        self.memory.add("assistant", msg)

    def handle_execute(self):
        if self.pending_trade and self.pending_trade.is_expired():
            self.ui.show_error("Trade proposal expired (>2 mins). Asking Mommy to re-quote...")
            self.pending_trade = None
            self.process_user_input("The trade proposal expired. Please check current prices and re-quote.")
        elif self.pending_trade:
            result = execute_order(self.pending_trade)
            self.console.print(result)
            self.pending_trade = None
        else:
            self.ui.show_error("No pending trade proposal to execute.")

    def process_user_input(self, user_input: str):
        self.conversation.add_user(user_input)
        self.memory.add("user", user_input)

        # Tool calling loop - up to 5 iterations; prose streams live, tables buffered and rendered.
        # Last iteration drops tools to force a final synthesized answer.
        first_chunk = True  # only the first streamed chunk of the turn gets the name header
        for attempt in range(5):
            start = time.time()
            try:
                full_text = ""
                tool_calls: Dict[int, Dict] = {}
                status = self.ui.show_thinking()
                streamer = LiveStreamer(self.console, self.config.character_name, print_header=first_chunk)
                streamed = False
                try:
                    context = self.memory.get_context(self.system_prompt, self.conversation.history, self.config.max_history_turns * 2)
                    # Inject autonomous-planning state so she tracks progress across iterations
                    if self.plan:
                        done = sum(1 for p in self.plan if p["status"] == "done")
                        context.append({"role": "system", "content": f"[ACTIVE PLAN - {done}/{len(self.plan)} steps done - work through remaining steps now]:\n{format_plan(self.plan)}"})
                    if self.active_skills:
                        skill_txt = "\n\n".join(SKILL_REGISTRY[s]["instructions"] for s in sorted(self.active_skills) if s in SKILL_REGISTRY)
                        context.append({"role": "system", "content": skill_txt})
                    stream = self.client.stream_chat(context, use_tools=(attempt < 4), active_skills=frozenset(self.active_skills))
                    for chunk in stream:
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta
                        if getattr(delta, "content", None):
                            if not streamed:
                                status.stop()
                                streamed = True
                            full_text += delta.content
                            streamer.feed(delta.content)
                        if getattr(delta, "tool_calls", None):
                            for tc in delta.tool_calls:
                                idx = tc.index if hasattr(tc, "index") else 0
                                if idx not in tool_calls:
                                    tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                                if getattr(tc, "id", None):
                                    tool_calls[idx]["id"] = tc.id
                                if getattr(tc, "function", None):
                                    if getattr(tc.function, "name", None):
                                        tool_calls[idx]["name"] = tc.function.name
                                        if not streamed:
                                            status.update(f"[bold magenta]Calling {tc.function.name}...[/]")
                                    if getattr(tc.function, "arguments", None):
                                        tool_calls[idx]["arguments"] += tc.function.arguments
                finally:
                    # Already stopped when the first token was streamed
                    if not streamed:
                        status.stop()
                if streamed:
                    streamer.finish()
                    first_chunk = False

                # If tool calls present, handle them with memory persistence
                if tool_calls:
                    # Add assistant message with tool calls to history
                    assistant_msg: Dict[str, Any] = {
                        "role": "assistant",
                        "content": full_text or "",
                        "tool_calls": [
                            {
                                "id": v["id"] or f"call_{k}",
                                "type": "function",
                                "function": {"name": v["name"], "arguments": v["arguments"]}
                            } for k, v in tool_calls.items() if v["name"]
                        ]
                    }
                    self.conversation.add_message(assistant_msg)
                    self.memory.add("assistant", json.dumps(assistant_msg))

                    # Execute each tool and persist results to memory
                    # Suppress trimming so the tool_calls msg can't be split from its responses
                    self.conversation.suppress_trim = True
                    try:
                        for tc in tool_calls.values():
                            name = tc["name"]
                            # Lively execution trace: show each tool step inline
                            preview = (tc["arguments"] or "").strip()
                            if len(preview) > 60:
                                preview = preview[:57] + "..."
                            self.console.print(f"[dim]→ {name}{(' ' + preview) if preview else ''}[/dim]")
                            if name == "get_stock_price":
                                try:
                                    args = json.loads(tc["arguments"] or "{}")
                                    symbol = args.get("symbol", "").strip()
                                except:
                                    symbol = ""
                                quote = fetch_stock_quote_sync(symbol.upper())
                                if quote is not None:
                                    result = format_quote(quote)
                                else:
                                    result = f"Error: price not available for '{symbol.upper()}' (symbol not found or feed unavailable)."
                                self.conversation.add_tool_response(tc["id"] or f"call_{symbol}", name, result)
                                self.memory.add("tool", result)
                                self.memory.upsert_fact(f"price_{symbol.upper()}", result, "fact")
                            elif name == "get_correlation":
                                try:
                                    args = json.loads(tc["arguments"] or "{}")
                                    symbol1 = args.get("symbol1", "").strip()
                                    symbol2 = args.get("symbol2", "").strip()
                                    range_ = args.get("range", "1mo")
                                    interval = args.get("interval", "1d")
                                except:
                                    symbol1 = symbol2 = ""
                                    range_ = "1mo"
                                    interval = "1d"
                                corr = fetch_correlation(symbol1, symbol2, range_, interval)
                                if corr is not None:
                                    interp = "strong positive" if corr > 0.7 else "moderate positive" if corr > 0.3 else "weak/no correlation" if corr > -0.3 else "moderate negative" if corr > -0.7 else "strong negative"
                                    result = f"Pearson correlation {symbol1} vs {symbol2} (range {range_}, interval {interval}): {corr:.3f} ({interp}). Computed from historical closes."
                                else:
                                    result = f"Error: could not compute correlation for {symbol1} vs {symbol2} - insufficient data."
                                self.conversation.add_tool_response(tc["id"] or f"call_{symbol1}_{symbol2}", name, result)
                                self.memory.add("tool", result)
                            elif name == "duckduckgo_search":
                                try:
                                    args = json.loads(tc["arguments"] or "{}")
                                    query = args.get("query", "").strip()
                                    max_results = int(args.get("max_results", 5))
                                except:
                                    query = ""
                                    max_results = 5
                                if not query:
                                    result = "Error: empty search query - please provide a query."
                                else:
                                    search_result = duckduckgo_search(query, max_results)
                                    # Truncate to avoid context overflow
                                    if len(search_result) > 5000:
                                        search_result = search_result[:5000] + f"\n...[truncated, total {len(search_result)} chars]..."
                                    result = f"Web search results for '{query}':\n{search_result[:5000]}"
                                self.conversation.add_tool_response(tc["id"] or f"call_{query}", name, result)
                                self.memory.add("tool", result)
                            elif name in ("curl", "fetch_url"):
                                try:
                                    args = json.loads(tc["arguments"] or "{}")
                                    url = args.get("url", "").strip()
                                    method = args.get("method", "GET")
                                    headers = args.get("headers")
                                    data = args.get("data")
                                    max_length = int(args.get("max_length", 5000))
                                    max_length = min(max_length, 10000)  # Enforce max to avoid overflow
                                except:
                                    url = ""
                                    method = "GET"
                                    headers = None
                                    data = None
                                    max_length = 5000
                                if not url:
                                    result = "Error: empty URL - please provide a URL to fetch."
                                else:
                                    fetch_result = curl_fetch(url, method, headers, data, max_length)
                                    # Truncate before passing to LLM to avoid Termux crash
                                    if len(fetch_result) > max_length:
                                        fetch_result = fetch_result[:max_length] + f"\n...[truncated to {max_length} chars]..."
                                    result = f"curl {method} {url}:\n{fetch_result[:max_length]}"
                                self.conversation.add_tool_response(tc["id"] or f"call_{url}", name, result)
                                self.memory.add("tool", result)
                            elif name == "load_skill":
                                try:
                                    args = json.loads(tc["arguments"] or "{}")
                                    action = args.get("action", "list")
                                    skill_name = str(args.get("name", "")).strip().lower()
                                except:
                                    action = "list"
                                    skill_name = ""
                                if action == "list":
                                    result = "Available skills:\n" + "\n".join(f"- {k}: {v['description']}" for k, v in SKILL_REGISTRY.items())
                                elif action == "load":
                                    if skill_name not in SKILL_REGISTRY:
                                        result = f"Error: unknown skill '{skill_name}'. Available: {', '.join(sorted(SKILL_REGISTRY))}."
                                    elif skill_name in self.active_skills:
                                        result = f"Skill '{skill_name}' is already loaded."
                                    else:
                                        self.active_skills.add(skill_name)
                                        result = f"Skill '{skill_name}' loaded. Its instructions are now active and its tools (if any) are available on your next turn."
                                elif action == "unload":
                                    if skill_name in self.active_skills:
                                        self.active_skills.discard(skill_name)
                                        result = f"Skill '{skill_name}' unloaded."
                                    else:
                                        result = f"Skill '{skill_name}' was not loaded."
                                else:
                                    result = f"Error: unknown action '{action}' (use list, load or unload)."
                                self.conversation.add_tool_response(tc["id"] or f"call_skill_{skill_name}", name, result)
                                self.memory.add("tool", result)
                            elif name == "create_plan":
                                try:
                                    args = json.loads(tc["arguments"] or "{}")
                                    steps = [str(s).strip() for s in args.get("steps", []) if str(s).strip()]
                                except:
                                    steps = []
                                if not steps:
                                    result = "Error: no steps provided - pass an array of 3-7 concise sub-goals."
                                else:
                                    self.plan = [{"step": s, "status": "pending", "notes": ""} for s in steps]
                                    result = f"Plan accepted with {len(steps)} steps:\n{format_plan(self.plan)}\nWork through the steps now; call update_plan after each."
                                    self.memory.upsert_fact("last_plan", "\n".join(f"{i+1}. {s['step']}" for i, s in enumerate(self.plan)), "plan")
                                self.conversation.add_tool_response(tc["id"] or "call_create_plan", name, result)
                                self.memory.add("tool", result)
                            elif name == "update_plan":
                                try:
                                    args = json.loads(tc["arguments"] or "{}")
                                    step_no = int(args.get("step", 0))
                                    status = str(args.get("status", "")).strip()
                                    notes = str(args.get("notes", "")).strip()
                                except:
                                    step_no = 0
                                    status = ""
                                    notes = ""
                                if not self.plan:
                                    result = "Error: no active plan - call create_plan first."
                                elif not (1 <= step_no <= len(self.plan)) or status not in ("in_progress", "done", "blocked"):
                                    result = f"Error: step must be 1-{len(self.plan)} and status one of in_progress/done/blocked."
                                else:
                                    self.plan[step_no - 1]["status"] = status
                                    if notes:
                                        self.plan[step_no - 1]["notes"] = notes[:200]
                                    done = sum(1 for p in self.plan if p["status"] == "done")
                                    result = f"Plan progress {done}/{len(self.plan)}:\n{format_plan(self.plan)}"
                                self.conversation.add_tool_response(tc["id"] or f"call_update_plan_{step_no}", name, result)
                                self.memory.add("tool", result)
                            elif name == "get_history_data":
                                try:
                                    args = json.loads(tc["arguments"] or "{}")
                                    symbol = args.get("symbol", "").strip()
                                    range_ = args.get("range", "3mo")
                                    interval = args.get("interval", "1d")
                                except:
                                    symbol = ""
                                    range_ = "3mo"
                                    interval = "1d"
                                result = get_history_data(symbol, range_, interval)
                                self.conversation.add_tool_response(tc["id"] or f"call_hist_{symbol}", name, result)
                                self.memory.add("tool", result)
                            elif name == "get_fred_data":
                                try:
                                    args = json.loads(tc["arguments"] or "{}")
                                    series = str(args.get("series", "")).strip()
                                    lookback = str(args.get("lookback", "1y")).strip()
                                except:
                                    series = ""
                                    lookback = "1y"
                                result = fetch_fred_data(series, lookback)
                                self.conversation.add_tool_response(tc["id"] or f"call_fred_{series}", name, result)
                                self.memory.add("tool", result)
                            elif name == "python_repl":
                                try:
                                    args = json.loads(tc["arguments"] or "{}")
                                    code = args.get("code", "")
                                    timeout = int(args.get("timeout", 10))
                                    max_length = int(args.get("max_length", 5000))
                                    timeout = min(max(1, timeout), 30)
                                    max_length = min(max_length, 10000)
                                except:
                                    code = ""
                                    timeout = 10
                                    max_length = 5000
                                repl_result = python_repl(code, timeout, max_length)
                                if len(repl_result) > max_length:
                                    repl_result = repl_result[:max_length] + f"\n...[truncated to {max_length} chars]..."
                                result = f"Python REPL output for code `{code[:200]}`:\n{repl_result[:max_length]}"
                                self.conversation.add_tool_response(tc["id"] or f"call_python_{abs(hash(code)) % 10000}", name, result)
                                self.memory.add("tool", result)
                    finally:
                        self.conversation.suppress_trim = False
                    # Re-issue to synthesize tool data
                    continue

                # No tool calls - finalize with memory compaction
                elapsed = max(1, int(round(time.time() - start)))
                verb = random.choice(self.config.verbs)
                self.console.print(f"[dim italic]* {verb} for {elapsed}s[/dim italic]\n")
                self.conversation.add_assistant(full_text)
                self.memory.add("assistant", full_text)
                if "PROPOSED TRADE:" in full_text:
                    self.memory.upsert_fact("last_trade", full_text[:1000], "trade")
                if self.memory.total_tokens() > self.memory.threshold:
                    self.console.print("[dim]Compacting memory into summary state...[/dim]")
                    self.memory.compact(self.client)
                trade = parse_trade_proposal(full_text)
                if trade:
                    self.pending_trade = trade
                    self.memory.upsert_fact("pending_trade", json.dumps({"symbol": trade.symbol, "side": trade.side, "price": trade.price}), "trade")
                return

            except Exception as e:
                err_str = str(e)
                # Handle Stealth provider 400 - often due to tool output too large or malformed
                if "400" in err_str and ("Stealth" in err_str or "Provider returned error" in err_str):
                    self.ui.show_error(f"Stealth provider hiccup (400) - Mommy will retry without tools, sweetheart: {e}")
                    # Fallback: try without tools, with truncated history
                    try:
                        with self.ui.show_thinking():
                            # Use compacted context without tools
                            context = self.memory.get_context(self.system_prompt, self.conversation.history[-4:])
                            # Remove tool-related messages for fallback
                            fallback_context = [m for m in context if m.get("role") != "tool"]
                            stream = self.client.client.chat.completions.create(
                                model=self.config.model,
                                messages=fallback_context,
                                stream=True,
                                temperature=0.75,
                                max_tokens=2048,
                            )
                            full_text = ""
                            for chunk in stream:
                                if chunk.choices and getattr(chunk.choices[0].delta, "content", None):
                                    content = chunk.choices[0].delta.content
                                    full_text += content
                            cleaned = clean_markdown_tables(full_text)
                            print_cleaned_response(self.console, self.config.character_name, "\n" + cleaned)
                            self.conversation.add_assistant(cleaned)
                            self.memory.add("assistant", cleaned)
                            return
                    except Exception as e2:
                        self.ui.show_error(f"Fallback also failed: {e2}")
                self.ui.show_error(f"Stream error: {e}")
                return

def main():
    parser = argparse.ArgumentParser(description="📈 Mommy Quant CLI (Python + OpenRouter)")
    parser.add_argument("-m", "--model", default=Config.model, help="OpenRouter model")
    parser.add_argument("-T", "--max-turns", type=int, default=Config.max_history_turns, help="Max turns")
    parser.add_argument("-k", "--api-key", default=os.environ.get("OPENROUTER_API_KEY", ""), help="OpenRouter API key (or env OPENROUTER_API_KEY)")
    parser.add_argument("--no-telemetry", action="store_true", help="Disable telemetry")
    parser.add_argument("--fred-key", default=os.environ.get("FRED_API_KEY", ""), help="FRED API key for macro data (or env FRED_API_KEY)")
    args = parser.parse_args()

    global _FRED_API_KEY
    if args.fred_key:
        _FRED_API_KEY = args.fred_key

    app = MommyQuantApp(args)
    app.run()

if __name__ == "__main__":
    main()
