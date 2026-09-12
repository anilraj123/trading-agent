import re, requests
UA = "Mozilla/5.0 (X11; Linux x86_64) trading-agent research"
html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                    headers={"User-Agent": UA}, timeout=30).text
print("len(html)", len(html))
for kw in ("Selected changes", "changes to the list", "Removed", "wikitable"):
    print(f"  {kw!r}: {html.count(kw)} occurrences")
# headings
print("\nheadings:")
for m in re.finditer(r'<h[23][^>]*>(.*?)</h[23]>', html, re.S):
    t = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    if t: print("  ", t[:80])
