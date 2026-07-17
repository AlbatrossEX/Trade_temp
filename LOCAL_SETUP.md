# Running the LEAN Engine Locally — Setup & Backtest Tutorial

How to backtest QuantConnect algorithms (like `Volitility.py`) on this machine, inside
**WSL Ubuntu**, with **no QuantConnect account required**.

---

## TL;DR — it's already set up

Everything below is installed and verified working. To run a backtest right now:

```bash
wsl                                          # from PowerShell
source ~/quantconnect/.venv/bin/activate     # activate the venv
cd ~/quantconnect/workspace
lean backtest "Volatility"
```

Read on for what each piece is, how it was built (so you can rebuild it), and the
one real gotcha: **data**.

---

## 1. What you need, and why

| Piece | What it does | Status on this machine |
|---|---|---|
| **WSL Ubuntu** | Linux environment; Docker + Lean run here, not on Windows | Ubuntu 24.04 (WSL2) ✔ |
| **Docker** | Runs the LEAN engine container | Installed, daemon running ✔ |
| **Python venv** | Isolated env holding the Lean CLI | `~/quantconnect/.venv` ✔ |
| **Lean CLI** | The `lean` command; orchestrates Docker for you | v1.0.227 ✔ |
| **LEAN engine image** | The actual backtest engine (C#), as a Docker image | `quantconnect/lean:latest`, 42.5 GB ✔ |
| **Workspace** | Holds `lean.json` + `data/` + one folder per algorithm | `~/quantconnect/workspace` ✔ |
| **Market data** | Price bars the engine reads | Binance minute/hour/daily for BTC, ETH, SUI, TAO — **see §5** |

You do **not** need a QuantConnect login for any of this. See §6.

---

## 2. How it was installed (to reproduce elsewhere)

```bash
# 1. Create the venv and install the Lean CLI
mkdir -p ~/quantconnect
python3 -m venv ~/quantconnect/.venv
~/quantconnect/.venv/bin/pip install --upgrade pip
~/quantconnect/.venv/bin/pip install lean

# 2. Pull the LEAN engine image (~42 GB — takes a while; re-run if it times out,
#    Docker resumes from completed layers)
docker pull quantconnect/lean:latest

# 3. Build the workspace WITHOUT `lean init` (which demands a paid account — see §6).
#    Sparse-clone the public Lean repo for its Data folder + config template:
cd ~/quantconnect
git clone --depth 1 --filter=blob:none --sparse https://github.com/QuantConnect/Lean.git lean-src
cd lean-src && git sparse-checkout set Data Launcher

# 4. Assemble the workspace
mkdir -p ~/quantconnect/workspace && cd ~/quantconnect/workspace
cp -r ~/quantconnect/lean-src/Data data
sed 's|"data-folder": "../../../Data/"|"data-folder": "data"|' \
    ~/quantconnect/lean-src/Launcher/config.json > lean.json
```

That's it — `lean.json` + `data/` is exactly what `lean init` would have produced.
The CLI finds `lean.json` by searching upward from your current directory, so always
run `lean` commands from inside `~/quantconnect/workspace`.

---

## 3. Daily workflow

### Open the environment

```bash
wsl                                          # from PowerShell / or open the Ubuntu app
source ~/quantconnect/.venv/bin/activate     # prompt shows (.venv)
cd ~/quantconnect/workspace
```

Optional one-time shortcut — then just type `qc`:

```bash
echo "alias qc='source ~/quantconnect/.venv/bin/activate && cd ~/quantconnect/workspace'" >> ~/.bashrc
```

### Create a project (already done for `Volatility`)

```bash
lean project-create --language python "MyStrategy"
```

Creates `MyStrategy/` with `main.py`, `research.ipynb`, `config.json`. The entry file
must be `main.py` and hold exactly one `QCAlgorithm` subclass.

### Update the algorithm code

Your Windows files are visible in WSL under `/mnt/c/...`. To sync your edits in:

```bash
cp "/mnt/c/D/SynologyDrive/CS-PROJECTS/Trade_temp/Volitility.py" \
   ~/quantconnect/workspace/Volatility/main.py
```

> `Volitility.py` in the Windows repo is the source of truth; `Volatility/main.py` is a
> copy. Re-run this after every edit, or you'll backtest stale code.

### Run the backtest

```bash
lean backtest "Volatility"
```

`Volitility.py` is parameterized — **switch coin or timescale without editing code**, via
`Volatility/config.json` `"parameters"` (all optional):

| Parameter | Values | Default |
|---|---|---|
| `symbol` | `BTCUSDT` `ETHUSDT` `SUIUSDT` `TAOUSDT` | `BTCUSDT` |
| `resolution` | `Second` `Minute` `Hour` `Daily` | `Minute` |
| `start` / `end` | `YYYYMMDD` | listing date / `20260715` |
| `lookback` `vol_threshold` | ints/floats | `300` / `0.8` |

```jsonc
// Volatility/config.json
"parameters": { "symbol": "ETHUSDT", "resolution": "Hour", "start": "20220101", "end": "20231231" }
```

`start` is auto-clamped to each coin's listing date, and volatility is annualized correctly
per resolution. Verified working: BTCUSDT/Minute (368 orders) and ETHUSDT/Hour (1,810
orders). Leave `parameters` empty to run the coin's **full** history (heavy for BTC/ETH at
minute resolution — ~4.7 M bars).

First run is slower (container start). Results go to
`Volatility/backtests/<timestamp>/`:

