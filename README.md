# Mommy Quant CLI

Your warm, well-heeled AI quant assistant — streaming real market data, live Python math, and a healthy dose of maternal Wall Street energy straight into your terminal. Think of me as the portfolio manager who brings you tea, checks your risk, and won't let you yolo into crapcoins.

I stream responses from OpenRouter, pull live market telemetry, and — most importantly — call my own native tools so I never guess when I can *know*: live quotes, historical data, correlation matrices, web research, a full Python REPL, and FRED macro series.

## What I Bring To The Table 🍼📈

- **Market mama's radar** — SPX, VIX, gold spot, crypto volatility screen (CoinGecko/Bybit), Hyperliquid perps
- **Tool use, no training wheels** — I autonomously call tools in a loop (up to 5 rounds) and synthesize real data into answers
- **Global coverage** — stocks, ETFs, indices, FX, futures, crypto via Yahoo Finance, with TradingView as backup
- **Python, my dear** — I write and execute real Python (numpy/pandas/ta) for deterministic math and technical indicators. No hand-waving.
- **News & research mode** — DuckDuckGo search + URL fetching for the latest headlines and deep dives
- **Macro intelligence** — Treasury yields, yield curve, Fed liquidity, CPI/PCE, DXY via FRED (free API key required)
- **Skills on demand** — swap in domain packs: `technical_analysis`, `portfolio_risk`, `macro_fred`, `deep_research`
- **Self-planning** — I break complex tasks into steps and track progress without losing focus
- **I remember** — SQLite conversation log with automatic LLM compaction into summaries + key facts
- **My workspace** — `~/mommy_workspace` for cached data, backtests, model weights (parquet/CSV), KV store
- **Sweet nothings** — `/eli5`, `/quant`, `/audit`, `/bull`, `/bear` one-shot pipelines
- **Live prose** — token-by-token streaming; I detect, clean, and render markdown tables properly via Rich
- **Paper trades only** — I can propose trades (`EXECUTE` confirms; simulation only — no actual orders)

## Getting Mommy Set Up 🛠️

- Python 3.9+ (runs on Termux/Android and Linux)
- An [OpenRouter](https://openrouter.ai) API key
- Optional: [FRED](https://fred.stlouisfed.org/docs/api/api_key.html) API key for macro data

```bash
pip install openai rich httpx
export OPENROUTER_API_KEY="sk-or-..."
```

Optional extras — installed when available for the Python REPL tool:

```bash
pip install numpy pandas ta
export FRED_API_KEY="..."
```

## Chatting With Me 💬

```bash
python quant.py                                   # default settings
python quant.py -m anthropic/claude-sonnet-4      # custom model
python quant.py --no-telemetry                    # no startup market data
python quant.py -T 20                             # more historical turns
python quant.py --fred-key ...                    # FRED key inline
```

Try one of these:

```
You > is NVDA up today?
You > how correlated are BTC-USD and TSLA over 3 months?
You > compute RSI(14) and MACD for ETH-USD on daily closes
You > what's the yield curve saying right now?
```

### Mama's Command Menu

| Command | What It Does |
|---|---|
| `/quant <ticker>` | No chit-chat — a structured metrics table (price, RSI, vol, P/E, beta) |
| `/audit <ticker>` | Full agentic deep-dive: price + news sentiment + technicals + verdict |
| `/bull <ticker>` | My strongest, most honest bullish thesis with real catalysts |
| `/bear <ticker>` | Devil's advocate — the real risks you can't afford to ignore |
| `/eli5 <topic>` | Any finance concept, zero jargon, maximum patience |
| `EXECUTE` | Confirm the pending simulated trade proposal (expires in 120s) |
| `/reset` | Wipe the session clean — fresh start, no hard feelings |
| `help` | Show me off |

## How My Mind Works 🧠

```
quant.py (~2200 lines, one file to rule them all)
├── LiveStreamer          # streams prose live; buffers/cleans/renders markdown tables
├── Config                # my personality, system prompt, defaults
├── Telemetry             # Yahoo/TradingView quotes, gold, crypto, Hyperliquid
├── Tools                 # get_stock_price, get_correlation, get_history_data,
│                         # duckduckgo_search, curl/fetch_url, python_repl, get_fred_data
├── SKILL_REGISTRY        # loadable domain packs (technical_analysis, portfolio_risk, ...)
├── ConversationManager   # message history with tool-batch-safe trimming
├── Workspace             # ~/mommy_workspace: data/backtests/models + SQLite TTL cache
├── SQLiteMemory          # persistent log -> LLM compaction -> summary + KV facts
└── MommyQuantApp         # main loop: tool-calling, slash commands, trade proposals
```

**A typical conversation turn with me:**

1. Your question lands in history and gets persisted.
2. I stream back from OpenRouter with my tool declarations attached.
3. If I decide I need data, I emit `tool_calls` — each one runs locally. Results come back as `tool` messages and I re-issue (up to 5 attempts).
4. On the last round, I drop the tools entirely to make sure I give you a final, synthesized answer.
5. All the while, my prose streams live to your terminal. Tables? I buffer them, repair what needs fixing, and render them clean with Rich.

**A few things to know:**
- All API keys are read from environment variables or CLI flags — **nothing sensitive is hardcoded or persisted**.
- Every trade I mention is **simulated only**. No orders are ever signed or sent. This is practice, not execution.
- I was built with Termux/Android in mind: I detect your device timezone via `getprop`, guard against context overflow with output truncation, and run my Python REPL in a timeout-guarded thread.
- Telemetry failures degrade gracefully — I'll tell you when data's missing rather than crashing.
