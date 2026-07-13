from AlgorithmImports import *
class BasicEthereumSmaCrossover(QCAlgorithm):

    def initialize(self):
        # Backtest period
        self.set_start_date(2021, 1, 1)
        self.set_end_date(2025, 12, 31)

        # Starting virtual cash
        self.set_cash(10000)

        # Use Coinbase crypto data / brokerage model
        self.set_brokerage_model(BrokerageName.COINBASE, AccountType.CASH)

        # Add Ethereum/USD daily data
        self.eth = self.add_crypto(
            "ETHUSD",
            Resolution.DAILY,
            Market.COINBASE
        ).symbol

        # Indicators
        self.fast_sma = self.sma(self.eth, 20, Resolution.DAILY)
        self.slow_sma = self.sma(self.eth, 50, Resolution.DAILY)

        # Need 50 days before the slow SMA is ready
        self.set_warm_up(50, Resolution.DAILY)

        # Strategy state
        self.current_signal = 0  # 0 = cash, 1 = holding ETH

        # We will invest 90%, not 100%, to leave cash buffer
        self.target_weight = 0.90
        self.cash_buffer = 0.01

    def on_data(self, data):
        if self.is_warming_up:
            return

        if self.eth not in data.bars:
            return

        if not self.fast_sma.is_ready or not self.slow_sma.is_ready:
            return

        price = data.bars[self.eth].close

        # Plot price and moving averages
        self.plot("ETHUSD", "Price", price)
        self.plot("Moving Averages", "20D SMA", self.fast_sma.current.value)
        self.plot("Moving Averages", "50D SMA", self.slow_sma.current.value)

        bullish = self.fast_sma.current.value > self.slow_sma.current.value

        # Buy ETH when fast SMA crosses above slow SMA
        if bullish and self.current_signal == 0:
            self.set_crypto_holdings(self.eth, self.target_weight)
            self.current_signal = 1
            self.debug(f"BUY ETH at {price}")

        # Sell ETH when fast SMA falls below slow SMA
        elif not bullish and self.current_signal == 1:
            self.set_crypto_holdings(self.eth, 0)
            self.current_signal = 0
            self.debug(f"SELL ETH at {price}")

    def set_crypto_holdings(self, symbol, percentage):
        """
        Crypto-specific position sizing.
    This avoids using self.settings.free_portfolio_value because it may be None.
        """
        crypto = self.securities[symbol]
        base_currency = crypto.base_currency

        if crypto.price <= 0:
            return

    # Use our own cash buffer instead of self.settings.free_portfolio_value
        usable_portfolio_value = self.portfolio.total_portfolio_value * (1 - self.cash_buffer)

    # Target amount of ETH
        target_quantity = percentage * usable_portfolio_value / base_currency.conversion_rate

    # Current ETH amount
        current_quantity = base_currency.amount

    # Difference between target ETH and current ETH
        quantity = target_quantity - current_quantity

    # Respect lot size
        lot_size = crypto.symbol_properties.lot_size
        quantity = round(quantity / lot_size) * lot_size

        if self.is_valid_order_size(crypto, quantity):
            self.market_order(symbol, quantity)

    def is_valid_order_size(self, crypto, quantity):
        return abs(crypto.price * quantity) > crypto.symbol_properties.minimum_order_size