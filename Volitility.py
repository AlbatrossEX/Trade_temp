from AlgorithmImports import *
from datetime import timedelta, datetime
import numpy as np


class IntradayCryptoVolatility(QCAlgorithm):
    """
    Intraday crypto volatility / trend strategy, backtestable against the local
    Binance minute database on ANY of the downloaded coins at ANY resolution.

    Configure via backtest parameters (Project -> Parameters, or lean.json
    "parameters"), all optional:
        symbol      e.g. BTCUSDT (default), ETHUSDT, SUIUSDT, TAOUSDT
        resolution  Minute (default), Hour, Daily, Second
        start       YYYYMMDD start date  (default: coin listing date)
        end         YYYYMMDD end date    (default: 20260715)

    Nothing needs to be edited to switch coin or timescale.
    """

    # First date each coin has Binance spot 1m data (used to clamp the start
    # so we don't warm up over empty history before the coin was listed).
    LISTING = {
        "BTCUSDT": datetime(2017, 8, 17),
        "ETHUSDT": datetime(2017, 8, 17),
        "SUIUSDT": datetime(2023, 5, 3),
        "TAOUSDT": datetime(2024, 4, 11),
    }

    # Bars per year for annualising volatility, by resolution.
    PERIODS_PER_YEAR = {
        Resolution.Second: 60 * 60 * 24 * 365,
        Resolution.Minute: 60 * 24 * 365,   # 525,600
        Resolution.Hour:   24 * 365,         # 8,760
        Resolution.Daily:  365,
    }

    RES_MAP = {
        "SECOND": Resolution.Second,
        "MINUTE": Resolution.Minute,
        "HOUR":   Resolution.Hour,
        "DAILY":  Resolution.Daily,
    }

    def Initialize(self):
        # ---- read parameters (all optional) ----
        ticker = (self.GetParameter("symbol") or "BTCUSDT").upper()
        res_name = (self.GetParameter("resolution") or "Minute").upper()
        resolution = self.RES_MAP.get(res_name, Resolution.Minute)

        listing = self.LISTING.get(ticker, datetime(2017, 8, 17))
        start = self._parse_date(self.GetParameter("start"), listing)
        if start < listing:
            start = listing
        end = self._parse_date(self.GetParameter("end"), datetime(2026, 7, 15))

        self.SetStartDate(start.year, start.month, start.day)
        self.SetEndDate(end.year, end.month, end.day)
        self.SetAccountCurrency("USDT")
        self.SetCash(100000)
        self.SetBrokerageModel(BrokerageName.Binance, AccountType.Cash)

        self.symbol = self.AddCrypto(ticker, resolution, Market.Binance).Symbol
        # Benchmark against the traded pair itself; the LEAN default (BTCUSDC)
        # has no local data and would log benign "failed data request" noise.
        self.SetBenchmark(self.symbol)
        self.resolution = resolution
        self.periods_per_year = self.PERIODS_PER_YEAR.get(resolution, 60 * 24 * 365)

        # ---- indicators / windows ----
        # Volatility lookback (number of bars). 300 works well for Minute; for
        # coarser resolutions it is capped so warm-up stays reasonable.
        default_lookback = 300 if resolution in (Resolution.Second, Resolution.Minute) else 60
        self.lookback = int(self.GetParameter("lookback") or default_lookback)
        self.returns_window = RollingWindow[float](self.lookback)
        self.last_price = None

        # Trend filter: rolling mean of price over the last N bars.
        self.mean_lookback = 11
        self.price_window = RollingWindow[float](self.mean_lookback)

        self.SetWarmUp(self.lookback + 1)

        # ---- fee-aware trading constraints ----
        self.taker_fee = 0.001                       # Binance spot taker fee
        self.min_edge = 2 * self.taker_fee           # price move must clear round-trip fee
        # Minimum holding period scales with the bar size so it isn't tiny on
        # daily bars nor huge on second bars.
        hold_bars = 15 if resolution in (Resolution.Second, Resolution.Minute) else 2
        self.min_holding_period = self._bar_span(resolution) * hold_bars
        self.last_trade_time = None

        self.volatility_threshold = float(self.GetParameter("vol_threshold") or 0.8)

        self.Log(f"Init symbol={ticker} res={res_name} start={start:%Y-%m-%d} "
                 f"end={end:%Y-%m-%d} lookback={self.lookback}")

    # ------------------------------------------------------------------ helpers
    def _parse_date(self, s, default):
        if not s:
            return default
        try:
            return datetime.strptime(str(s), "%Y%m%d")
        except ValueError:
            return default

    def _bar_span(self, resolution):
        return {
            Resolution.Second: timedelta(seconds=1),
            Resolution.Minute: timedelta(minutes=1),
            Resolution.Hour:   timedelta(hours=1),
            Resolution.Daily:  timedelta(days=1),
        }.get(resolution, timedelta(minutes=1))

    # -------------------------------------------------------------------- OnData
    def OnData(self, data):
        if not data.ContainsKey(self.symbol) or data[self.symbol] is None:
            return

        current_price = data[self.symbol].Close

        if self.last_price is not None:
            bar_return = (current_price - self.last_price) / self.last_price
            self.returns_window.Add(bar_return)

        self.last_price = current_price
        self.price_window.Add(current_price)

        if self.IsWarmingUp or not self.returns_window.IsReady or not self.price_window.IsReady:
            return

        returns_list = list(self.returns_window)
        bar_volatility = np.std(returns_list)
        annualized_volatility = bar_volatility * np.sqrt(self.periods_per_year)

        mean_price = np.mean(list(self.price_window))
        price_edge = (current_price - mean_price) / mean_price

        can_trade = (
            self.last_trade_time is None
            or (self.Time - self.last_trade_time) >= self.min_holding_period
        )

        low_volatility = annualized_volatility < self.volatility_threshold
        trending_up = price_edge > self.min_edge
        trending_down = price_edge < -self.min_edge

        if not can_trade:
            return

        if low_volatility and trending_up:
            if not self.Portfolio.Invested:
                self.SetHoldings(self.symbol, 1.0)
                self.last_trade_time = self.Time
        elif (not low_volatility) or trending_down:
            if self.Portfolio.Invested:
                self.Liquidate(self.symbol)
                self.last_trade_time = self.Time
