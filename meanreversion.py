# region imports
from AlgorithmImports import *
# endregion


class ETHMeanReversionAlgorithm(QCAlgorithm):
    """
    ETH Mean Reversion Strategy for QuantConnect (LEAN engine) - PEP8/snake_case API.

    v2 changes (fee-bleed fixes vs. the hourly all-in version):
      1. MINUTE resolution instead of hourly -> far fewer threshold crossings
      2. Re-entry cooldown -> can't immediately jump back in after an exit
      3. Fractional position sizing -> a single whipsaw costs less
      4. Per-trade fee logging via on_order_event -> watch costs accumulate

    Educational template only. Not investment advice.
    """

    def initialize(self):
        # --- Backtest window & starting capital ---
        self.set_start_date(2023, 1, 1)
        self.set_end_date(2024, 12, 31)
        self.set_cash(100000)

        # --- Add ETH/USD ---
        # Cash account = spot, LONG-ONLY (cannot short spot crypto on a cash account).
        crypto = self.add_crypto("ETHUSD", Resolution.MINUTE, Market.COINBASE)
        self._symbol = crypto.symbol
        self.set_brokerage_model(BrokerageName.COINBASE, AccountType.CASH)

        # --- Strategy parameters ---
        self.window = 20             # rolling window for z-score
        self.trend_window = 100      # longer MA for trend filter
        self.entry_z = 2.0
        self.exit_z = 0.5
        self.trend_threshold = 0.05  # 5% deviation = "strong trend"
        self.stop_loss_pct = 0.08    # a bit wider on MINUTE bars

        # --- Fee-control knobs ---
        self.position_size = 0.30    # allocate 30% per trade instead of 100%
        self.cooldown_days = 3       # wait N days after an exit before re-entering
        self.allow_shorts = False    # only True on a margin brokerage

        # --- State ---
        self.entry_price = None
        self.last_exit_time = None
        self.total_fees = 0.0
        self.trade_count = 0

        # --- Indicators (underscore-prefixed to avoid clashing with self.sma()/self.std()) ---
        self._sma = self.sma(self._symbol, self.window, Resolution.MINUTE)
        self._std = self.std(self._symbol, self.window, Resolution.MINUTE)
        self._trend_sma = self.sma(self._symbol, self.trend_window, Resolution.MINUTE)

        # --- Warm up so indicators are valid before trading ---
        self.set_warm_up(self.trend_window, Resolution.MINUTE)

    def _in_cooldown(self):
        if self.last_exit_time is None:
            return False
        return (self.time - self.last_exit_time).days < self.cooldown_days

    def on_data(self, slice: Slice):
        if self.is_warming_up:
            return
        if not (self._sma.is_ready and self._std.is_ready and self._trend_sma.is_ready):
            return
        if self._symbol not in slice.bars:
            return

        price = slice.bars[self._symbol].close

        std_val = self._std.current.value
        if std_val == 0:
            return
        zscore = (price - self._sma.current.value) / std_val

        trend_val = self._trend_sma.current.value
        trend_dev = (price - trend_val) / trend_val
        strong_uptrend = trend_dev > self.trend_threshold
        strong_downtrend = trend_dev < -self.trend_threshold

        holdings = self.portfolio[self._symbol].quantity

        # --- Flat: look for entries (respect cooldown) ---
        if holdings == 0:
            if self._in_cooldown():
                return

            if zscore < -self.entry_z and not strong_downtrend:
                self.set_holdings(self._symbol, self.position_size)
                self.entry_price = price
                self.debug(f"LONG entry at {price:.2f}, z={zscore:.2f}")

            elif self.allow_shorts and zscore > self.entry_z and not strong_uptrend:
                self.set_holdings(self._symbol, -self.position_size)
                self.entry_price = price
                self.debug(f"SHORT entry at {price:.2f}, z={zscore:.2f}")

        # --- Long open: exit on reversion or stop-loss ---
        elif holdings > 0:
            reverted = zscore > -self.exit_z
            stopped_out = price < self.entry_price * (1 - self.stop_loss_pct)
            if reverted or stopped_out:
                self.liquidate(self._symbol)
                self.last_exit_time = self.time
                self.debug(f"LONG exit at {price:.2f} ({'stop' if stopped_out else 'reverted'})")
                self.entry_price = None

        # --- Short open: exit on reversion or stop-loss ---
        elif holdings < 0:
            reverted = zscore < self.exit_z
            stopped_out = price > self.entry_price * (1 + self.stop_loss_pct)
            if reverted or stopped_out:
                self.liquidate(self._symbol)
                self.last_exit_time = self.time
                self.debug(f"SHORT exit at {price:.2f} ({'stop' if stopped_out else 'reverted'})")
                self.entry_price = None

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
