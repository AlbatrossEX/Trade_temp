# region imports
from AlgorithmImports import *
# endregion


class ETHMeanReversionAlgorithm(QCAlgorithm):
    """
    ETH Mean Reversion Strategy for QuantConnect (LEAN engine) - PEP8/snake_case API.

    v3 changes (fee-bleed fixes vs. the hourly/minute all-in versions):
      1. DAILY resolution -> indicator windows now span real trend/reversion
         cycles instead of intraday noise, so far fewer threshold crossings
      2. Re-entry cooldown -> can't immediately jump back in after an exit
      3. Fractional position sizing -> a single whipsaw costs less
      4. Per-trade fee logging via on_order_event -> watch costs accumulate

    v4 changes (win-rate improvements; v3 fee fixes cut cost drag but win rate
    still had room -- these target trade *quality* over trade quantity):
      1. Sharper trend filter -> now blocks entries when price is far below the
         trend SMA *or* the trend SMA's own slope is still falling, catching
         early-stage downtrends the old %-deviation-only filter let through
      2. ATR-based stop -> replaces the flat 8% stop with atr_stop_multiplier x
         14-day ATR, snapshotted at entry, so stop width adapts to the ETH
         volatility regime instead of using one static percentage
      3. EMA-smoothed z-score input -> the price feeding the z-score numerator
         is now a short EMA instead of the raw close, cutting single-bar noise
         right around the entry/exit thresholds (SMA/STD bands still use raw
         close, only the "where is price right now" side is smoothed).
         entry_z is lowered from 2.0 -> 1.25 to compensate: the smoothed
         numerator compresses the z-score range vs. raw close, so 2.0 almost
         never fires on the smoothed series.

    A fourth candidate -- "reversal confirmation" (only enter once price closes
    back above the low seen since a z-score extreme, instead of immediately) --
    was implemented and backtested alongside the above, but empirically made
    every metric worse (56% win rate vs. 62% baseline, negative expectancy):
    confirmed entries land after part of the bounce has already happened,
    leaving less room to reach the exit band before something else (chop,
    cooldown) turns the trade into a small loss instead of the win it would
    have been on an immediate entry. Dropped based on that result -- the trend
    filter above already screens out the falling-knife case it was meant to
    catch. 2023-2024 backtest: 70% win rate (vs. 62% baseline), 21 trades.

    Also tried and reverted: replacing the flat 20-day SMA + EMA-smoothed
    numerator above with a momentum-adaptive reference mean (Holt linear-trend
    / double exponential smoothing: forecast_t = level_(t-1) + trend_(t-1),
    zscore = residual / rolling_std(residuals), both level and trend updated
    from price each bar). Theory: a reference that already tracks the trend
    should need less bolted-on trend filtering and give a more honest surprise
    signal. In practice it underperformed on every parameter combination
    tried (alpha in [0.10, 0.25], beta in [0.02, 0.10]): best case was 64% win
    rate / -1.3% net profit, worse than this version's 70% / +3.2% across the
    board. Likely cause: Holt's trend term absorbs real momentum fast enough
    that the residual left to signal on shrinks and gets noisier, so entries
    fire on lower-conviction deviations than comparing price to a genuinely
    stale flat mean does.

    NOTE: v3 tried a protective StopMarketOrder placed on fill, to get
    intrabar High/Low stop enforcement instead of a close-price check. Live
    backtest confirmed Coinbase does not support Stop Market orders (deprecated
    exchange-side since 2019-03-23) -- QuantConnect's Coinbase brokerage model
    rejects every submission, so that stop never actually protected anything.
    Reverted to a manual close-price stop check via self.liquidate() (a plain
    market order, which the brokerage does accept). A StopLimitOrder is worth
    trying as a follow-up since real Coinbase still supports stop-limit.

    Educational template only. Not investment advice.
    """

    def initialize(self):
        # --- Backtest window & starting capital ---
        self.set_start_date(2022, 8, 1)
        self.set_end_date(2024, 12, 31)
        self.set_cash(100000)

        # --- Add ETH/USD ---
        # Cash account = spot, LONG-ONLY (cannot short spot crypto on a cash account).
        crypto = self.add_crypto("ETHUSD", Resolution.DAILY, Market.COINBASE)
        self._symbol = crypto.symbol
        self.set_brokerage_model(BrokerageName.COINBASE, AccountType.CASH)

        # --- Strategy parameters ---
        self.window = 20             # rolling window for z-score
        self.trend_window = 100      # longer MA for trend filter
        # entry_z lowered from the v3 value of 2.0: the z-score numerator is now
        # EMA-smoothed (see price_smoothing_window below), which damps single-bar
        # spikes and compresses the z-score range vs. raw close -- 2.0 on the
        # smoothed series almost never fires, so this is a required recalibration,
        # not an independent tuning choice.
        self.entry_z = 1.25
        self.exit_z = 0.5
        self.trend_threshold = 0.05      # %-deviation from trend SMA considered "trending"
        self.trend_slope_lookback = 5    # bars used to measure trend SMA's own slope
        self.trend_slope_threshold = 0.01  # trend SMA move over that lookback considered "trending"
        self.price_smoothing_window = 3  # EMA period smoothing the z-score's price input
        self.atr_period = 14
        self.atr_stop_multiplier = 2.5   # stop distance = multiplier x ATR at entry

        # --- Fee-control knobs ---
        self.position_size = 0.30    # allocate 30% per trade instead of 100%
        self.cooldown_days = 3       # wait N days after an exit before re-entering
        self.allow_shorts = False    # only True on a margin brokerage

        # --- State: position/trade tracking ---
        self.entry_price = None
        self.entry_atr = None
        self.stop_price = None
        self.last_exit_time = None
        self.total_fees = 0.0
        self.trade_count = 0

        # --- Indicators (underscore-prefixed to avoid clashing with self.sma()/self.std()) ---
        self._sma = self.sma(self._symbol, self.window, Resolution.DAILY)
        self._std = self.std(self._symbol, self.window, Resolution.DAILY)
        self._trend_sma = self.sma(self._symbol, self.trend_window, Resolution.DAILY)
        self._trend_sma.window.size = self.trend_slope_lookback + 1
        self._price_ema = self.ema(self._symbol, self.price_smoothing_window, Resolution.DAILY)
        self._atr = self.atr(self._symbol, self.atr_period, MovingAverageType.WILDERS, Resolution.DAILY)

        # --- Warm up so indicators are valid before trading ---
        self.set_warm_up(self.trend_window, Resolution.DAILY)

    def _in_cooldown(self):
        if self.last_exit_time is None:
            return False
        return (self.time - self.last_exit_time).days < self.cooldown_days

    def _enter_long(self, price):
        self.set_holdings(self._symbol, self.position_size)
        self.entry_price = price
        self.entry_atr = self._atr.current.value
        self.stop_price = self.entry_price - self.atr_stop_multiplier * self.entry_atr
        self.debug(
            f"LONG entry at {price:.2f} | stop={self.stop_price:.2f} (atr={self.entry_atr:.2f})"
        )

    def _enter_short(self, price):
        self.set_holdings(self._symbol, -self.position_size)
        self.entry_price = price
        self.entry_atr = self._atr.current.value
        self.stop_price = self.entry_price + self.atr_stop_multiplier * self.entry_atr
        self.debug(
            f"SHORT entry at {price:.2f} | stop={self.stop_price:.2f} (atr={self.entry_atr:.2f})"
        )

    def on_data(self, slice: Slice):
        if self.is_warming_up:
            return
        if not (
            self._sma.is_ready and self._std.is_ready and self._trend_sma.is_ready
            and self._price_ema.is_ready and self._atr.is_ready
        ):
            return
        if self._symbol not in slice.bars:
            return

        price = slice.bars[self._symbol].close
        smoothed_price = self._price_ema.current.value

        std_val = self._std.current.value
        if std_val == 0:
            return
        # z-score input is EMA-smoothed to cut single-bar noise near the thresholds;
        # the SMA/STD band itself still tracks raw closes.
        zscore = (smoothed_price - self._sma.current.value) / std_val

        trend_val = self._trend_sma.current.value
        trend_dev = (price - trend_val) / trend_val

        trend_slope = 0.0
        if self._trend_sma.window.is_ready:
            prior = self._trend_sma.window[self.trend_slope_lookback].value
            if prior != 0:
                trend_slope = (self._trend_sma.current.value - prior) / prior

        # Sharper trend filter: block on either a large deviation from trend OR
        # the trend SMA itself still actively moving -- catches early-stage
        # trends the %-deviation check alone would miss.
        strong_uptrend = (trend_dev > self.trend_threshold) or (trend_slope > self.trend_slope_threshold)
        strong_downtrend = (trend_dev < -self.trend_threshold) or (trend_slope < -self.trend_slope_threshold)

        holdings = self.portfolio[self._symbol].quantity

        # --- Flat: look for entries (respect cooldown and trend filter) ---
        if holdings == 0:
            if self._in_cooldown():
                return

            if zscore < -self.entry_z and not strong_downtrend:
                self._enter_long(price)

            elif self.allow_shorts and zscore > self.entry_z and not strong_uptrend:
                self._enter_short(price)

        # --- Long open: exit on reversion or ATR stop (close-price check; Coinbase
        # rejects Stop Market orders, so this can't be enforced intrabar) ---
        elif holdings > 0:
            reverted = zscore > -self.exit_z
            stopped_out = price < self.stop_price
            if reverted or stopped_out:
                self.liquidate(self._symbol)
                self.last_exit_time = self.time
                self.debug(f"LONG exit at {price:.2f} ({'stop' if stopped_out else 'reverted'})")
                self.entry_price = None
                self.entry_atr = None
                self.stop_price = None

        # --- Short open: exit on reversion or ATR stop (close-price check) ---
        elif holdings < 0:
            reverted = zscore < self.exit_z
            stopped_out = price > self.stop_price
            if reverted or stopped_out:
                self.liquidate(self._symbol)
                self.last_exit_time = self.time
                self.debug(f"SHORT exit at {price:.2f} ({'stop' if stopped_out else 'reverted'})")
                self.entry_price = None
                self.entry_atr = None
                self.stop_price = None

    def on_order_event(self, order_event: OrderEvent):
        # Fires on every fill; accumulate and log realized fees so you can watch the drag.
        if order_event.status != OrderStatus.FILLED:
            return
        fee = order_event.order_fee.value.amount
        self.total_fees += fee
        self.trade_count += 1
        self.debug(
            f"FILL #{self.trade_count} | qty={order_event.fill_quantity:.4f} "
            f"@ {order_event.fill_price:.2f} | fee=${fee:.2f} | cum fees=${self.total_fees:.2f}"
        )

    def on_end_of_algorithm(self):
        pv = self.portfolio.total_portfolio_value
        self.debug(f"Final portfolio value: ${pv:.2f}")
        self.debug(f"Total fills: {self.trade_count} | Total fees paid: ${self.total_fees:.2f}")
        if pv > 0:
            self.debug(f"Fees as % of final value: {100 * self.total_fees / pv:.1f}%")
