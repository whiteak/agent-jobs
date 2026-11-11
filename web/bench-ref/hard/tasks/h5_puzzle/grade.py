import re, sys
WANT = {"Q1": "7334", "Q2": "359", "Q3": "sunday", "Q4": "7220", "Q5": "5768"}
text = sys.stdin.read()
got = {}
for k in WANT:
    m = re.findall(rf"^\W*{k}\W*[:=]\s*\**\s*([A-Za-z]+|[\d,]+)", text, re.M)
    if m:
        got[k] = m[-1].replace(",", "").lower()
passed = sum(got.get(k) == v for k, v in WANT.items())
print(passed, len(WANT), " ".join(f"{k}={got.get(k)}" for k in WANT))
