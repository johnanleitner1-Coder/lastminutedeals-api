"""
scrape_bokun_supplier_directory.py - Export Bokun marketplace suppliers via the UI.

This avoids DevTools/API reverse-engineering entirely:
  1. Opens the Bokun marketplace supplier directory in a visible Chrome session.
  2. Waits for the user to sign in normally if needed.
  3. Walks page=N through the supplier directory.
  4. Extracts visible supplier cards from the rendered UI.
  5. Saves JSON + CSV for downstream use.

Usage:
  python tools/scrape_bokun_supplier_directory.py
  python tools/scrape_bokun_supplier_directory.py --start-page 0 --max-pages 50

Output:
  .tmp/bokun_suppliers.json
  .tmp/bokun_suppliers.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_URL = (
    "https://last-minute-deals-hq-llc.bokun.io/"
    "v2/sales-tools/marketplace/discover?page=0&sortBy=RELEVANCE&typeFilter=SUPPLIER"
)
PROFILE_DIR = Path(".tmp/bokun_scraper_profile")
JSON_OUT = Path(".tmp/bokun_suppliers.json")
CSV_OUT = Path(".tmp/bokun_suppliers.csv")


def build_page_url(page_num: int) -> str:
    parsed = urlparse(BASE_URL)
    qs = parse_qs(parsed.query)
    qs["page"] = [str(page_num)]
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))


def parse_total_count(text: str) -> int | None:
    match = re.search(r"Found\s+([\d,]+)\s+suppliers", text, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def parse_supplier_id(text: str) -> str:
    match = re.search(r"\((\d+)\)\s*$", text.strip())
    return match.group(1) if match else ""


def extract_cards(page) -> list[dict]:
    js = """
    () => {
      const items = [];
      const seen = new Set();

      const candidates = Array.from(document.querySelectorAll('div, article, section, a'))
        .filter((el) => {
          const txt = (el.innerText || '').trim();
          if (!txt) return false;
          if (!/Experiences?/i.test(txt)) return false;
          if (txt.length < 30 || txt.length > 1200) return false;
          return true;
        });

      for (const el of candidates) {
        const txt = (el.innerText || '').trim();
        if (!txt || seen.has(txt)) continue;
        seen.add(txt);

        const lines = txt.split('\\n').map((s) => s.trim()).filter(Boolean);
        if (lines.length < 3) continue;

        const experienceLine = lines.find((line) => /Experiences?/i.test(line)) || '';
        const idMatch = txt.match(/\\((\\d+)\\)\\s*$/m);
        const supplierId = idMatch ? idMatch[1] : '';

        let name = '';
        for (const line of lines) {
          if (/proposal sent/i.test(line)) continue;
          if (/Experiences?/i.test(line)) continue;
          if (/sort by/i.test(line)) continue;
          if (/discover partners/i.test(line)) continue;
          if (/edit your profile/i.test(line)) continue;
          if (line.length < 2) continue;
          name = line;
          break;
        }
        if (!name) continue;

        let location = '';
        const categories = [];
        for (const line of lines) {
          if (line === name || line === experienceLine) continue;
          if (/proposal sent/i.test(line)) continue;
          if (/\\(\\d+\\)\\s*$/.test(line)) continue;
          if (!location && /,/.test(line)) {
            location = line;
            continue;
          }
          if (line.length <= 120) categories.push(line);
        }

        items.push({
          supplier_id: supplierId,
          name,
          experience_line: experienceLine,
          location,
          categories: categories.join(' | '),
          raw_text: txt,
        });
      }

      return items;
    }
    """
    return page.evaluate(js)


def wait_for_directory(page, timeout_ms: int = 300_000) -> int | None:
    start = time.time()
    printed_hint = False
    while (time.time() - start) * 1000 < timeout_ms:
        try:
            body_text = page.locator("body").inner_text(timeout=2_000)
        except Exception:
            time.sleep(1)
            continue

        if "Discover partners" in body_text and "suppliers" in body_text.lower():
            total = parse_total_count(body_text)
            return total

        if (not printed_hint) and (
            "Sign in" in body_text or "/signin" in page.url or "/login" in page.url
        ):
            print("Waiting for Bokun login. Sign in in the opened browser window, then return here.")
            printed_hint = True

        time.sleep(2)

    raise TimeoutError("Timed out waiting for the Bokun supplier directory to load after login.")


def write_outputs(records: list[dict]) -> None:
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")

    with CSV_OUT.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["supplier_id", "name", "experience_line", "location", "categories", "raw_text"],
        )
        writer.writeheader()
        writer.writerows(records)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Bokun marketplace suppliers via the UI.")
    parser.add_argument("--start-page", type=int, default=0, help="Page number to start from.")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Optional cap on how many pages to scrape. 0 = scrape until exhausted.",
    )
    parser.add_argument(
        "--pause-ms",
        type=int,
        default=1500,
        help="Pause between page navigations in milliseconds.",
    )
    args = parser.parse_args()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    all_records: list[dict] = []
    seen_keys: set[str] = set()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR.resolve()),
            channel="chrome",
            headless=False,
            no_viewport=True,
        )

        page = context.new_page()
        page.goto(build_page_url(args.start_page), wait_until="domcontentloaded", timeout=120_000)

        total_suppliers = wait_for_directory(page)
        print(f"Bokun directory loaded. Total suppliers shown by UI: {total_suppliers or 'unknown'}")

        current_page = args.start_page
        empty_pages = 0
        per_page = None

        while True:
            if args.max_pages and (current_page - args.start_page) >= args.max_pages:
                break

            url = build_page_url(current_page)
            print(f"Scraping page {current_page}: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)

            try:
                page.locator("text=Discover partners").wait_for(timeout=30_000)
            except PlaywrightTimeoutError:
                print(f"Page {current_page}: heading did not appear, stopping.")
                break

            page.wait_for_timeout(args.pause_ms)
            cards = extract_cards(page)

            if per_page is None and cards:
                per_page = len(cards)
                print(f"Detected about {per_page} supplier cards per page.")

            added_this_page = 0
            for card in cards:
                key = card["supplier_id"] or card["name"]
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                all_records.append(card)
                added_this_page += 1

            print(
                f"Page {current_page}: visible cards={len(cards)} "
                f"new={added_this_page} total_collected={len(all_records)}"
            )

            if not cards or added_this_page == 0:
                empty_pages += 1
            else:
                empty_pages = 0

            if empty_pages >= 3:
                print("Saw 3 consecutive empty/repeat pages; stopping.")
                break

            if total_suppliers and len(all_records) >= total_suppliers:
                print("Collected at least the total supplier count shown by Bokun; stopping.")
                break

            current_page += 1

        write_outputs(all_records)
        print(f"Wrote {len(all_records)} suppliers to {JSON_OUT} and {CSV_OUT}")
        print("You can close the browser window now.")
        context.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
