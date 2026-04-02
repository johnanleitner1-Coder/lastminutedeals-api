"""Analyze FareHarbor embed JS to find availability API patterns."""
import requests, re

HEADERS = {'User-Agent': 'Mozilla/5.0 Chrome/122'}

r = requests.get('https://fareharbor.com/embeds/script/calendar/nyc-ferry/?full-items=yes', headers=HEADERS, timeout=15)
text = r.text
print(f"Script length: {len(text)}")

# Find API patterns
api_matches = re.findall(r'/api/external/v[0-9]/[a-z0-9_/-]+', text)
print(f"\nAPI patterns found: {len(api_matches)}")
for m in api_matches:
    print(f"  {m}")

# Find availability patterns
avail_ctx = []
for m in re.finditer(r'availabilit[a-z/\?]', text, re.IGNORECASE):
    start = max(0, m.start() - 100)
    end = min(len(text), m.end() + 100)
    avail_ctx.append(text[start:end])

print(f"\nAvailability contexts: {len(avail_ctx)}")
for ctx in avail_ctx[:5]:
    print(f"  ...{ctx}...")
    print()

# Find date-range or calendar patterns
date_ctx = []
for pattern in ['date-range', 'date_range', 'calendar/', 'availabilities']:
    for m in re.finditer(pattern, text, re.IGNORECASE):
        start = max(0, m.start() - 50)
        end = min(len(text), m.end() + 100)
        date_ctx.append((pattern, text[start:end]))

print(f"\nDate/calendar contexts: {len(date_ctx)}")
for pat, ctx in date_ctx[:5]:
    print(f"  [{pat}] ...{ctx}...")
    print()

# Look for fareharbor.com domain references
fh_refs = []
for m in re.finditer(r'fareharbor', text, re.IGNORECASE):
    start = max(0, m.start() - 20)
    end = min(len(text), m.end() + 100)
    fh_refs.append(text[start:end])

print(f"\nFareHarbor domain refs: {len(fh_refs)}")
for ref in fh_refs[:10]:
    print(f"  {ref}")
