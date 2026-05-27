# Pre-Weekend Review — May 30, 2026

Review and consider changes after end of week.

---

### 1. SPY regime filter — tiered response instead of hard block

When SPY RSI < 40 but individual stocks are making 5–8% moves with 15–18x volume, a hard buy block misses real alpha.

Proposal:
- **SPY RSI 30–40**: reduce position size by 50%, allow high-conviction buys only (`buy_score > 3.0`)
- **SPY RSI < 30**: hard block as currently implemented

---

### 2. SPY regime reset intraday

Confirm the regime block resets mid-session when SPY recovers. If SPY RSI was 23 at 9:30am but recovers to 45 by 1pm, buys should be re-enabled. The check runs every cycle, so this should already work — verify by reviewing logs for `SPY regime filter` messages at different times.

---

### 3. NVDA put stop-loss not firing

Entry $0.935 (contract value $93.50), current value $54 = −42% loss. The −55% stop threshold for >14 DTE means it should exit at ~$42. It hasn't fired yet which is correct, but confirm the position management cycle is checking this every 15 minutes as designed.

---

### 4. Options — minimum contract value floor check for legacy positions

TSLA $630 call ($26 value, essentially dead) and PFE call (−$17) are zombie positions. The $10 floor should eventually close them but confirm it's running against all open options positions each cycle.

---

### 5. LLM prompt — remove "mean_reversion" from strategy field options

The output format still lists `mean_reversion` as a valid strategy choice even though the system is now momentum-only. Small cleanup but avoids confusing the LLM.

---

### 6. Add regime state to daily summary

Currently the daily summary doesn't clearly show whether the SPY regime was blocked or normal. Adding a line like:

```
Regime: BLOCKED (SPY RSI 23.75)
```

to the daily summary would make weekly reviews much easier.

---

### 7. NKE call — active management call this week

10 DTE with +27.5% gain ($91 entry, $116 current). Decision needed:
- **Hold** for +50% target ($1.365 contract value ≈ $136.50)
- **Exit early** — theta decay accelerates at <15 DTE, +27% now vs risking drawdown

The DTE-based stop applies at ≤14 DTE (−40% stop, exit at ~$56). Proactively taking profit vs riding theta decay is worth the LLM considering as a signal.
