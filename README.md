# Mommy Quant CLI

A terminal-based AI quant assistant with a maternal Wall Street persona. Streams responses from OpenRouter, pulls live market telemetry, and lets the LLM call native tools (quotes, history, correlation, web search, Python REPL, FRED macro data) to answer with real data instead of guesses.

## Features

- **Live market telemetry** — SPX, VIX, gold spot, crypto volatility screen (CoinGecko/Bybit), Hyperliquid perps
- **Native tool calling** — the model autonomously calls tools in a loop (up to 5 iterations) and synthesizes results
- **Any asset, worldwide** — stocks, ETFs, indices, FX, futures, crypto via Yahoo Finance with TradingView fallback
- **Python REPL tool** — the model writes and executes real Python (numpy/pandas/ta) for deterministic math and technical indicators
- **Web browsing** — DuckDuckGo search + curl-style URL fetching for news and research
- **FRED macro data** — Treasury yields, yield curve, Fed funds/liquidity, CPI/PCE, DXY (needs free API key)
- **Skills** — on-demand domain packs: `technical_analysis`, `portfolio_risk`, `macro_fred`, `deep_research`
- **Autonomous planning** — the model can break complex tasks into steps and track progress
- **Persistent memory** — SQLite conversation log with automatic LLM compaction into summaries + key facts
- **Workspace** — `~/mommy_workspace` for cached data, backtests, model weights (parquet/CSV), KV store
- **Slash commands** — `/eli5`, `/quant`, `/audit`, `/bull`, `/bear` one-shot pipelines
- **Rich streaming output** — token-by-token prose, markdown tables detected, cleaned and rendered properly
- **Simulated trade proposals** — the model can propose trades (`EXECUTE` confirms; simulation only)

## Requirements

- Python 3.9+ (tested on Termux/Android and Linux)
- An [OpenRouter](https://openrouter.ai) API key
- Optional: [FRED](https://fred.stlouisfed.org/docs/api/api_key.html) API key for macro data

## Installation

```bash
pip install openai rich httpx
export OPENROUTER_API_KEY="sk-or-..."
```

Optional extras used by the Python REPL tool when available:

```bash
pip install numpy pandas ta          # technical analysis libs
export FRED_API_KEY="..."            # macro data
```

## Usage

```bash
python quant.py                                  # default model
python quant.py -m anthropic/claude-sonnet-4     # custom OpenRouter model
python quant.py --no-telemetry                   # offline / no market data at startup
python quant.py -T 20                            # more conversation history turns
python quant.py --fred-key ...                   # FRED key without env var
```

### Chat examples

```
You > is NVDA up today?
You > how correlated are BTC-USD and TSLA over 3 months?
You > compute RSI(14) and MACD for ETH-USD on daily closes
You > what's the yield curve saying right now?
```

### Slash commands

| Command | Description |
|---|---|
| `/quant <ticker>` | No pleasantries — structured metrics table (price, RSI, vol, P/E, beta) |
| `/audit <ticker>` | Full agentic pipeline: price + news sentiment + technicals + verdict |
| `/bull <ticker>` | Strongest honest bullish thesis with catalysts |
| `/bear <ticker>` | Devil's advocate bearish risk analysis |
| `/eli5 <topic>` | Explain any finance concept with zero jargon |
| `EXECUTE` | Confirm the pending simulated trade proposal (expires after 120s) |
| `/reset` | Wipe session memory/context instantly |
| `help` | Show all commands |

## Architecture

```
quant.py (~2200 lines, single file)
├── LiveStreamer          # streams prose live; buffers/cleans/renders markdown tables
├── Config                # persona, system prompt, defaults
├── Telemetry             # Yahoo/TradingView quotes, gold, CoinGecko/Bybit, Hyperliquid
├── Tools                 # get_stock_price, get_correlation, get_history_data,
│                         # duckduckgo_search, curl/fetch_url, python_repl, get_fred_data
├── SKILL_REGISTRY        # loadable domain packs (technical_analysis, portfolio_risk, ...)
├── ConversationManager   # message history with tool-batch-safe trimming
├── Workspace             # ~/mommy_workspace: data/backtests/models + SQLite TTL cache
├── SQLiteMemory          # persistent log -> LLM compaction -> summary + KV facts
└── MommyQuantApp         # main loop: tool-calling iterations, slash commands, trades
```

Data flow per turn:

1. User input is added to history and persisted.
2. The request streams from OpenRouter with tool declarations attached.
3. If the model emits `tool_calls`, each is executed locally, results are appended as `tool` messages, and the loop re-issues (max 5 attempts; the last drops tools to force a final answer).
4. Prose streams live to the terminal; tables are buffered, repaired, and rendered via Rich.

All keys are read from environment variables or CLI flags — **nothing sensitive is hardcoded or persisted**. Trade execution is simulated only; no orders are ever signed or sent.

## Notes

- Designed with Termux/Android in mind: device timezone is detected via `getprop`, output truncation guards against context overflow, and the Python REPL runs in a timeout-guarded thread.
- Telemetry failures degrade gracefully to fallback strings.
