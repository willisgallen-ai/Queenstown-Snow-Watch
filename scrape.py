"""
Queenstown Snow Watch — scraper (v2)

KEY FINDING from manual research on 14 Jul 2026: the official resort sites for
Coronet Peak and Cardrona/Treble Cone load their live report data via
JavaScript, so a plain fetch can't see it. BUT snow.nz's *individual*
per-resort pages (not their combined Queenstown/Wanaka overview pages) mirror
the same operational feed, server-rendered as plain HTML — lift-by-lift
status, and for Coronet Peak specifically, individually named trails. That's
the source this version uses for three of the four resorts.

Remarkables is the exception: its OWN official site (theremarkables.co.nz)
does server-render, and gives cleaner %-open-by-difficulty data than its
snow.nz mirror, so that stays the primary source there.

No headless browser needed anywhere in this version — plain `requests`
throughout. Big simplification over the first draft of this script, which
assumed (wrongly, for Coronet/Cardrona/TC) that a real browser was required.

Still unverified end-to-end — I have no live network access in the sandbox
this was written in. Run it locally once and check data.json before trusting
the schedule. Section-header strings (STATUS_SUFFIXES, the header lists in
scrape_coronetpeak/cardrona/treblecone) are the most likely thing to need a
tweak if a resort changes their page layout.
"""

import json
import re
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

REMARKABLES_URL = "https://www.theremarkables.co.nz/weather-report/"
SNOWNZ_CORONETPEAK_URL = "https://www.snow.nz/area/nz/queenstown/coronetpeak/"
SNOWNZ_CARDRONA_URL = "https://www.snow.nz/area/nz/wanaka/cardrona/"
SNOWNZ_TREBLECONE_URL = "https://www.snow.nz/area/nz/wanaka/treble-cone/"


def get_text(url, timeout=20):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser").get_text(separator="\n")


# ---------------- Remarkables (official site, server-rendered) ----------------

