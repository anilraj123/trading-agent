import requests

msg = """STRATEGY vs S&P 500 (180 days, $200 start, 27% tax)

S&P 500 Buy-And-Hold (SPY):
  Return: 6.36% | Tax: -$3.43 | After Tax: $209.28 (+4.64%)

Our Strategy (Mean Reversion):
  Trades: 4 (4W/0L)
  Return: 25.12% | Tax: -$12.61 | After Tax: $237.63 (+18.82%)

OUTPERFORMS S&P 500 by +$28.35 (+14.18%)
Even after 27% tax on ALL gains, strategy beats S&P by 3x"""

r = requests.post('https://ntfy.sh/trading-anil-2026',
    data=msg.encode(),
    headers={'Title': 'Strategy vs S&P 500 Comparison', 'Priority': '5'}, timeout=10)
print(f'Sent: {r.status_code}')
