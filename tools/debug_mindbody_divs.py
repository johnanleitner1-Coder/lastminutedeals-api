"""Inspect the div structure of the Mindbody schedule."""
from pathlib import Path
from bs4 import BeautifulSoup
import re

html = Path(".tmp/mindbody_debug.html").read_text(encoding="utf-8")
soup = BeautifulSoup(html, "html.parser")

# Find the schedule container
sched = soup.find("div", class_="classSchedule-mainTable-loaded")
if not sched:
    sched = soup.find("div", class_=re.compile(r"classSchedule"))
    print(f"Fallback: {sched.get('class') if sched else 'not found'}")

if sched:
    print(f"Schedule div found: class={sched.get('class')}")
    print(f"Inner HTML length: {len(str(sched))}")
    print("\n=== CHILD ELEMENTS ===")
    children = list(sched.children)
    print(f"Total children: {len(children)}")
    for i, child in enumerate(children[:30]):
        import bs4
        if isinstance(child, bs4.NavigableString):
            text = str(child).strip()
            if text:
                print(f"  [{i}] TEXT: '{text[:80]}'")
        else:
            cls = ' '.join(child.get('class', []))
            inner = ' '.join(child.stripped_strings)
            print(f"  [{i}] <{child.name} class='{cls}'>: '{inner[:100]}'")

    print("\n=== FIRST 3000 CHARS OF SCHEDULE DIV HTML ===")
    print(str(sched)[:3000])
