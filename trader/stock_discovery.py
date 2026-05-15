import json
import logging
import requests
from typing import List
from bs4 import BeautifulSoup

logger = logging.getLogger("trader.discovery")

UNIVERSE_100 = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "JPM", "V", "JNJ",
    "WMT", "PG", "MA", "UNH", "HD", "DIS", "BAC", "XOM", "PFE", "CSCO",
    "INTC", "VZ", "KO", "PEP", "MRK", "ABT", "TMO", "COST", "NFLX", "ADBE",
    "CRM", "AMD", "QCOM", "TXN", "AVGO", "ORCL", "ACN", "LLY", "DHR", "NKE",
    "NEE", "BMY", "UNP", "LOW", "PM", "RTX", "LIN", "HON", "AMGN", "SPGI",
    "BLK", "SBUX", "CAT", "GS", "AXP", "DE", "IBM", "GE", "ISRG", "NOW",
    "INTU", "TJX", "AMT", "CVS", "PLD", "MDT", "ZTS", "SYK", "ADP", "BKNG",
    "MMM", "CI", "MO", "GILD", "REGN", "VRTX", "MU", "LRCX", "ADI", "KLAC",
    "AMAT", "MCHP", "SNPS", "CDNS", "MRVL", "NXPI", "ON", "SPY", "QQQ",
    "IWM", "DIA", "VTI", "XLK", "XLF", "XLV", "XLE", "XLI", "XLP", "XLU"
]

class StockDiscovery:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
        })
        self.discovered_stocks = list(UNIVERSE_100)
        self.last_discovery_time = None

    def discover_trending_stocks(self) -> List[str]:
        from .config import Config
        stocks = set(UNIVERSE_100)

        live_sources = [
            self._get_yahoo_gainers,
            self._get_yahoo_losers,
            self._get_yahoo_most_active,
            self._get_marketwatch_movers,
        ]

        for source in live_sources:
            try:
                result = source()
                if result:
                    stocks.update(result)
                    logger.info(f"{source.__name__}: found {len(result)} stocks")
            except Exception as e:
                logger.warning(f"{source.__name__} failed: {e}")

        stocks = {s for s in stocks if s not in Config.BLACKLIST}
        final_list = list(stocks)[:150]  # Increased from 100 to 150 for better diversity in 50-stock watchlist
        self.discovered_stocks = final_list

        from datetime import datetime
        self.last_discovery_time = datetime.now()

        logger.info(f"Stock universe updated: {len(final_list)} stocks (trending + core)")
        return final_list

    def _get_yahoo_gainers(self) -> List[str]:
        return self._scrape_yahoo_list("https://finance.yahoo.com/gainers")

    def _get_yahoo_losers(self) -> List[str]:
        return self._scrape_yahoo_list("https://finance.yahoo.com/losers")

    def _get_yahoo_most_active(self) -> List[str]:
        return self._scrape_yahoo_list("https://finance.yahoo.com/most-active")

    def _scrape_yahoo_list(self, url: str) -> List[str]:
        stocks = []
        try:
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                links = soup.find_all("a", href=True)
                for link in links:
                    href = link.get("href", "")
                    if "/quote/" in href:
                        parts = href.split("/quote/")[1].split("/")[0].split("?")[0]
                        if parts.isalpha() and len(parts) <= 5 and '.' not in parts:
                            stocks.append(parts.upper())
        except:
            pass
        return list(set(stocks))[:50]  # Increased from 30 to 50 per source

    def _get_marketwatch_movers(self) -> List[str]:
        stocks = []
        try:
            resp = self.session.get(
                "https://www.marketwatch.com/investing/stock/active",
                timeout=10
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a.get("href", "")
                    if "/investing/stock/" in href:
                        symbol = href.split("/stock/")[1].split("/")[0].split("?")[0]
                        if symbol.isalpha() and len(symbol) <= 5:
                            stocks.append(symbol.upper())
        except:
            pass
        return list(set(stocks))[:50]  # Increased from 20 to 50
