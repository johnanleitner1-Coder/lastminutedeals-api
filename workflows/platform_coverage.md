# Platform Coverage Map

Documents all booking platforms, their access method, current status,
and what's needed to activate them. Update as new platforms are added.

**Strategic focus (2026-04-17):** Bokun/OCTO-only. All non-OCTO platforms
(Eventbrite, Mindbody, Ticketmaster, Meetup, Luma, Airbnb, LiquidSpace,
SeatGeek, Dice, Booksy, FareHarbor) have been removed from the codebase.

---

## Active Platforms (OCTO Standard)

Pure HTTP booking execution via the OCTO standard API. No browser automation,
no ToS risk, clean supplier relationships.

| Platform | Category | Access | Status | Booking Execution |
|---|---|---|---|---|
| **Bokun** | Experiences/Tours | $49/month, self-serve API key | ACTIVE | OCTOBooker |
| **Ventrata** | Experiences/Tours | Test sandbox immediate; prod 1-2 days | READY | OCTOBooker |
| **Peek Pro** | Experiences/Tours | Email ben.smithart@peek.com | WAITING | OCTOBooker |
| **Xola** | Experiences/Activities | Free sandbox, kick-off call for prod | WAITING | OCTOBooker |
| **Zaui** | Experiences/Tours | Partner application | WAITING | OCTOBooker |
| **Checkfront** | Experiences/Activities | Partner application | WAITING | OCTOBooker |

## Active Platforms (Non-OCTO API)

| Platform | Category | Access | Status | Booking Execution |
|---|---|---|---|---|
| **Rezdy** | Experiences/Tours/Activities | Free account, API key in 48h | READY | RezdyBooker |

Supplier config and activation instructions: `tools/seeds/octo_suppliers.json`
Full setup runbook: `workflows/octo_integration.md`

---

## Platform Architecture

All fetchers output to `.tmp/{platform}_slots.json` and follow the schema in
`tools/normalize_slot.py`. To add a new OCTO-compliant platform:

1. Add supplier config to `tools/seeds/octo_suppliers.json`
2. Add platform name to `VALID_PLATFORMS` in `tools/normalize_slot.py`
3. Fetch slots with `tools/fetch_octo_slots.py` (handles all OCTO platforms)
4. Booking execution uses `OCTOBooker` in `tools/complete_booking.py`

For non-OCTO platforms (like Rezdy), create a dedicated fetcher and booker.

---

## Active Suppliers (Bokun)

| Supplier | Region | Products | Commission |
|---|---|---|---|
| Arctic Adventures | Iceland | Northern Lights, Ice Cave, Snorkeling tours | 25% |
| Bicycle Roma | Italy | Rome bike tours | 25% |
| All Washington View | USA (DC) | DC sightseeing tours | 25% |
| TUTU VIEW Ltd | China | City tours (Shanghai, Chongqing, Xi'an, etc.) | 20% |

Full supplier list: `tools/supplier_contracts.json`

---

## Next Platform Targets

Priority order for expanding OCTO supply:

1. **Ventrata** - get prod API key from docs.ventrata.com
2. **Rezdy** - sign up free at rezdy.com (48h approval)
3. **Peek Pro** - email ben.smithart@peek.com for OCTO reseller access
4. **Xola** - register developer account, test in sandbox

---

*Last updated: 2026-04-17*
