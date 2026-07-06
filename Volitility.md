# Volitility.py — Line-by-Line Explanation

This is a [QuantConnect](https://www.quantconnect.com/) (LEAN engine) algorithm called
`IntradayCryptoVolatility`. It trades BTC/USD on Coinbase, combining two signals:

1. **Volatility filter** — only trade while short-term (intraday) volatility is low.
2. **11-minute mean trend filter** — only go long when price is trending above its trailing
   11-minute mean by more than the round-trip trading fee, and exit when it drops back below.

It also enforces fee-aware constraints: a minimum price "edge" required to justify a trade, and
a minimum holding period between trades, both aimed at avoiding fee-eating churn.

```python
from AlgorithmImports import *
from datetime import timedelta
import numpy as np
```
- **Line 1**: Imports everything from QuantConnect's `AlgorithmImports` module — this pulls in
  the `QCAlgorithm` base class, `Resolution`, `BrokerageName`, `AccountType`, `RollingWindow`,
  and all other LEAN framework types used below.
- **Line 2**: Imports `timedelta`, used to express the minimum holding period as a duration.
- **Line 3**: Imports NumPy, used to compute the standard deviation of returns and the mean of
  recent prices.

```python
class IntradayCryptoVolatility(QCAlgorithm):
```
- Defines the algorithm class. Inheriting from `QCAlgorithm` gives it access to the LEAN
  engine's lifecycle hooks (`Initialize`, `OnData`), portfolio management, and order execution
  methods.

## `Initialize` — one-time setup before the backtest runs

```python
    def Initialize(self):
        self.SetStartDate(2020, 1, 1)
        self.SetEndDate(2023, 1, 31)
        self.SetCash("USD", 100000)
        self.SetBrokerageModel(BrokerageName.Coinbase, AccountType.Cash)
```
- Sets the backtest window (2020-01-01 to 2023-01-31), seeds the portfolio with $100,000 USD,
  and configures the brokerage simulation to match Coinbase's rules (fees, order types), using a
  `Cash` account (no margin/leverage — you can't short or borrow).

```python
        self.symbol = self.AddCrypto("BTCUSD", Resolution.Minute).Symbol
```
- Subscribes to BTC/USD price data at **minute resolution**. `AddCrypto` returns a `Security`
  object; `.Symbol` extracts its identifier, used in `OnData` and order calls.

```python
        self.lookback = 300
        self.returns_window = RollingWindow[float](self.lookback)
        self.last_price = None
```
- `self.lookback = 300`: the volatility calculation uses the last 300 one-minute returns.
- `self.returns_window`: a fixed-size FIFO buffer holding those returns — the oldest entry is
  dropped automatically once a new one is added past capacity.
- `self.last_price`: tracks the previous bar's close so a return can be computed on the next bar.

```python
        # 11-minute rolling mean dataset: maps a trailing mean price to every minute,
        # used below as a trend filter alongside the volatility filter.
        self.mean_lookback = 11
        self.price_window = RollingWindow[float](self.mean_lookback)
```
- `self.mean_lookback = 11`: size of the new short-term rolling window.
- `self.price_window`: a separate `RollingWindow` (distinct from `returns_window`) that holds
  raw **prices**, not returns. Every minute, this window gives a trailing "mean of the last 11
  minutes" data point — the dataset requested — which is later compared against the current
  price to detect a short-term trend.

```python
        self.SetWarmUp(self.lookback + 1)
```
- Warms up for 301 bars before trading starts, so `returns_window` (needs 301 prices for 300
  returns) is full before any decisions are made. This comfortably covers the 11-bar window too.

```python
        # 3. TRADING FEE CONSTRAINTS
        self.taker_fee = 0.006
        self.min_edge = 2 * self.taker_fee
        self.min_holding_period = timedelta(minutes=15)
        self.last_trade_time = None
```
- `self.taker_fee = 0.006`: an assumed 0.6% Coinbase taker fee per trade (adjust to your actual
  fee tier if known).
- `self.min_edge = 2 * self.taker_fee`: the round-trip cost (enter + exit) that a price move must
  exceed before a trade is considered worthwhile. This stops the strategy from opening/closing
  positions on moves too small to profit from after fees.
- `self.min_holding_period = timedelta(minutes=15)`: once a trade fires, no new trade is allowed
  for 15 minutes, regardless of signal flips — a second, independent brake on fee-churning
  behavior (rapid in/out trading).
- `self.last_trade_time`: tracks when the last trade happened, to enforce the holding period.

## `OnData` — called every time a new minute bar arrives

```python
    def OnData(self, data):
        if not data.ContainsKey(self.symbol) or data[self.symbol] is None:
            return
```
- Defensive check — if this bar's data slice doesn't contain BTC/USD data (e.g. a feed gap),
  exit early and wait for the next bar.

```python
        current_price = data[self.symbol].Close
```
- Reads the closing price of the current minute bar.

```python
        if self.last_price is not None:
            minute_return = (current_price - self.last_price) / self.last_price
            self.returns_window.Add(minute_return)

        self.last_price = current_price
        self.price_window.Add(current_price)
```
- Computes the minute-over-minute percentage return (skipped on the very first bar, since there
  is no prior price) and pushes it into `returns_window`.
- Updates `last_price` for the next bar's return calculation.
- Pushes the current price into `price_window` — this is what builds the "mean of the last 11
  minutes" dataset, one point per minute.

```python
        if self.IsWarmingUp or not self.returns_window.IsReady or not self.price_window.IsReady:
            return
```
- Guards against trading before there's enough data: still warming up, the 300-return window
  isn't full, or the 11-price window isn't full yet.

```python
        returns_list = list(self.returns_window)
        minute_volatility = np.std(returns_list)

        annualized_volatility = minute_volatility * np.sqrt(525600)
```
- Converts the rolling returns window to a list, computes its standard deviation (intraday,
  per-minute volatility), then annualizes it via the square-root-of-time rule
  (`sqrt(525,600)` — minutes in a 365-day year).

```python
        mean_price = np.mean(list(self.price_window))
        price_edge = (current_price - mean_price) / mean_price
```
- `mean_price`: the mean of the last 11 minutes of prices — the requested dataset, evaluated for
  the current minute.
- `price_edge`: how far (as a fraction) the current price sits above or below that 11-minute
  mean. A positive value means the price is trending up relative to its recent average; negative
  means it's trending down. This is the momentum/trend signal that "completes" the strategy.

```python
        volatility_threshold = 1.0

        can_trade = (
            self.last_trade_time is None
            or (self.Time - self.last_trade_time) >= self.min_holding_period
        )

        low_volatility = annualized_volatility < volatility_threshold
        trending_up = price_edge > self.min_edge
        trending_down = price_edge < -self.min_edge

        if not can_trade:
            return
```
- `volatility_threshold = 1.0`: 100% annualized volatility — the cutoff between "calm" and
  "turbulent" regimes.
- `can_trade`: true if no trade has happened yet, or if at least 15 minutes have passed since the
  last one (the fee-driven holding-period constraint).
- `low_volatility` / `trending_up` / `trending_down`: named boolean signals for readability.
  `trending_up`/`trending_down` only fire once the price deviation from the 11-minute mean
  exceeds the fee-derived `min_edge` — small, fee-unprofitable wiggles are ignored.
- If `can_trade` is false, the method returns immediately — no position change is made even if
  the signals would otherwise call for one.

```python
        if low_volatility and trending_up:
            if not self.Portfolio.Invested:
                self.SetHoldings(self.symbol, 1.0)
                self.last_trade_time = self.Time
        elif (not low_volatility) or trending_down:
            if self.Portfolio.Invested:
                self.Liquidate(self.symbol)
                self.last_trade_time = self.Time
```
- **Entry**: if volatility is low **and** price is trending up beyond the fee threshold, and the
  algorithm isn't already invested, allocate 100% of portfolio equity into BTC/USD and record the
  trade time.
- **Exit**: if volatility has risen above the threshold **or** the price has trended down beyond
  the fee threshold, and a position is currently held, liquidate it and record the trade time.
- Note the asymmetry: entry requires *both* low volatility and an uptrend; exit fires on
  *either* high volatility or a downtrend — the strategy is quick to de-risk but more selective
  about getting back in.

## Strategy summary

This is a **volatility-gated trend-following strategy with fee-aware trade throttling**:
- Stay in cash by default.
- Go long only when the market is calm (low annualized volatility) **and** price is trending
  above its 11-minute mean by more than the round-trip fee cost.
- Exit immediately if volatility spikes or the trend reverses meaningfully.
- Never trade more than once every 15 minutes, and never trade on a price move too small to
  cover fees.

## Assumptions worth double-checking

- **`self.taker_fee = 0.006`** is a placeholder Coinbase taker-fee estimate — replace it with
  your actual fee tier for accurate `min_edge` sizing.
- **`self.min_holding_period = 15 minutes`** and **`volatility_threshold = 1.0`** are illustrative
  values, not tuned/backtested — treat them as starting points.
- The 11-minute mean is computed on **price**, not returns, and used purely as a **trend filter**
  layered on top of the existing volatility filter (as opposed to, say, smoothing the return
  series itself). If the intended design was different (e.g. mean-reversion instead of
  trend-following), the sign of `trending_up`/`trending_down` would need to flip.