def find_pct_before_label(label, text):
    m = re.search(r"(\d{1,3})%\s*OPEN\s*\n+\s*" + re.escape(label), text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if re.search(r"CLOSED\s*\n+\s*" + re.escape(label), text, re.IGNORECASE):
        return 0
    return None


def parse_lifts_block_remarkables(text):
    """The '## Lifts' section on theremarkables.co.nz reads as 'Open From /
    9am / <name>' or 'Closed / <name>' triples in extracted text — that was
    my read of it via a different fetch tool. lifts_open/lifts_total came
    back correct in the first live run but lifts=[] came back empty, so that
    assumption is wrong somewhere. Debug prints below show the real text so
    the regex can be fixed from evidence instead of another guess."""
    start = text.find("## Lifts")
    if start == -1:
        print("  [debug] '## Lifts' marker not found on Remarkables page", file=sys.stderr)
        return []
    end = text.find("## Terrain", start)
    block = text[start: end if end != -1 else start + 3000]
    print(f"  [debug] Remarkables lifts block, raw (first 800 chars):\n{block[:800]!r}", file=sys.stderr)
    lifts = []
    for m in re.finditer(
        r"(Open From\s*\n\s*\d{1,2}\s*[ap]m|Closed|Wind\s*Hold)\s*\n+\s*([A-Z][A-Za-z0-9'’\- ]{2,40})",
        block,
    ):
        raw, name = m.group(1).lower(), m.group(2).strip()
        status = "Open" if raw.startswith("open") else ("Wind Hold" if "wind" in raw else "Closed")
        lifts.append({"name": name, "status": status})
    print(f"  [debug] parsed {len(lifts)} Remarkables lifts", file=sys.stderr)
    return lifts


def scrape_remarkables():
    out = {"status": None, "lifts_open": None, "lifts_total": None,
           "base_lower": None, "base_upper": None, "terrain": {}, "lifts": [], "trails": None}
    try:
        text = get_text(REMARKABLES_URL)
    except Exception as e:
        print(f"  ! fetch failed for Remarkables: {e}", file=sys.stderr)
        return out

    m = re.search(r"Mountain Status\s*\n+\s*(Open|Closed)", text, re.IGNORECASE)
    if m:
        out["status"] = m.group(1).title()
    m = re.search(r"Lift Status\s*\n+\s*(\d+)\s*/\s*(\d+)\s*Open", text, re.IGNORECASE)
    if m:
        out["lifts_open"], out["lifts_total"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Snow Base\s*\n+\s*(\d+)\s*-\s*(\d+)cm", text, re.IGNORECASE)
    if m:
        out["base_lower"], out["base_upper"] = int(m.group(1)), int(m.group(2))
    for key, label in [("novice", "Novice"), ("intermediate", "Intermediate"),
                        ("advanced", "Advanced"), ("expert", "Expert")]:
        val = find_pct_before_label(label, text)
        if val is not None:
            out["terrain"][key] = val
    out["lifts"] = parse_lifts_block_remarkables(text)
    return out


# --------- Coronet Peak / Cardrona / Treble Cone (snow.nz individual pages) ---------

# Longest-first: these are stripped off the end of a facility link's label to
# separate the name from its status, e.g. "Greengates Express Advanced Riders
# Only" -> name "Greengates Express", status "Advanced Riders Only".
STATUS_SUFFIXES = [
    "Advanced Riders Only", "Ungroomed fun bumps", "Ungroomed",
    "Wind Hold", "Opens Tuesday", "Open", "Closed", "Available",
]


def split_name_status(label):
    label = label.strip()
    for suf in STATUS_SUFFIXES:
        if label.endswith(suf) and len(label) > len(suf):
            return label[: -len(suf)].strip(), suf
    return label, None


def parse_facility_section(text, header_variants, stop_headers):
    """Finds the first header in header_variants and extracts every lift/trail
    entry up to whichever stop_header comes next. Tries two patterns: the
    original markdown-link-style match, then (if that finds nothing) a plain-
    text fallback that just looks for each known status word and takes
    whatever text runs immediately before it as the name. lifts_open/
    lifts_total came back correct in the first live run but lifts=[] came
    back empty, meaning the markdown-bracket assumption was wrong — the debug
    print below shows the real text so this can be fixed precisely next."""
    start = -1
    matched_header = None
    for h in header_variants:
        start = text.find(h)
        if start != -1:
            matched_header = h
            break
    if start == -1:
        print(f"  [debug] none of {header_variants} found in page text", file=sys.stderr)
        return []
    start = text.find("\n", start)
    end = len(text)
    for h in stop_headers:
        idx = text.find(h, start + 1)
        if idx != -1:
            end = min(end, idx)
    block = text[start:end]
    print(f"  [debug] found '{matched_header}', block raw (first 800 chars):\n{block[:800]!r}", file=sys.stderr)

    items = []
    for m in re.finditer(r"\[([^\]]+)\]\(#facility-\d+\)", block):
        name, status = split_name_status(m.group(1))
        if name:
            items.append({"name": name, "status": status or "Unknown"})

    if not items:
        anchor = "|".join(re.escape(s) for s in sorted(STATUS_SUFFIXES, key=len, reverse=True))
        matches = list(re.finditer(rf"\b({anchor})\b", block))
        prev_end = 0
        for m in matches:
            chunk = block[prev_end: m.start()]
            name = re.sub(r"\s+", " ", chunk).strip(" :\n-")
            if name and len(name) < 45:
                items.append({"name": name, "status": m.group(1)})
            prev_end = m.end()

    print(f"  [debug] parsed {len(items)} items from this section", file=sys.stderr)
    return items


def scrape_snownz_resort(url, lift_headers, trail_headers, all_headers):
    out = {"status": None, "lifts_open": None, "lifts_total": None,
           "base_lower": None, "base_upper": None, "terrain": {}, "lifts": [], "trails": []}
    try:
        text = get_text(url)
    except Exception as e:
        print(f"  ! fetch failed for {url}: {e}", file=sys.stderr)
        return out

    if re.search(r"OPEN\s*\n+\s*Mountain status", text, re.IGNORECASE):
        out["status"] = "Open"
    elif re.search(r"CLOSED\s*\n+\s*Mountain status", text, re.IGNORECASE):
        out["status"] = "Closed"

    m = re.search(r"(\d+)\s*/\s*(\d+)\s*\n+\s*Lifts open", text, re.IGNORECASE)
    if m:
        out["lifts_open"], out["lifts_total"] = int(m.group(1)), int(m.group(2))

    m = re.search(r"(\d+)\s*cm\s*\n+\s*Snow base \(upper\)", text, re.IGNORECASE)
    if m:
        out["base_upper"] = int(m.group(1))
    m = re.search(r"(\d+)\s*cm\s*\n+\s*Snow base \(lower\)", text, re.IGNORECASE)
    if m:
        out["base_lower"] = int(m.group(1))

    out["lifts"] = parse_facility_section(text, lift_headers, all_headers)
    trails = []
    for th in trail_headers:
        trails.extend(parse_facility_section(text, [th], all_headers))
    out["trails"] = trails
    return out


def scrape_coronetpeak():
    return scrape_snownz_resort(
        SNOWNZ_CORONETPEAK_URL,
        lift_headers=["All Lifts :"],
        trail_headers=["Trails :", "Zones :", "Parks :"],
        all_headers=["Road :", "All Lifts :", "Facilities :", "Car Parks :", "Trails :", "Zones :", "Parks :"],
    )


def scrape_cardrona():
    r = scrape_snownz_resort(
        SNOWNZ_CARDRONA_URL,
        lift_headers=["Chair Lifts :"],
        trail_headers=[],
        all_headers=["Road :", "Chair Lifts :", "Parks :", "Car Parks :"],
    )
    r["trails"] = None  # not published as named trails on this page
    return r


def scrape_treblecone():
    r = scrape_snownz_resort(
        SNOWNZ_TREBLECONE_URL,
        lift_headers=["Chair Lifts :"],
        trail_headers=[],
        all_headers=["Road :", "Chair Lifts :", "Car Parks :"],
    )
    r["trails"] = None
    return r


# ---------------- snow-forecast.com 6-day snowfall table ----------------

SNOWFORECAST_URLS = {
    "remarkables": "https://www.snow-forecast.com/resorts/Remarkables/6day/mid",
    "coronetpeak": "https://www.snow-forecast.com/resorts/Coronet-Peak/6day/mid",
    "cardrona": "https://www.snow-forecast.com/resorts/Cardrona/6day/mid",
    "treblecone": "https://www.snow-forecast.com/resorts/Treble-Cone/6day/mid",
}


def scrape_snowforecast_com(url):
    """Parses snow-forecast.com's 6-day forecast table into one total-per-day
    figure each, summing however many AM/PM/night periods fall in that day.

    This is the most structurally uncertain scraper in this file. I inferred
    the table uses real <table>/<td colspan> markup — there's a "Print Table"
    link on the page, which usually implies genuine tabular HTML, and cell
    labels like "cm" / "mm" appeared in the extracted text exactly where a
    real table would put them — but I never saw the raw page source to
    confirm actual class names. If this returns None, or the numbers look
    wrong, view-source the page yourself and check whether day headers really
    carry a colspan attribute; that's the assumption most likely to be wrong.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  ! fetch failed for {url}: {e}", file=sys.stderr)
        return None

    issued = None
    m = re.search(r"Issued:\s*([\d:]*\s*[ap]m\s+\d{1,2}\s+\w+\s+\d{4})", soup.get_text(), re.IGNORECASE)
    if m:
        issued = re.sub(r"\s+", " ", m.group(1)).strip()

    # Day-header row: cells like "Tuesday 14" carrying a colspan = period count
    day_spans = []
    for row in soup.find_all("tr"):
        candidate = []
        for c in row.find_all(["td", "th"]):
            txt = c.get_text(" ", strip=True)
            dm = re.match(r"([A-Za-z]+)\s+(\d{1,2})$", txt)
            if dm:
                span = int(c.get("colspan", 1))
                candidate.append((f"{dm.group(1)[:3]} {dm.group(2)}", span))
        if len(candidate) >= 4:
            day_spans = candidate
            break
    if not day_spans:
        print(f"  ! no day-header row found for {url} (colspan assumption likely wrong)", file=sys.stderr)
        return None

    def find_value_row(unit_label):
        for row in soup.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if cells and cells[0].get_text(" ", strip=True).lower() == unit_label:
                return [c.get_text(strip=True) for c in cells[1:]]
        return None

    cm_cells = find_value_row("cm")
    mm_cells = find_value_row("mm")
    if not cm_cells:
        print(f"  ! no 'cm' row found for {url}", file=sys.stderr)
        return None

    def to_num(s):
        s = (s or "").strip().lstrip("—-").strip()
        return int(s) if s.isdigit() else None

    cm_vals = [to_num(c) for c in cm_cells]
    mm_vals = [to_num(c) for c in mm_cells] if mm_cells else [None] * len(cm_vals)

    days, idx = [], 0
    for label, span in day_spans:
        cm_slice = cm_vals[idx: idx + span]
        mm_slice = mm_vals[idx: idx + span]
        idx += span
        cm_known = [v for v in cm_slice if v is not None]
        mm_known = [v for v in mm_slice if v is not None]
        total_cm = sum(cm_known) if cm_slice else None  # None only if this day wasn't in the table at all
        total_mm = sum(mm_known) if mm_known else 0
        days.append({
            "label": label,
            "cm": (total_cm or 0),
            "mm": total_mm,
            "rain": total_mm > 0 and not total_cm,
        })

    return {"issued": issued, "days": days}


def main():
    print("Scraping Remarkables (official site)...")
    remarkables = scrape_remarkables()
    print("Scraping Coronet Peak (snow.nz)...")
    coronetpeak = scrape_coronetpeak()
    print("Scraping Cardrona (snow.nz)...")
    cardrona = scrape_cardrona()
    print("Scraping Treble Cone (snow.nz)...")
    treblecone = scrape_treblecone()

    print("Scraping 6-day snowfall forecasts (snow-forecast.com)...")
    remarkables["forecast"] = scrape_snowforecast_com(SNOWFORECAST_URLS["remarkables"])
    coronetpeak["forecast"] = scrape_snowforecast_com(SNOWFORECAST_URLS["coronetpeak"])
    cardrona["forecast"] = scrape_snowforecast_com(SNOWFORECAST_URLS["cardrona"])
    treblecone["forecast"] = scrape_snowforecast_com(SNOWFORECAST_URLS["treblecone"])

    data = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "resorts": {
            "remarkables": remarkables,
            "coronetpeak": coronetpeak,
            "cardrona": cardrona,
            "treblecone": treblecone,
        },
    }
    with open("data.json", "w") as f:
        json.dump(data, f, indent=2)
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
