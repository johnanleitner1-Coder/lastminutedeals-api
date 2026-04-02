# Platform Coverage Map

Documents all booking/event platforms, their access method, current status,
and what's needed to activate them. Update as new platforms are added.

---

## Active Platforms

| Platform | Category | Access Method | Status | Notes |
|---|---|---|---|---|
| **Eventbrite** | Events | `window.__SERVER_DATA__` scrape | ACTIVE | ~40-50 events/city/72h; no API key |
| **Meetup** | Events/Wellness/Professional | `__NEXT_DATA__` scrape | ACTIVE | ~5-15 events/city/72h; no API key |
| **Ticketmaster** | Events (music/sports/arts) | Discovery API v2 | READY* | *Needs free API key; `TICKETMASTER_API_KEY` in `.env` |

---

## OCTO + API Platforms (Core Infrastructure)

These are the primary supply sources for the LastMinuteDeals pipe. Pure HTTP execution
— no browser automation, no ToS risk, clean supplier relationships.

| Platform | Category | Access | Status | Booking Execution |
|---|---|---|---|---|
| **Ventrata** | Experiences/Tours | Test sandbox immediate; prod 1-2 days | READY — needs `VENTRATA_API_KEY` | OCTOBooker ✓ |
| **Rezdy** | Experiences/Tours/Activities | Free account, API key in 48h | READY — needs `REZDY_API_KEY` | RezdyBooker ✓ |
| **Bokun** | Experiences/Tours | $49/month, self-serve API key | READY — needs `BOKUN_API_KEY` + plan | OCTOBooker ✓ |
| **Peek Pro** | Experiences/Tours | Email ben.smithart@peek.com | WAITING — needs `PEEK_API_KEY` | OCTOBooker ✓ |
| **Xola** | Experiences/Activities | Free sandbox, kick-off call for prod | WAITING — needs `XOLA_API_KEY` | OCTOBooker ✓ |
| **FareHarbor** | Tours/Activities | $250k TTM required for API; affiliate accessible | LONG-TERM — start affiliate now | N/A until API approved |

Supplier config and activation instructions: `tools/seeds/octo_suppliers.json`
Full setup runbook: `workflows/octo_integration.md`

---

## Ready to Activate (Need API Key / Credentials)

| Platform | Category | What's Needed | Sign-Up URL | Est. Volume |
|---|---|---|---|---|
| **Ticketmaster** | Music, sports, theater | Free API key (5,000 req/day) | developer.ticketmaster.com | High in major cities |
| **Telegram** | Distribution | Bot token + channel ID | @BotFather on Telegram | - |
| **Twitter/X** | Distribution | OAuth 1.0a keys | developer.twitter.com | - |
| **Reddit** | Distribution | PRAW credentials | reddit.com/prefs/apps | - |
| **Google Sheets** | Storage | OAuth service account | console.cloud.google.com | - |
| **Netlify** | Hosting | API token + site ID | netlify.com -> account settings | - |

---

## Platforms Investigated but Blocked

| Platform | Category | Blocker | Workaround |
|---|---|---|---|
| **Airbnb** | Hospitality | iCal now requires auth; 19-digit IDs required | Re-enable if auth approach found |
| **Mindbody** | Wellness/Beauty | `prod-mkt-gateway` data frozen at Feb 2025 | Monitor for data refresh; tool is structurally correct |
| **Booksy** | Beauty/Salon | 404 on all search endpoints | Try again; may need partner API |
| **Vagaro** | Wellness/Beauty | 404 on city search | Try again |
| **ClassPass** | Wellness/Fitness | 403 (requires login) | Needs OAuth / partner program |
| **FareHarbor** | Tours/Activities | API requires `X-FareHarbor-API-App` key | Apply for partner access |
| **Bandsintown** | Music events | 403 on public endpoints | Needs API key from bandsintown.com/api |
| **Dice.fm** | Music events | City pages work but only show 2-6 month advance events | Revisit for <72h coverage |
| **Lu.ma** | Events | Page loads but initialData returns 0 entries | Try direct API endpoints |
| **SeatGeek** | Sports/Concerts | 403 (needs free API key) | Register at seatgeek.com/api |
| **OpenTable** | Restaurants | Timeout / anti-scrape | OpenTable API requires partner status |

---

## Platform Architecture

All fetchers output to `.tmp/{platform}_slots.json` and follow the schema in
`tools/normalize_slot.py`. To add a new platform:

1. Create `tools/fetch_{platform}_slots.py`
2. Add `"{platform}"` to `VALID_PLATFORMS` in `tools/normalize_slot.py`
3. Add `"{platform}_slots.json"` to `PLATFORM_FILES` in `tools/aggregate_slots.py`
4. Add fetch step to `run_pipeline.bat`

The `tools/platforms/base.py` `BaseSlotFetcher` class provides shared infrastructure
(session, retry logic, normalization). New fetchers can subclass it or run standalone.

---

## Coverage by Category

| Category | Platforms | Gap |
|---|---|---|
| **Events** | Eventbrite, Meetup, Ticketmaster (ready) | Good coverage |
| **Wellness/Fitness** | Meetup (partial), Mindbody (stale) | Mindbody data stale; need Vagaro/ClassPass |
| **Beauty/Salon** | Meetup (partial) | Need Booksy/Vagaro |
| **Hospitality** | (Airbnb blocked) | Need Booking.com, VRBO |
| **Professional Services** | Meetup (partial) | Good enough for now |
| **Home Services** | None | Low priority; hard to find open APIs |

---

## Next Platform Targets

Priority order — actions to take now:

1. **Ventrata** — get test API key from docs.ventrata.com, run first real OCTO booking test
2. **Rezdy** — sign up free at rezdy.com (48h approval), fastest path to real inventory
3. **Bokun** — $49/month, self-serve, 1.5% booking fee, massive supplier network
4. **Ticketmaster** — free API key, huge event volume
5. **SeatGeek** — free API key at platform.seatgeek.com
6. **Peek Pro** — email ben.smithart@peek.com for OCTO reseller access
7. **Xola** — register developer account, test in sandbox, kick-off call for production

---

## Coverage by Category (Current State)

| Category | Active Platforms | OCTO/API Platforms | Gap |
|---|---|---|---|
| **Experiences/Tours** | None yet | Ventrata, Rezdy, Bokun, Peek, Xola (all built, need keys) | Activate keys |
| **Events** | Eventbrite, Meetup | Ticketmaster (needs key) | Good once TM activated |
| **Wellness/Fitness** | Meetup (partial) | Mindbody (scrape only) | Need Vagaro/ClassPass API |
| **Beauty/Salon** | None | None | Need Booksy/Fresha API |
| **Hospitality** | None | None | Long-term; Booking.com Connectivity Partner |
| **Home Services** | None | None | Low priority |

---

*Last updated: 2026-03-30*
