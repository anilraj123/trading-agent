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
        self.last_gainers = []
        self.last_losers = []
        self.last_active = []
        self.last_finviz_gainers = []
        self.last_finviz_losers = []
        self.last_marketwatch = []

    def discover_trending_stocks(self) -> List[str]:
        from .config import Config
        stocks = set(UNIVERSE_100)

        source_names = {
            self._get_yahoo_gainers: "last_gainers",
            self._get_yahoo_losers: "last_losers",
            self._get_yahoo_most_active: "last_active",
            self._get_finviz_gainers: "last_finviz_gainers",
            self._get_finviz_losers: "last_finviz_losers",
            self._get_marketwatch_movers: "last_marketwatch",
        }

        live_sources = list(source_names.keys())

        for source in live_sources:
            attr = source_names[source]
            try:
                result = source()
                setattr(self, attr, result or [])
                if result:
                    stocks.update(result)
                    logger.info(f"{source.__name__}: found {len(result)} stocks")
                else:
                    logger.warning(f"{source.__name__}: returned 0 stocks — feed may be blocked or changed")
            except Exception as e:
                logger.warning(f"{source.__name__} failed: {e}")
                setattr(self, attr, [])

        stocks = {s for s in stocks if s not in Config.BLACKLIST}
        final_list = list(stocks)[:150]  # Increased from 100 to 150 for better diversity in 50-stock watchlist
        self.discovered_stocks = final_list

        from datetime import datetime
        self.last_discovery_time = datetime.now()

        logger.info(
            f"Stock universe updated: {len(final_list)} stocks "
            f"(gainers={len(self.last_gainers)}, losers={len(self.last_losers)}, "
            f"active={len(self.last_active)}, finviz_g={len(self.last_finviz_gainers)}, "
            f"finviz_l={len(self.last_finviz_losers)}, mw={len(self.last_marketwatch)})"
        )
        return final_list

    def _get_yahoo_gainers(self) -> List[str]:
        return self._scrape_yahoo_list("https://finance.yahoo.com/gainers")

    def _get_yahoo_losers(self) -> List[str]:
        return self._scrape_yahoo_list("https://finance.yahoo.com/losers")

    def _get_yahoo_most_active(self) -> List[str]:
        return self._scrape_yahoo_list("https://finance.yahoo.com/most-active")

    def _scrape_yahoo_list(self, url: str) -> List[str]:
        stocks = []
        resp = None
        try:
            resp = self.session.get(url, timeout=10)
        except Exception as e:
            logger.warning(f"Yahoo request failed ({url}): {e}")
            return []
        if resp.status_code != 200:
            logger.warning(f"Yahoo returned {resp.status_code} ({url})")
            return []
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            # Strategy 1: collect tickers from /quote/ links (fragile)
            links = soup.find_all("a", href=True)
            for link in links:
                href = link.get("href", "")
                if "/quote/" in href:
                    parts = href.split("/quote/")[1].split("/")[0].split("?")[0]
                    if parts.isalpha() and len(parts) <= 5 and '.' not in parts:
                        stocks.append(parts.upper())

            if len(stocks) < 5:
                # Strategy 2: look for tickers in <td> elements with data-test="quoteLink"
                for cell in soup.select('[data-test="quoteLink"]'):
                    text = cell.get_text(strip=True)
                    if text.isalpha() and len(text) <= 5 and text.isupper():
                        stocks.append(text)

            if len(stocks) < 5:
                # Strategy 3: extract from embedded JSON
                for script in soup.find_all("script"):
                    text = script.string or ""
                    if "symbol" in text or "ticker" in text.lower():
                        import re as _re
                        for m in _re.finditer(r'"(?:symbol|ticker)"\s*:\s*"([A-Z]{1,5})"', text):
                            stocks.append(m.group(1))

        except Exception as e:
            logger.warning(f"Yahoo parse failed ({url}): {e}")
            return []
        return list(set(stocks))[:50]

    def _scrape_finviz_list(self, url: str) -> List[str]:
        stocks = []
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for link in soup.find_all("a", href=True):
                    href = link.get("href", "")
                    if href.startswith("/quote/"):
                        parts = href.split("/quote/")[1].split(".")[0]
                        if parts.isalpha() and len(parts) <= 5:
                            stocks.append(parts.upper())
        except Exception as e:
            logger.warning(f"Finviz request failed ({url}): {e}")
        return list(set(stocks))[:50]

    def _get_finviz_gainers(self) -> List[str]:
        return self._scrape_finviz_list("https://finviz.com/screener.ashx?v=111&f=sh_curvol_o1000,sh_relvol_o1.5&ft=4")

    def _get_finviz_losers(self) -> List[str]:
        return self._scrape_finviz_list("https://finviz.com/screener.ashx?v=111&f=sh_curvol_o1000,sh_relvol_o1.5,an_recom_buyup,change_u&ft=4")

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
        return list(set(stocks))[:50]
