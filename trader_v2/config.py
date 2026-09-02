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

    # --- entry hard gates (deterministic, applied to every NEW thesis) ------
    # The lessons file has said for weeks that these are hard disqualifiers,
    # and the analyst kept "honouring" them by sizing down to conviction 3
    # instead of skipping (LEU 1.19x, BHVN +16% 5d, SNPS 0.88x). A prompt
    # cannot enforce a gate; code can. Research rejects a thesis whose screen
    # metrics fail any gate (journalled in research_run.validation_errors).
    # 0 disables a gate; a missing metric never blocks.
    GATE_MIN_VOLUME_RATIO = float(os.getenv("V2_GATE_MIN_VOLUME_RATIO", "1.4"))
    GATE_MAX_MOVE_5D_PCT = float(os.getenv("V2_GATE_MAX_MOVE_5D_PCT", "7.0"))
    GATE_MAX_RSI = float(os.getenv("V2_GATE_MAX_RSI", "67"))

    # --- per-thesis risk cap ------------------------------------------------
    # Exits cap winners at low single digits (trail floor = entry, 5-day TTL),
    # so a thesis whose invalidation sits 7% below the zone is negative
    # expectancy by construction (NBTX: zone top 45.5, inval 40 -> -7.1%).
    # Worst-case fill (zone high) to invalidation must be <= this; research
    # rejects the thesis otherwise, and a revision may not loosen past it.
    MAX_RISK_PCT = float(os.getenv("V2_MAX_RISK_PCT", "0.05"))

    # --- lessons hygiene ----------------------------------------------------
    # A lesson minted from ONE trade is an observation, not a rule. Lessons
    # carry (n=k) trade counts; below this the prompts treat them as tentative.
    LESSON_MIN_TRADES = int(os.getenv("V2_LESSON_MIN_TRADES", "3"))
    # Post-mortem/weekly also see how each exit aged (price now vs exit) for
    # exits within this many days, so exit lessons rest on the post-exit path.
    POSTMORTEM_FOLLOWUP_DAYS = int(os.getenv("V2_POSTMORTEM_FOLLOWUP_DAYS", "10"))

    # --- nightly universe screen -------------------------------------------
    # The analyst only sees names that pass the winning-profile screen over a
    # broad fixed universe (S&P 500 + 400 via trader_v2/universe.py), plus the
    # book. Trending scrapes are folded in but screened identically.
    UNIVERSE_SOURCES = os.getenv("V2_UNIVERSE_SOURCES", "sp500,sp400")
    UNIVERSE_REFRESH_DAYS = int(os.getenv("V2_UNIVERSE_REFRESH_DAYS", "7"))
    SCREEN_BAR_DAYS = int(os.getenv("V2_SCREEN_BAR_DAYS", "120"))          # calendar days of daily bars
    SCREEN_MIN_RSI = float(os.getenv("V2_SCREEN_MIN_RSI", "50"))           # bullish RSI floor (cap = GATE_MAX_RSI)
    SCREEN_REQUIRE_TREND = os.getenv("V2_SCREEN_REQUIRE_TREND", "true").lower() == "true"  # above both SMAs
    SCREEN_BEARISH_MAX = int(os.getenv("V2_SCREEN_BEARISH_MAX", "3"))     # 0 while options are off
    SCREEN_INCLUDE_TRENDING = os.getenv("V2_SCREEN_INCLUDE_TRENDING", "true").lower() == "true"
    # One position per GICS sector: SMR+LEU (nuclear) and NBTX+BHVN (biotech)
    # fell together on 8/28. Unknown sector is never capped. 0 disables.
    MAX_PER_SECTOR = int(os.getenv("V2_MAX_PER_SECTOR", "1"))

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

    @staticmethod
    def capital_base(equity) -> float:
        """The one capital anchor every consumer must use: live equity in
        dynamic mode, the static V2_CAPITAL otherwise. The executor, the
        research prompt, and reporting must all agree on this number — the
        first live night showed the analyst being told $1350 while the
        executor sized on $1032."""
        if V2Config.CAPITAL_MODE == "dynamic" and equity:
            return float(equity)
        return V2Config.CAPITAL

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
        assert V2Config.GATE_MIN_VOLUME_RATIO >= 0 and V2Config.GATE_MAX_MOVE_5D_PCT >= 0
        assert 0 <= V2Config.GATE_MAX_RSI <= 100
        assert 0 < V2Config.MAX_RISK_PCT <= abs(V2Config.DISASTER_STOP_PCT)
        assert V2Config.LESSON_MIN_TRADES >= 1 and V2Config.POSTMORTEM_FOLLOWUP_DAYS >= 0
        assert V2Config.SCREEN_BAR_DAYS >= 90                      # SMA50 needs ~72 calendar days
        if V2Config.GATE_MAX_RSI > 0:
            assert 0 <= V2Config.SCREEN_MIN_RSI < V2Config.GATE_MAX_RSI
        assert V2Config.SCREEN_BEARISH_MAX >= 0 and V2Config.MAX_PER_SECTOR >= 0
        from trader_v2.options import parse_stop_tiers
        parse_stop_tiers(V2Config.OPT_STOP_TIERS_SPEC)  # raises on a bad spec
