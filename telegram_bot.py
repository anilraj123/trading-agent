#!/usr/bin/env python3
"""Read-only Telegram command bot for the trading droplet.

Long-polls Telegram and answers a fixed set of SAFE, read-only commands
(/status /pnl /positions /help) by querying Alpaca. Locked to a single
allow-listed chat id — messages from anyone else are ignored. No arbitrary
execution, no order placement. Run as a systemd service on the droplet.
"""
import os, json, time, urllib.request, urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
COST_BASIS = 1350.0  # initial deposit, for lifetime P&L

def load_env(path=os.path.join(ROOT, ".env")):
    env = {}
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env

ENV = load_env()
TG_TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = str(ENV.get("TELEGRAM_CHAT_ID", "")).strip()
ALP_BASE = ENV.get("ALPACA_BASE_URL", "https://api.alpaca.markets").rstrip("/")
ALP_H = {"APCA-API-KEY-ID": ENV.get("ALPACA_API_KEY", ""),
         "APCA-API-SECRET-KEY": ENV.get("ALPACA_SECRET_KEY", "")}

def alp(path):
    r = urllib.request.Request(ALP_BASE + path, headers=ALP_H)
    return json.load(urllib.request.urlopen(r, timeout=30))

def tg(method, **params):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    return json.load(urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=40))

def send(text):
    try:
        tg("sendMessage", chat_id=ALLOWED, text=text)
    except Exception as e:
        print("send failed:", e, flush=True)

# ---- command handlers (read-only) ----
def cmd_status():
    a = alp("/v2/account"); clk = alp("/v2/clock"); pos = alp("/v2/positions")
    eq = float(a["equity"]); le = float(a["last_equity"]); cash = float(a["cash"]); lmv = float(a["long_market_value"])
    names = ", ".join(f"{p['symbol']} {float(p['market_value'])/eq*100:.0f}%" for p in
                      sorted(pos, key=lambda x: -float(x['market_value']))[:6])
    return (f"📊 Trading bot\n"
            f"Equity: ${eq:,.0f}  (today {eq-le:+.2f} / {(eq/le-1)*100:+.2f}%)\n"
            f"Deployed: {lmv/eq*100:.0f}%   Cash: ${cash:,.0f} ({cash/eq*100:.0f}%)\n"
            f"Positions: {len(pos)} — {names or 'none'}\n"
            f"Market: {'OPEN' if clk['is_open'] else 'closed'}")

def cmd_pnl():
    a = alp("/v2/account"); eq = float(a["equity"]); le = float(a["last_equity"])
    return (f"💵 P&L\n"
            f"Today: {eq-le:+.2f} ({(eq/le-1)*100:+.2f}%)\n"
            f"Equity: ${eq:,.2f}\n"
            f"Lifetime vs ${COST_BASIS:,.0f}: {eq-COST_BASIS:+.2f} ({(eq/COST_BASIS-1)*100:+.2f}%)")

def cmd_positions():
    a = alp("/v2/account"); eq = float(a["equity"]); pos = alp("/v2/positions")
    if not pos:
        return "No open positions (100% cash)."
    lines = ["📦 Positions:"]
    for p in sorted(pos, key=lambda x: -float(x['market_value'])):
        mv = float(p['market_value'])
        lines.append(f"  {p['symbol']:6} ${mv:>6.0f}  {mv/eq*100:>4.0f}%  {float(p['unrealized_plpc'])*100:+.1f}%")
    return "\n".join(lines)

def cmd_help():
    return ("🤖 Commands:\n/status — equity, deployment, market\n/pnl — today + lifetime P&L\n"
            "/positions — open positions\n/help — this list\n\n(Read-only. No trades can be placed from here.)")

HANDLERS = {"/status": cmd_status, "/start": cmd_status, "/pnl": cmd_pnl,
            "/positions": cmd_positions, "/pos": cmd_positions, "/help": cmd_help}

def handle(text):
    cmd = (text or "").strip().split()[0].lower().split("@")[0]
    fn = HANDLERS.get(cmd)
    if not fn:
        return f"Unknown command '{cmd}'. Send /help."
    try:
        return fn()
    except Exception as e:
        return f"⚠️ Error fetching data: {e}"

def main():
    if not TG_TOKEN or not ALLOWED:
        print("missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env", flush=True); return
    print(f"telegram bot up; allow-listed chat={ALLOWED}", flush=True)
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            res = tg("getUpdates", **params).get("result", [])
            for u in res:
                offset = u["update_id"] + 1
                m = u.get("message") or u.get("edited_message") or {}
                chat = str(m.get("chat", {}).get("id", ""))
                text = m.get("text", "")
                if chat != ALLOWED:
                    print(f"ignored msg from {chat}", flush=True)
                    continue
                if text:
                    send(handle(text))
        except Exception as e:
            print("loop error:", e, flush=True)
            time.sleep(5)

if __name__ == "__main__":
    main()
