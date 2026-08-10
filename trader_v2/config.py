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

    # --- options (leverage on strong theses; shares stay the default) -------
    # An option expresses a thesis only when conviction >= OPT_MIN_CONVICTION,
    # the catalyst is dated within the TTL, the account level allows it, and a
    # contract passes every filter below. Anything else falls back to shares.
    OPT_ENABLED = os.getenv("V2_OPT_ENABLED", "true").lower() == "true"
    OPT_MIN_CONVICTION = int(os.getenv("V2_OPT_MIN_CONVICTION", "4"))
    OPT_DTE_MIN = int(os.getenv("V2_OPT_DTE_MIN", "30"))
    OPT_DTE_MAX = int(os.getenv("V2_OPT_DTE_MAX", "45"))
    OPT_DELTA_MIN = float(os.getenv("V2_OPT_DELTA_MIN", "0.40"))
    OPT_DELTA_MAX = float(os.getenv("V2_OPT_DELTA_MAX", "0.60"))
    OPT_MIN_OI = int(os.getenv("V2_OPT_MIN_OI", "100"))
    OPT_MAX_SPREAD_PCT = float(os.getenv("V2_OPT_MAX_SPREAD_PCT", "0.10"))   # of mid, not $
    OPT_MAX_PREMIUM_PCT = float(os.getenv("V2_OPT_MAX_PREMIUM_PCT", "0.25")) # of capital, per position
    OPT_MIN_PREMIUM = float(os.getenv("V2_OPT_MIN_PREMIUM", "50"))
    OPT_SLEEVE_CAP_PCT = float(os.getenv("V2_OPT_SLEEVE_CAP_PCT", "0.35"))   # total open premium / capital
    OPT_STOP_TIERS_SPEC = os.getenv("V2_OPT_STOP_TIERS", "5:-0.25,14:-0.40,-0.55")
    OPT_TAKE_PROFIT_PCT = float(os.getenv("V2_OPT_TAKE_PROFIT_PCT", "0.60"))
    OPT_FORCE_EXIT_DTE = int(os.getenv("V2_OPT_FORCE_EXIT_DTE", "3"))
    OPT_FILL_WAIT_SEC = int(os.getenv("V2_OPT_FILL_WAIT_SEC", "45"))
    OPT_REQUIRE_GREEKS = os.getenv("V2_OPT_REQUIRE_GREEKS", "true").lower() == "true"
    OPT_FEED = os.getenv("V2_OPT_FEED") or None                              # None -> SDK default

    # --- PDT guard (dormant on cash accounts — see trader_v2/guards.py) -----
    PDT_SOFT_MAX = int(os.getenv("V2_PDT_SOFT_MAX", "2"))

    # --- capital anchor mode ------------------------------------------------
    # static: sizing anchored to CAPITAL (paper apples-to-apples vs v1).
    # dynamic: sizing anchored to live account equity each cycle.
    CAPITAL_MODE = os.getenv("V2_CAPITAL_MODE", "static")

    # --- research -----------------------------------------------------------
    # Sonnet 5: better analyst, intro pricing ($2/$10 per MTok) through 2026-08-31.
    # NOTE: Sonnet 5 rejects non-default sampling params — every call site must
    # pass temperature=None (llm_engine omits the kwarg for None).
    RESEARCH_MODEL = os.getenv("V2_RESEARCH_MODEL", "claude-sonnet-5")
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
        assert 0 < V2Config.OPT_DELTA_MIN < V2Config.OPT_DELTA_MAX <= 1
        assert 0 < V2Config.OPT_DTE_MIN < V2Config.OPT_DTE_MAX
        assert 0 < V2Config.OPT_MAX_PREMIUM_PCT <= V2Config.OPT_SLEEVE_CAP_PCT <= 1
        assert V2Config.OPT_TAKE_PROFIT_PCT > 0
        assert V2Config.CAPITAL_MODE in ("static", "dynamic")
        from trader_v2.options import parse_stop_tiers
        parse_stop_tiers(V2Config.OPT_STOP_TIERS_SPEC)  # raises on a bad spec
