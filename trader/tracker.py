import csv
import os
import glob
from datetime import date, datetime, timedelta

DATA_DIR = "/app/data"
HISTORY_FILE = f"{DATA_DIR}/daily_history.csv"
TRADE_FILE = f"{DATA_DIR}/trade_log.csv"
LLM_REPORTS_DIR = f"{DATA_DIR}/llm_reports"
DISCOVERY_FILE = f"{DATA_DIR}/stock_discovery.csv"

def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

COLUMNS_TRADE = ["timestamp", "date", "bot", "symbol", "action", "quantity", "entry_price", "exit_price", "pnl_pct", "pnl_dollars", "strategy", "reason", "stop_type"]

COLUMNS_DISCOVERY = ["timestamp", "watchlist_size", "universe_size", "gainers", "losers", "active"]

def save_daily_snapshot(bot: str, start_value: float, end_value: float, pnl: float, trades: int, wins: int, losses: int, total_deposited: float = None):
    _ensure_dir()
    file_exists = os.path.isfile(HISTORY_FILE)
    with open(HISTORY_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["date", "bot", "start_value", "end_value", "pnl", "trades", "wins", "losses", "total_deposited"])
        writer.writerow([date.today().isoformat(), bot, round(start_value, 2), round(end_value, 2), round(pnl, 2), trades, wins, losses, round(total_deposited, 2) if total_deposited else ""])

