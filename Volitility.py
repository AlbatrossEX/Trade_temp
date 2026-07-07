from AlgorithmImports import *
from datetime import timedelta
import numpy as np


class IntradayCryptoVolatility(QCAlgorithm):

    def Initialize(self):
        self.SetStartDate(2020, 1, 1)
        self.SetEndDate(2023, 1, 31)
        self.SetCash("USD", 100000)
        self.SetBrokerageModel(BrokerageName.Coinbase, AccountType.Cash)

        # 1. CHANGE RESOLUTION TO MINUTE
        self.symbol = self.AddCrypto("BTCUSD", Resolution.Minute).Symbol

        # 2. ADJUST LOOKBACK PERIOD
        # We will use the last 300 minutes to calculate intraday volatility
        self.lookback = 300
        self.returns_window = RollingWindow[float](self.lookback)
        self.last_price = None

        # 11-minute rolling mean dataset: maps a trailing mean price to every minute,
        # used below as a trend filter alongside the volatility filter.
        self.mean_lookback = 11
        self.price_window = RollingWindow[float](self.mean_lookback)

        self.SetWarmUp(self.lookback + 1)

        # 3. TRADING FEE CONSTRAINTS
        # Coinbase taker fee assumption (round-trip = enter + exit).
        self.taker_fee = 0.006
        self.min_edge = 2 * self.taker_fee  # price move must clear round-trip fee cost to be worth trading
        self.min_holding_period = timedelta(minutes=15)  # blocks fee-churning flip-flops
        self.last_trade_time = None

    def OnData(self, data):
        if not data.ContainsKey(self.symbol) or data[self.symbol] is None:
            return

        current_price = data[self.symbol].Close

        if self.last_price is not None:
            # This is now a MINUTE return, not a daily return
            minute_return = (current_price - self.last_price) / self.last_price
            self.returns_window.Add(minute_return)

        self.last_price = current_price
        self.price_window.Add(current_price)

        if self.IsWarmingUp or not self.returns_window.IsReady or not self.price_window.IsReady:
            return

        # 3. ADJUST THE MATH FOR MINUTE DATA
        returns_list = list(self.returns_window)
        minute_volatility = np.std(returns_list)

        # 525,600 minutes in a 365-day year
        annualized_volatility = minute_volatility * np.sqrt(525600)

        # 11-minute mean price and the current price's deviation from it (trend signal)
        mean_price = np.mean(list(self.price_window))
        price_edge = (current_price - mean_price) / mean_price

        # 4. EXECUTION LOGIC (Runs every 60 seconds)
        volatility_threshold = 0.8

        can_trade = (
            self.last_trade_time is None
            or (self.Time - self.last_trade_time) >= self.min_holding_period
        )

        low_volatility = annualized_volatility < volatility_threshold
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
