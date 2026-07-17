"""trader_v2 configuration.

v2 is thesis-driven: the LLM is a nightly analyst, deterministic code trades
the plan. Sizing is anchored to V2_CAPITAL (matching v1's live scale), NOT the
paper account's balance — Alpaca paper accounts default to $100k and the point
of the experiment is an apples-to-apples comparison with v1 and SPY.
"""
import os

from trader.config import Config  # shared: keys, base URL, LLM, ntfy, blacklists


class V2Config:
    # --- capital & book -----------------------------------------------------
    CAPITAL = float(os.getenv("V2_CAPITAL", "1350"))            # sizing base, not account equity
    MAX_POSITIONS = int(os.getenv("V2_MAX_POSITIONS", "4"))
    MAX_THESES = int(os.getenv("V2_MAX_THESES", "5"))           # non-terminal theses incl. entered
    POSITION_CAP_PCT = float(os.getenv("V2_POSITION_CAP_PCT", "0.30"))
    MIN_NOTIONAL = 10.0

    # --- exits (v1's hard-won lessons baked in) -----------------------------
    # Thesis invalidation is evaluated ONLY in the close window: intraday
    # tight stops on a daily-horizon signal went 0-for-11 live in v1.
    DISASTER_STOP_PCT = float(os.getenv("V2_DISASTER_STOP_PCT", "-0.08"))   # every cycle
    TRAIL_ACTIVATE_PCT = float(os.getenv("V2_TRAIL_ACTIVATE_PCT", "0.03"))
    TRAIL_STOP_PCT = float(os.getenv("V2_TRAIL_STOP_PCT", "-0.03"))
    CLOSE_WINDOW_MIN = int(os.getenv("V2_CLOSE_WINDOW_MIN", "25"))          # > cycle + runtime

    # --- risk rails ---------------------------------------------------------
    DAILY_LOSS_HALT_PCT = float(os.getenv("V2_DAILY_LOSS_HALT_PCT", "-0.05"))
    TRADING_ENABLED = os.getenv("V2_TRADING_ENABLED", "true").lower() == "true"
    REQUIRE_PAPER = os.getenv("V2_REQUIRE_PAPER", "true").lower() == "true"

    # --- cadence ------------------------------------------------------------
    CYCLE_MINUTES = int(os.getenv("V2_CYCLE_MINUTES", "15"))

    # --- research -----------------------------------------------------------
    RESEARCH_MODEL = os.getenv("V2_RESEARCH_MODEL", "claude-sonnet-4-6")
    WEEKLY_MODEL = os.getenv("V2_WEEKLY_MODEL", "claude-opus-4-8")
    RESEARCH_CANDIDATES = int(os.getenv("V2_RESEARCH_CANDIDATES", "12"))
    NEWS_PER_SYMBOL = int(os.getenv("V2_NEWS_PER_SYMBOL", "5"))
    LESSONS_MAX_CHARS = int(os.getenv("V2_LESSONS_MAX_CHARS", "8000"))      # ~2k tokens

    # --- misc ---------------------------------------------------------------
    DATA_DIR = os.getenv("DATA_DIR", "/app/data")
    # v1's daily_history.csv on the SHARED volume (v2's DATA_DIR is a subdir).
    V1_HISTORY_FILE = os.getenv("V2_V1_HISTORY_FILE", "/app/data/daily_history.csv")

    @staticmethod
    def validate():
        # The cheapest, most valuable rail in v2: a compose-file typo pointing
        # this bot at the live account must be a startup crash, not a trade.
        if V2Config.REQUIRE_PAPER and "paper-api" not in Config.ALPACA_BASE_URL:
            raise ValueError(
                "trader_v2 refuses to run against a non-paper Alpaca endpoint "
                f"({Config.ALPACA_BASE_URL}). Set V2_REQUIRE_PAPER=false to override."
            )
        assert 1 <= V2Config.MAX_POSITIONS <= V2Config.MAX_THESES <= 10
        assert V2Config.DISASTER_STOP_PCT < V2Config.TRAIL_STOP_PCT < 0
        assert V2Config.CLOSE_WINDOW_MIN > V2Config.CYCLE_MINUTES