def save_trade(bot: str, symbol: str, action: str, quantity: float, entry_price: float = None, exit_price: float = None, pnl_pct: float = None, pnl_dollars: float = None, strategy: str = None, reason: str = None, stop_type: str = None):
    _ensure_dir()
    file_exists = os.path.isfile(TRADE_FILE)
    with open(TRADE_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(COLUMNS_TRADE)
        writer.writerow([datetime.now().isoformat(), date.today().isoformat(), bot, symbol, action, quantity, entry_price or "", exit_price or "", pnl_pct or "", pnl_dollars or "", strategy or "", reason or "", stop_type or ""])

def save_discovery_snapshot(watchlist_size, universe_size, gainers, losers, active):
    _ensure_dir()
    file_exists = os.path.isfile(DISCOVERY_FILE)
    with open(DISCOVERY_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(COLUMNS_DISCOVERY)
        writer.writerow([datetime.now().isoformat(), watchlist_size, universe_size, gainers, losers, active])

def _read_trades_since(day):
    if not os.path.isfile(TRADE_FILE):
        return []
    trades = []
    with open(TRADE_FILE) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                row_date = datetime.fromisoformat(row["timestamp"]).date()
                if row_date >= day:
                    trades.append(row)
            except:
                pass
    return trades

def _read_history_since(day):
    if not os.path.isfile(HISTORY_FILE):
        return []
    rows = []
    with open(HISTORY_FILE) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                row_date = date.fromisoformat(row["date"])
                if row_date >= day:
                    rows.append(row)
            except:
                pass
    return rows

def generate_weekly_summary():
    today = date.today()
    monday = today - timedelta(days=today.weekday())

    trades = _read_trades_since(monday)
    history = _read_history_since(monday)

    total_pnl = 0.0
    for row in history:
        try:
            total_pnl += float(row["pnl"])
        except:
            pass

    stock_trades = [t for t in trades if t["bot"] == "trading"]
    options_trades = [t for t in trades if t["bot"] == "options"]

    def win_rate(bot_trades):
        closed = [t for t in bot_trades if t.get("pnl_pct")]
        if not closed:
            return 0, 0, 0, 0
        wins = sum(1 for t in closed if float(t["pnl_pct"]) >= 0)
        losses = sum(1 for t in closed if float(t["pnl_pct"]) < 0)
        return len(closed), wins, losses, round(wins / len(closed) * 100, 1) if closed else 0

    stock_count, stock_wins, stock_losses, stock_wr = win_rate(stock_trades)
    opt_count, opt_wins, opt_losses, opt_wr = win_rate(options_trades)

    biggest_winner = None
    biggest_loser = None
    for t in trades:
        try:
            pnl = float(t["pnl_dollars"])
            if pnl > 0 and (not biggest_winner or pnl > float(biggest_winner["pnl_dollars"])):
                biggest_winner = t
            if pnl < 0 and (not biggest_loser or pnl < float(biggest_loser["pnl_dollars"])):
                biggest_loser = t
        except:
            pass

    stops = [t for t in trades if t.get("stop_type")]

    last_week = monday - timedelta(days=7)
    prev_history = _read_history_since(last_week)
    prev_rows = [r for r in prev_history if r["date"] < monday.isoformat()]
    prev_end = None
    if prev_rows:
        try:
            prev_end = max(float(r["end_value"]) for r in prev_rows if r["bot"] == "trading")
        except:
            pass

    current_end = None
    curr_rows = [r for r in history if r["bot"] == "trading"]
    if curr_rows:
        try:
            current_end = float(curr_rows[-1]["end_value"])
        except:
            pass

    deposited = None
    for row in history:
        try:
            if row.get("total_deposited"):
                deposited = float(row["total_deposited"])
        except:
            pass

    equity_line = ""
    if prev_end and current_end:
        eq_change = current_end - prev_end
        equity_line = f"Equity: ${current_end:.0f} (${prev_end:.0f} last week, {eq_change:+.0f})"

    deposited_line = ""
    if deposited and current_end:
        vs_deposited = current_end - deposited
        deposited_line = f"vs Deposited ${deposited:.0f}: {vs_deposited:+.0f} ({vs_deposited/deposited*100:+.1f}%)"

    total_trades = stock_count + opt_count
    total_wins = stock_wins + opt_wins
    total_losses = stock_losses + opt_losses
    total_wr = round(total_wins / total_trades * 100, 1) if total_trades else 0

    week_start = None
    week_end = None
    for row in sorted(history, key=lambda r: r["date"]):
        try:
            v = float(row["end_value"])
            if week_start is None:
                week_start = v
            week_end = v
        except:
            pass
    week_start_val = week_start or 0
    week_end_val = week_end or 0

    with open(HISTORY_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([today.isoformat(), "weekly", round(week_start_val, 2), round(week_end_val, 2), round(total_pnl, 2), total_trades, total_wins, total_losses, round(deposited, 2) if deposited else ""])

    lines = [
        f"WEEKLY SUMMARY (week of {monday.isoformat()})",
        f"Week P&L: ${total_pnl:+.2f}",
        "",
        f"Stocks: {stock_count} trades ({stock_wins}W/{stock_losses}L) — {stock_wr}% win rate",
        f"Options: {opt_count} trades ({opt_wins}W/{opt_losses}L) — {opt_wr}% win rate",
    ]

    if biggest_winner:
        lines.append(f"Best: {biggest_winner['symbol']} ${biggest_winner['pnl_dollars']}+")
    if biggest_loser:
        lines.append(f"Worst: {biggest_loser['symbol']} ${biggest_loser['pnl_dollars']}")
    if stops:
        lines.append(f"Stops triggered: {len(stops)}")
    if equity_line:
        lines.append("")
        lines.append(equity_line)
    if deposited_line:
        lines.append(deposited_line)

    return "\n".join(lines)


def _read_llm_reports_since(day):
    if not os.path.isdir(LLM_REPORTS_DIR):
        return []
    reports = []
    for fpath in sorted(glob.glob(os.path.join(LLM_REPORTS_DIR, "llm_decision_*.txt"))):
        try:
            fname = os.path.basename(fpath)
            ts_str = fname.replace("llm_decision_", "").replace(".txt", "")
            report_time = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            if report_time.date() >= day:
                with open(fpath) as f:
                    content = f.read()
                reports.append({"time": report_time, "content": content, "path": fpath})
        except:
            pass
    return reports


def _read_discovery_since(day):
    if not os.path.isfile(DISCOVERY_FILE):
        return []
    rows = []
    with open(DISCOVERY_FILE) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["timestamp"])
                if ts.date() >= day:
                    rows.append(row)
            except:
                pass
    return rows


def build_weekly_report(total_deposits, starting_account_value):
    today = date.today()
    monday = today - timedelta(days=today.weekday())

    trades = _read_trades_since(monday)
    history = _read_history_since(monday)
    llm_reports = _read_llm_reports_since(monday)
    discovery = _read_discovery_since(monday)

    total_pnl = 0.0
    for row in history:
        try:
            total_pnl += float(row["pnl"])
        except:
            pass

    stock_trades = [t for t in trades if t["bot"] == "trading"]
    closed = [t for t in stock_trades if t.get("pnl_pct")]
    wins = sum(1 for t in closed if float(t["pnl_pct"]) >= 0)
    losses = sum(1 for t in closed if float(t["pnl_pct"]) < 0)
    win_rate = round(wins / len(closed) * 100, 1) if closed else 0

    sep = "=" * 72
    short_sep = "-" * 40
    lines = []
    lines.append(sep)
    lines.append(f"WEEKLY SUMMARY - Week of {monday.isoformat()}")
    lines.append(sep)
    lines.append("")

    lines.append("PERFORMANCE")
    lines.append(short_sep)
    lines.append(f"Starting Value: ${starting_account_value:.2f}")
    lines.append(f"Total Deposits: ${total_deposits:.2f}")
    last_history = [r for r in history if r["bot"] == "trading"]
    if last_history:
        end_val = float(last_history[-1]["end_value"])
        lines.append(f"End Value: ${end_val:.2f}")
        lines.append(f"Week P&L: ${total_pnl:+.2f}")
    lines.append("")

    lines.append("TRADES")
    lines.append(short_sep)
    lines.append(f"Total: {len(closed)} closed trades ({wins}W/{losses}L) — {win_rate}% win rate")
    lines.append(f"Open trades: {len(stock_trades) - len(closed)}")
    biggest_winner = None
    biggest_loser = None
    for t in stock_trades:
        try:
            pnl = float(t.get("pnl_dollars") or 0)
            if pnl > 0 and (not biggest_winner or pnl > float(biggest_winner["pnl_dollars"])):
                biggest_winner = t
            if pnl < 0 and (not biggest_loser or pnl < float(biggest_loser["pnl_dollars"])):
                biggest_loser = t
        except:
            pass
    if biggest_winner:
        lines.append(f"Best: {biggest_winner['symbol']} +${biggest_winner['pnl_dollars']}")
    if biggest_loser:
        lines.append(f"Worst: {biggest_loser['symbol']} ${biggest_loser['pnl_dollars']}")
    if closed:
        lines.append("")
        lines.append("All Closed Trades:")
        for t in closed:
            pnl_pct = float(t.get("pnl_pct") or 0)
            pnl_dol = float(t.get("pnl_dollars") or 0)
            lines.append(f"  {t['symbol']}: {pnl_pct:+.2f}% (${pnl_dol:+.2f}) — {t.get('strategy', '?')}")
    lines.append("")

    lines.append("OPEN POSITIONS")
    lines.append(short_sep)
    open_pos = [t for t in stock_trades if not t.get("pnl_pct")]
    if open_pos:
        for t in open_pos:
            lines.append(f"  {t['symbol']}: {t['action']} {t['quantity']} @ ${t.get('entry_price', '?')} [{t.get('strategy', '?')}]")
    else:
        lines.append("  No open positions (or all closed)")
    lines.append("")

    lines.append("STOCK DISCOVERY")
    lines.append(short_sep)
    if discovery:
        for d in discovery:
            lines.append(f"  {d['timestamp']}: watchlist={d['watchlist_size']}, universe={d['universe_size']}, gainers={d['gainers']}, losers={d['losers']}, active={d['active']}")
    else:
        lines.append("  No discovery data recorded this week")
    lines.append("")

    lines.append(f"LLM REPORTS THIS WEEK ({len(llm_reports)} total)")
    lines.append(short_sep)
    lines.append("")
    for i, r in enumerate(llm_reports, 1):
        lines.append(f"--- LLM Report #{i} ({r['time'].strftime('%Y-%m-%d %H:%M:%S')}) ---")
        lines.append(r["content"])
        lines.append("")

    lines.append(sep)
    lines.append("END OF WEEKLY REPORT")
    lines.append(sep)

    return "\n".join(lines), len(llm_reports)


def build_daily_report(today, start_value, end_value, daily_pnl, total_deposits, true_trading_pnl, trades_today, win_count, loss_count, positions, spy_return_pct, apy, spy_apy):
    trades = _read_trades_since(today)
    llm_reports = _read_llm_reports_since(today)
    discovery = _read_discovery_since(today)

    sep = "=" * 72
    short_sep = "-" * 40
    lines = []
    lines.append(sep)
    lines.append(f"DAILY SUMMARY - {today.isoformat()}")
    lines.append(sep)
    lines.append("")

    lines.append("PERFORMANCE")
    lines.append(short_sep)
    lines.append(f"Day Start: ${start_value:.2f}")
    lines.append(f"Day End:   ${end_value:.2f}")
    lines.append(f"Day P&L:   ${daily_pnl:+.2f}")
    lines.append(f"Deposits:  ${total_deposits:.2f}")
    lines.append(f"Trading P&L: ${true_trading_pnl:+.2f}")
    lines.append(f"Trades: {trades_today} ({win_count}W/{loss_count}L)")
    lines.append(f"APY: {apy:+.2f}% | vs SPY APY: {spy_apy:+.2f}% | Edge: {apy - spy_apy:+.2f}%")
    lines.append("")

    closed_today = [t for t in trades if t.get("pnl_pct")]
    if closed_today:
        lines.append("TRADES TODAY")
        lines.append(short_sep)
        for t in closed_today:
            pnl_pct = float(t.get("pnl_pct") or 0)
            pnl_dol = float(t.get("pnl_dollars") or 0)
            lines.append(f"  {t['symbol']}: {t['action']} | {pnl_pct:+.2f}% (${pnl_dol:+.2f}) | {t.get('strategy', '?')}")
        lines.append("")

    if positions:
        lines.append("OPEN POSITIONS")
        lines.append(short_sep)
        for p in positions:
            sym = p.get("symbol", "?")
            qty = p.get("qty", 0)
            mv = p.get("market_value", 0)
            entry = p.get("avg_entry_price", 0)
            upnl = p.get("unrealized_pl", 0)
            lines.append(f"  {sym}: {qty} sh @ ${entry:.2f} = ${mv:.2f} (${upnl:+.2f})")
        lines.append("")

    if discovery:
        lines.append("STOCK DISCOVERY TODAY")
        lines.append(short_sep)
        for d in discovery:
            lines.append(f"  {d['timestamp']}: watchlist={d['watchlist_size']}, universe={d['universe_size']}, gainers={d['gainers']}, losers={d['losers']}, active={d['active']}")
        lines.append("")

    if llm_reports:
        lines.append(f"LLM CYCLES TODAY ({len(llm_reports)} total)")
        lines.append(short_sep)
        lines.append("")
        for i, r in enumerate(llm_reports, 1):
            lines.append(f"--- LLM Report #{i} ({r['time'].strftime('%H:%M:%S')}) ---")
            lines.append(r["content"])
            lines.append("")

    lines.append(sep)
    lines.append("END OF DAILY SUMMARY")
    lines.append(sep)

    return "\n".join(lines), len(llm_reports)
