# Parameter Review Checklist

Track equity milestones and revisit configuration thresholds as account grows.

## Equity Milestones

### $1,500 Equity
**Action**: Revisit `TOTAL_DEPLOYED_PCT` for options bot
- Current: 0.50 (allows ~6 concurrent positions)
- New: 0.33 (caps to ~3 concurrent positions)
- Rationale: Reduce position concentration risk as capital grows

**Status**: ⏳ Pending

---

### $2,500 Equity
**Action**: Revisit RSI thresholds for trading bot
- Current: 35/65 (tight, generates more signals)
- Consider: 30/70 (standard, reduces noise)
- Rationale: At higher equity, quality matters more than quantity

**Status**: ⏳ Pending

---

### $5,000 Equity
**Action**: Full position sizing review
- Review `PER_POSITION_PCT` for options bot (currently 0.08)
- Review `RISK_MAX_POSITION_PCT` for trading bot (currently 0.10)
- Review `TA_MIN_BUY_SCORE` and `TA_MIN_SELL_SCORE` (currently 0.65 and 0.6)
- Assess if daily trade limit `RISK_MAX_TRADES_PER_DAY` (currently 10) is still appropriate
- Review holding period `RISK_MAX_HOLDING_DAYS` (currently 3)

**Status**: ⏳ Pending

---

## Recent Changes (May 14, 2026)

| Parameter | Old | New | Reason |
|-----------|-----|-----|--------|
| `TA_MIN_BUY_SCORE` | 0.5 | 0.65 | Reduce weak signal entries |
| `TA_MIN_SELL_SCORE` | 1.0 | 0.6 | Easier exits, avoid asymmetric hold bias |
| `PER_POSITION_PCT` (options) | 0.025 | 0.08 | Higher budget for options at low equity |
| Pre-filter | None | Added | Filter watchlist before LLM call |

---

## Current Account State
- **Equity**: ~$840
- **Options Allocated**: $420 (50% of equity)
- **Trading Allocated**: $420 (50% of equity)
- **Next Milestone**: $1,500 (75% growth)
