"""Save Mindbody HTML and print structure for debugging."""
import time
from playwright.sync_api import sync_playwright
from pathlib import Path
from bs4 import BeautifulSoup
import re

def main():
    site_id = "18692"
    url = f"https://clients.mindbodyonline.com/classic/mainclass?studioid={site_id}"

    print(f"Loading {url}...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ).new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        html = page.content()
        browser.close()

    Path(".tmp").mkdir(exist_ok=True)
    Path(".tmp/mindbody_debug.html").write_text(html, encoding="utf-8")
    print(f"Saved {len(html)} bytes to .tmp/mindbody_debug.html")

    soup = BeautifulSoup(html, "html.parser")

    # Print all table rows with meaningful content
    print("\n=== ALL TABLE ROWS ===")
    rows = soup.find_all("tr")
    print(f"Total <tr> elements: {len(rows)}")

    for i, row in enumerate(rows[:50]):
        text = " | ".join(" ".join(c.stripped_strings) for c in row.find_all("td"))
        if text.strip():
            print(f"  Row {i:03d}: {text[:120]}")

    # Look for date patterns
    print("\n=== ROWS WITH DATES ===")
    for i, row in enumerate(rows):
        row_text = " ".join(row.stripped_strings)
        if re.search(r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*', row_text) and re.search(r'\d{4}', row_text):
            print(f"  Row {i:03d}: {row_text[:150]}")

    # Look for time patterns
    print("\n=== ROWS WITH TIMES ===")
    for i, row in enumerate(rows):
        row_text = " ".join(row.stripped_strings)
        if re.search(r'\d{1,2}:\d{2}\s*[ap]m', row_text, re.IGNORECASE):
            cells = [" ".join(c.stripped_strings) for c in row.find_all("td")]
            print(f"  Row {i:03d} [{len(cells)} cells]:")
            for j, c in enumerate(cells[:8]):
                print(f"    cell[{j}]: {c[:60]}")

    # Look for class-related CSS classes
    print("\n=== ELEMENTS WITH CLASS-RELATED CSS ===")
    for el in soup.find_all(class_=re.compile(r'class|session|schedule', re.IGNORECASE))[:20]:
        print(f"  <{el.name} class='{' '.join(el.get('class', [])[:3])}'>: {' '.join(el.stripped_strings)[:80]}")

    # Print first 2000 chars of body text
    print("\n=== BODY TEXT (first 3000 chars) ===")
    body_text = soup.get_text(" ", strip=True)
    print(body_text[:3000])


if __name__ == "__main__":
    main()