| File | Contents |
|---|---|
| `*-summary.json` | Statistics: Sharpe, drawdown, net profit, fees, total orders |
| `*.json` | Full result incl. equity curve and charts |
| `log.txt` | Engine trace + your `self.Debug()` / `self.Log()` output |
| `succeeded-data-requests-*.txt` | Data files the engine **found** |
| `failed-data-requests-*.txt` | Data files the engine **wanted but couldn't find** ← check this first when you get zero trades |

---

## 4. Reading a result

The fastest health check:

```bash
grep -E "DATA USAGE" Volatility/backtests/<timestamp>/log.txt
```

A healthy run shows `Failed data requests 0%`. If failures are high, the engine had no
prices to trade on and any "0 orders / $0.00 profit" result is meaningless — it's a data
problem, not a strategy result.

---

## 5. Data — the Binance minute database

Real Binance **spot** data is now installed under
`data/crypto/binance/`, sourced from Binance's free public archive
(`data.binance.vision`) and converted to LEAN format by
[`binance_to_lean.py`](binance_to_lean.py):

| Coin | LEAN symbol | Resolutions | Coverage |
|---|---|---|---|
| Bitcoin | `BTCUSDT` | minute · hour · daily | 2017-08-17 → present |
| Ethereum | `ETHUSDT` | minute · hour · daily | 2017-08-17 → present |
| SUI | `SUIUSDT` | minute · hour · daily | 2023-05-03 → present |
| TAO (Bittensor) | `TAOUSDT` | minute · hour · daily | 2024-04-11 → present |

Layout (matches LEAN exactly):

```
data/crypto/binance/minute/<sym>/YYYYMMDD_trade.zip   # one zip per day
        inner csv: YYYYMMDD_<sym>_minute_trade.csv
        rows:      ms_since_midnight,open,high,low,close,volume
data/crypto/binance/hour/<sym>_trade.zip              # one file, whole history
data/crypto/binance/daily/<sym>_trade.zip             # one file, whole history
        inner csv: <sym>.csv
        rows:      YYYYMMDD HH:MM,open,high,low,close,volume
```

### HYPE is not available from Binance

Hyperliquid's **HYPE** is **not listed on Binance.com spot** — no HYPEUSDT/USDC/BTC/FDUSD
klines exist in the public archive. LEAN's DB lists it only under `binanceus`
(Binance.US), which has no bulk-data archive. To backtest HYPE you'd source it from
another venue (Hyperliquid API, Coinbase, or Kraken) and write it into
`data/crypto/<market>/…` in the same format.

### Refreshing / adding data

The downloader is **idempotent and resumable** (it caches raw Binance zips under
`~/binance_cache`). Re-run any time to pull the latest days, or add a coin:

```bash
python3 ~/binance_to_lean.py                    # refresh the default 4 coins
python3 ~/binance_to_lean.py DOGEUSDT LINKUSDT  # add any other Binance spot pair
```

> Adding a *new* pair also needs a row in
> `data/symbol-properties/symbol-properties-database.csv` under the `binance` market
> (BTC/ETH/SUI/TAO are already present). Copy an existing `binance,…USDT,…` line and
> edit the ticker/precision.

### One expected wrinkle: "Failed data requests ≈ 50%"

LEAN auto-requests bid/ask **quote** bars in addition to trade bars, but Binance's free
klines are **trade-only** — there is no bid/ask. The engine logs those quote misses as
"failed data requests" and transparently falls back to trade bars, so **trading is
unaffected**. A run with real orders and a non-zero P&L is working correctly even when
the failed-request percentage looks high; the misses are all `*_quote.zip`, never the
`*_trade.zip` you actually trade on.

---

## 6. The "paid organization" error

Running `lean init` fails with:

> To request an access token, you must belong to a paid organization.

**Cause:** Lean CLI v1.0.227 forces a QuantConnect login at the top of `init`, and
QuantConnect now gates API tokens behind a paid tier.

**Why it doesn't matter:** the LEAN engine is open-source and free. `lean init` only
downloads the repo's `Data/` folder and config — which §2 does by hand. Confirmed in the
CLI source that `lean backtest` never validates credentials (it calls
`try_get_working_organization_id()`, which returns `None` harmlessly).

**Verified working with no account:** `lean project-create`, `lean backtest`,
`lean research`. Login/paid tier is only needed for QuantConnect **cloud** backtests and
their premium datasets.

---

## 7. Extras

```bash
lean research "Volatility"       # Jupyter Lab at localhost:8888 with the LEAN API
lean backtest "Volatility" --debug pycharm   # attach a debugger
pip install --upgrade lean       # update CLI (venv active)
docker pull quantconnect/lean:latest         # update engine
```

Paper/live trading (`lean live`) needs broker credentials and is out of scope here.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `lean: command not found` | Activate the venv: `source ~/quantconnect/.venv/bin/activate` |
| `'lean.json' not found` | You're outside the workspace — `cd ~/quantconnect/workspace` |
| `To request an access token...` | You ran `lean init`. Don't — see §6; the workspace already exists. |
| Zero orders / $0.00 profit | Check `failed-data-requests-*.txt` for missing `*_trade.zip`; confirm `symbol`/dates fall inside the coverage in §5 |
| "Failed data requests ≈ 50%" | Expected — those are trade-only quote-bar misses; harmless (§5). Only worry if `*_trade.zip` files are failing |
| Docker connect errors | `docker ps` to check the daemon; if using Docker Desktop, enable WSL integration for Ubuntu |
| Network timeout on image pull | Just re-run `docker pull`; it resumes |
| Edits not taking effect | You edited the Windows file but didn't copy it to `Volatility/main.py` (§3) |
