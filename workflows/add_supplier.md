# Add Supplier Workflow

Onboard a new Bokun/OCTO supplier into the system. Follow every step in order.

## Prerequisites

- Supplier must be on Bokun with OCTO API access
- You need their **vendor_id** from the Bokun reseller dashboard

---

## Step 1: Config files (manual — single source of truth)

### A. `tools/seeds/octo_suppliers.json`

1. Add the vendor_id to the `vendor_ids` array
2. Add an entry to `vendor_id_to_supplier_map`:
   ```json
   "VENDOR_ID": {"name": "Supplier Name", "city": "Primary City", "country": "XX"}
   ```
3. If the supplier uses product reference codes, add entries to `reference_supplier_map`
4. If specific product IDs need mapping, add to `product_id_map`

### B. `tools/supplier_contracts.json`

Add a new entry with pricing config:
```json
{
  "supplier_name": "Supplier Name",
  "bokun_vendor_id": VENDOR_ID,
  "pricing_model": "markup",
  "commission_pct": 20,
  "contract_status": "active",
  "cities": ["City Name"],
  "country": "XX"
}
```

---

## Step 2: Run the pipeline

```bash
python tools/fetch_octo_slots.py
python tools/aggregate_slots.py
python tools/compute_pricing.py
python tools/sync_to_supabase.py
```

Verify slots appear: `curl https://api.lastminutedealshq.com/slots?city=CITY_NAME`

---

## Step 3: Update static fallback

### `tools/run_api_server.py` — `_SUPPLIER_DIR_STATIC` (~line 871)

Add an entry to the list:
```python
{"name": "Supplier Name", "destinations": ["City, Country"], "platform": "Bokun"},
```

This is the fallback when Supabase is unreachable. The live supplier directory auto-discovers from the database, but this ensures agents always see the full network.

---

## Step 4: SEO (only if new destination)

If the supplier operates in a country/city NOT already in `_TOUR_DESTINATIONS` (~line 899 in `run_api_server.py`):

1. Add a new entry to `_TOUR_DESTINATIONS` with slug, name, query, title, meta_desc, intro, highlights
2. Add the country's ISO code to `_COUNTRY_ISO` if not present
3. The sitemap.xml auto-updates (it reads from `_TOUR_DESTINATIONS`)
4. Submit the new page URL to Google Search Console for indexing

If the supplier is in an EXISTING destination, no SEO changes needed — the destination page already fetches live inventory.

---

## Step 5: Update counts in static docs

Grep for the old count (e.g., "23 suppliers") and update in:

- [ ] `README.md` — description + supplier table
- [ ] `openapi.yaml` — line 6 description
- [ ] `smithery.yaml` — line 28 description
- [ ] `server.json` — line 5 description

**Do NOT manually update these** — they auto-update at runtime:
- MCP tool descriptions (built dynamically from `_supplier_count()`)
- SEO page subtitles (use `_supplier_count()`)
- Health endpoint supplier count

---

## Step 6: Deploy + verify

```bash
git add -A && git commit -m "Add supplier: Supplier Name (City, Country)"
git push origin main
```

After Railway deploys:
1. `curl https://api.lastminutedealshq.com/health` — check slot count increased
2. Test MCP: search_slots with the new city
3. Check `/tours/DESTINATION` page shows the new supplier's slots
4. `get_supplier_info` should auto-include the new supplier (live from Supabase)

---

## What NOT to do

- Don't edit MCP instruction strings to add supplier names — they build dynamically
- Don't manually update supplier counts in Python code — `_supplier_count()` handles it
- Don't re-publish to Smithery just for a new supplier — the tools/list response is live
- Don't create a new SEO page for a city that's already covered by an existing destination
