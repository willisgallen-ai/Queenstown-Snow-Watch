"""Queenstown Snow Watch scraper.

Uses DOM sections and list items rather than flattened page text. This prevents
adjacent lifts, trails, facilities and car parks being merged together.
"""
from __future__ import annotations

import json
import re
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone, time as dt_time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag

NZ_TZ = ZoneInfo("Pacific/Auckland")

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept-Language": "en-NZ,en;q=0.9",
}
TIMEOUT = 30

URLS = {
    "remarkables": "https://www.theremarkables.co.nz/weather-report/",
    "coronetpeak": "https://www.snow.nz/area/nz/queenstown/coronetpeak/",
    "cardrona": "https://www.metservice.com/mountains-and-parks/ski-fields/cardrona",
    "treblecone": "https://www.metservice.com/mountains-and-parks/ski-fields/treble-cone",
}
FALLBACK_URLS = {
    "cardrona": "https://www.snow.nz/area/nz/wanaka/cardrona/",
    "treblecone": "https://www.snow.nz/area/nz/wanaka/treble-cone/",
}
GRASSHOPPER_URL = "https://www.mountainwatch.com/grasshopper/"
WEBCAM_PAGES = {
    "remarkables": "https://www.mountainwatch.com/new-zealand/the-remarkables/snow-cams",
    "coronetpeak": "https://www.mountainwatch.com/new-zealand/coronet-peak/snow-cams",
    "cardrona": "https://www.mountainwatch.com/new-zealand/cardrona/snow-cams",
    "treblecone": "https://www.mountainwatch.com/new-zealand/treble-cone/snow-cams",
}

DIFFICULTY_MAP = {
    # Coronet Peak
    "big easy": "green", "beginner area": "green", "little easy": "green",
    "gentle annie": "green", "easy rider": "green", "dual carpet slopes": "green",
    "m1": "blue", "shirt front": "blue", "brough's lane": "blue",
    "million dollar": "blue", "pro am": "blue", "rocky return": "blue",
    "greengates": "red", "wall street": "red", "the hurdle": "red",
    "eighth basin": "red", "mid gully": "red", "overrun": "red",
    "west gates": "red", "sugar's run": "red", "tuck": "black",
    "upper walkabout": "black", "the chimney": "black", "exchange drop": "black",
    "race arena": "black", "powder run": "double-black", "back bowls": "double-black",
    # Parks / zones remain unclassified by design.
}


TRAIL_COLOURS_FILE = ROOT / "trail-colours.json"

ONTHESNOW_URLS = {
    "cardrona": "https://www.onthesnow.com/new-zealand/cardrona-alpine-resort/skireport",
    "treblecone": "https://www.onthesnow.com/new-zealand/treble-cone/skireport",
}

# Cardrona and Treble Cone's own site (cardrona-treblecone.com) loads its
# lift/terrain/park widget via client-side JS that a plain fetch can't see —
# confirmed by direct testing, not assumed. These names are hardcoded from a
# screenshot of that live widget (14 Jul 2026) rather than scraped, because
# there's currently no known plain-HTTP-fetchable source for them by name.
# Status for each is DERIVED from the aggregate open-count (see
# scrape_onthesnow / apply_uniform_status below), not scraped per-item —
# accurate when the mountain is fully open or fully closed, honestly
# "unknown" for individual items when it's a partial-open day, since there's
# no way to know *which* specific ones without JS rendering. If a name here
# goes stale (resort renames or adds a lift/zone), it needs a manual update —
# there's no live source to keep it in sync automatically.
KNOWN_LIFTS = {
    "cardrona": ["Learner Conveyors", "McDougall's Chondola", "Whitestar Express",
                 "Captains Express", "Willow's Quad", "Soho Express", "T-Bar",
                 "Valley View", "Kindy Conveyor", "Wells Pipe Platter"],
    "treblecone": ["Home Basin Express", "Saddle Quad", "Beginner Platter"],
}
TERRAIN_ZONES = {
    "cardrona": ["Learners Slope", "Main Basin", "Captains", "Soho", "Little Willows",
                 "Big Willows", "Backcountry Access", "Mt Cardrona", "Arcadia Chutes", "Summit"],
    "treblecone": ["Home Basin", "Saddle Basin", "Matukituki Basin", "Summit Slopes",
                   "Motatapu Chutes", "Sundance", "Backcountry Access", "Morning Skinning"],
}
# Park features are a genuinely separate category from terrain on the resort's
# own site (a "PARK" box distinct from the "TERRAIN" box) — kept separate here
# rather than folded into trails, which is what the previous version wrongly did.
PARK_FEATURES = {
    "cardrona": ["Baby Bucks", "Little Bucks", "Antlers Alley", "Stag Lane", "Big Bucks",
                 "Peoples Pipe", "Olympic Pipe", "Big Air", "Mc Park", "Gravity Cross"],
    "treblecone": [],
}

RESORT_COORDS = {
    "remarkables": (-45.0556, 168.8140),
    "coronetpeak": (-44.9277, 168.7366),
    "cardrona": (-44.8717, 168.9497),
    "treblecone": (-44.6320, 168.8804),
}

# Sourced from each resort's own site (14 Jul 2026) — worth rechecking each
# season, since night-ski dates/times are seasonal and could shift. weekday()
# is Monday=0 .. Sunday=6, so Wed=2, Fri=4.
RESORT_HOURS = {
    "remarkables": {"open": (9, 0), "close": (16, 0)},
    "coronetpeak": {"open": (9, 0), "close": (16, 0),
                     "night_ski_weekdays": (2, 4), "night_open": (16, 0), "night_close": (21, 0)},
    "cardrona": {"open": (8, 30), "close": (16, 0)},
    "treblecone": {"open": (9, 0), "close": (16, 0)},
}


def is_within_operating_hours(resort: str, now: datetime | None = None) -> bool:
    """Sanity backstop, not a primary data source: is NOW (NZ local time)
    within this resort's typical daily window? Accounts for Coronet Peak's
    Wed/Fri (+ occasional Saturday, not modelled here since those dates move
    every season) night skiing. Used to force a resort closed outside hours
    regardless of what a stale scrape might otherwise say — it does NOT force
    a resort open just because it's within hours, since a real weather
    closure during normal hours is still valid and should come from the
    actual scraped status."""
    now = now or datetime.now(NZ_TZ)
    hours = RESORT_HOURS[resort]
    t = now.time()
    if dt_time(*hours["open"]) <= t <= dt_time(*hours["close"]):
        return True
    night_days = hours.get("night_ski_weekdays")
    if night_days and now.weekday() in night_days:
        if dt_time(*hours["night_open"]) <= t <= dt_time(*hours["night_close"]):
            return True
    return False


_SCHEDULED_TIME_RE = re.compile(r"(?:Open\s+From|Opens?)\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)", re.I)


def resolve_scheduled_times(text: str, now: datetime | None = None) -> str:
    """Replaces 'Open From 9am' / 'Opens 9am' style phrases with a plain
    'Open' or 'Closed' token, resolved by comparing the stated time against
    the current NZ time. Without this, 'Open From 9am' was being read as
    simply 'Open' regardless of whether it was actually 9am yet — which is
    very likely why some lifts appeared open when they genuinely weren't, or
    were showing a scheduled future time as if it were current status. Call
    this on raw page text BEFORE the status-word extraction runs, so the
    downstream matching sees a resolved 'Open' or 'Closed' like any other."""
    now = now or datetime.now(NZ_TZ)

    def replace(m: re.Match) -> str:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        if m.group(3).lower() == "pm" and hour != 12:
            hour += 12
        if m.group(3).lower() == "am" and hour == 12:
            hour = 0
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return "Open" if now >= scheduled else "Closed"

    return _SCHEDULED_TIME_RE.sub(replace, text)


def apply_hours_backstop(name: str, resort: dict[str, Any]) -> dict[str, Any]:
    """Final sanity check applied after scraping: if it's currently outside
    this resort's typical operating hours, force status/lifts/trails/park to
    Closed regardless of what was scraped. This protects against a stale
    cached page — or a genuinely correct 'opens at 9am' reading that just
    hasn't been superseded yet — showing as open when the mountain simply
    isn't running right now. Deliberately does NOT force a resort open just
    because it's within hours: a real weather closure during normal
    operating hours is valid and should still come through as scraped."""
    if is_within_operating_hours(name):
        return resort
    resort = deepcopy(resort)
    resort["status"] = "Closed"
    resort["lifts_open"] = 0
    for key in ("lifts", "trails", "park"):
        if resort.get(key):
            resort[key] = [{**item, "status": "Closed"} for item in resort[key]]
    resort["hours_closed"] = True
    return resort
# Open-Meteo picks an elevation from its own terrain model for a bare lat/lon,
# which for these sites often lands well below the actual base area — these
# ski fields climb from ~1200m to ~2000m+, so an uncorrected reading can be
# noticeably milder than what's really happening on the mountain. Passing an
# explicit elevation (base-area height, roughly) fixes that. These are
# approximate on-mountain base elevations, not surveyed values.
RESORT_ELEVATIONS = {
    "remarkables": 1610,
    "coronetpeak": 1220,
    "cardrona": 1670,
    "treblecone": 1260,
}

def load_trail_colours() -> dict[str, str]:
    mapping = dict(DIFFICULTY_MAP)
    try:
        custom = json.loads(TRAIL_COLOURS_FILE.read_text(encoding="utf-8"))
        for resort_map in custom.values():
            if isinstance(resort_map, dict):
                mapping.update({clean(k).lower(): clean(v).lower() for k, v in resort_map.items()})
    except Exception:
        pass
    return mapping

def apply_trail_colours(trails: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapping = load_trail_colours()
    for trail in trails:
        trail["difficulty"] = mapping.get(clean(trail.get("name")).lower(), trail.get("difficulty", "unclassified"))
    return trails

STATUS_PHRASES = sorted([
    "Wind may affect chairlift ops", "Advanced Riders Only", "Closed for Racing",
    "Ungroomed fun bumps", "Chains or 4WD", "Opening Delayed", "On Hold",
    "Wind Hold", "Limited Spaces", "Filling Fast", "Ungroomed", "Available",
    "Variable", "Spaces", "Full", "Open", "Closed",
], key=len, reverse=True)

# Amenity/facility noise that occasionally slips into lift or trail listings
# on less-structured pages (MetService in particular) — none of these are
# skiable terrain or lift infrastructure, so they're excluded outright rather
# than risking them being misclassified as either.
EXCLUDE_TERMS = (
    "forecast", "temperature", "weather", "car park", "carpark", "road",
    "cafe", "restaurant", "rental", "hire", "guest service",
    "information", "info centre", "ski school", "snowsports school",
    "lesson", "creche", "childcare", "medical centre", "first aid",
    "retail", "shop", "store", "atm", "eftpos", "toilet", "bathroom",
    "locker", "day lodge", "lodge access", "accommodation", "booking",
)


def find_summary_text(soup: BeautifulSoup) -> str | None:
    """Best-effort short prose summary from a resort's own report page — the
    human-written 'conditions today' blurb these pages often carry, distinct
    from the structured stat fields (base depth, lift counts) already
    scraped elsewhere. Tries the page's own meta description first (concise,
    site-authored), then falls back to the first substantial paragraph in
    the page body. Not guaranteed to exist on every page — returns None
    rather than guessing if nothing suitable is found."""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        text = clean(meta["content"])
        if len(text) > 30:
            return text
    for p in soup.find_all("p"):
        text = clean(p.get_text(" "))
        if len(text) > 60 and not any(bad in text.lower() for bad in ("cookie", "javascript", "subscribe", "newsletter")):
            return text
    return None


def fetch(url: str) -> tuple[BeautifulSoup, str]:
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser"), response.text


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_number(value: str | None) -> int | None:
    match = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return int(round(float(match.group()))) if match else None


def split_status(label: str) -> tuple[str, str]:
    label = resolve_scheduled_times(clean(label))
    for status in STATUS_PHRASES:
        match = re.search(rf"\s+{re.escape(status)}(?:\s+.*)?$", label, re.I)
        if match:
            name = clean(label[:match.start()])
            actual = clean(label[match.start():])
            # Preserve the meaningful status phrase, but drop appended opening-hour prose.
            canonical = status
            if status == "Spaces": canonical = "Spaces available"
            return name, canonical
    return label, "Unknown"


def normalise_heading(value: str) -> str:
    return re.sub(r"\s*:\s*$", "", clean(value)).lower()


def heading_matches(tag: Tag, title: str) -> bool:
    return tag.name in {"h2", "h3", "h4", "h5", "h6"} and normalise_heading(tag.get_text(" ")) == normalise_heading(title)


def find_heading(soup: BeautifulSoup, titles: list[str]) -> Tag | None:
    wanted = {normalise_heading(t) for t in titles}
    for tag in soup.find_all(["h2", "h3", "h4", "h5", "h6"]):
        if normalise_heading(tag.get_text(" ")) in wanted:
            return tag
    return None


def section_nodes(heading: Tag) -> list[Tag]:
    nodes: list[Tag] = []
    level = int(heading.name[1]) if heading.name and heading.name[1:].isdigit() else 6
    for sibling in heading.next_siblings:
        if isinstance(sibling, Tag):
            if sibling.name in {"h2", "h3", "h4", "h5", "h6"}:
                sibling_level = int(sibling.name[1])
                if sibling_level <= level:
                    break
            nodes.append(sibling)
    return nodes


def parse_list_section(soup: BeautifulSoup, titles: list[str]) -> list[dict[str, str]]:
    heading = find_heading(soup, titles)
    if not heading:
        return []
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in section_nodes(heading):
        candidates = node.find_all("li") if node.name != "li" else [node]
        for li in candidates:
            anchor = li.find("a")
            label = clean(anchor.get_text(" ") if anchor else li.get_text(" "))
            if not label or label.lower() == "n/a":
                continue
            name, status = split_status(label)
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                items.append({"name": name, "status": status})
    return items


def text_value_near_label(soup: BeautifulSoup, label: str, unit: str = "cm") -> int | None:
    # SnowNZ places the numeric value immediately before its descriptive h6 label.
    label_node = soup.find(string=lambda s: s and clean(s).lower() == label.lower())
    if label_node:
        parent = label_node.parent
        previous = parent.find_previous(string=re.compile(rf"\d+(?:\.\d+)?\s*{re.escape(unit)}", re.I))
        if previous:
            return parse_number(str(previous))
    # Fallback against page text, restricted to a small distance around the label.
    text = soup.get_text("\n", strip=True)
    patterns = [
        rf"(\d+(?:\.\d+)?)\s*{re.escape(unit)}\s*\n\s*{re.escape(label)}",
        rf"{re.escape(label)}\s*\n\s*(\d+(?:\.\d+)?)\s*{re.escape(unit)}",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return parse_number(match.group(1))
    return None


def parse_summary_snownz(soup: BeautifulSoup) -> dict[str, Any]:
    text = resolve_scheduled_times(soup.get_text("\n", strip=True))
    result: dict[str, Any] = {
        "status": None, "lifts_open": None, "lifts_total": None,
        "base_lower": None, "base_upper": None, "new_snow_7d": None,
        "terrain": {}, "lifts": [], "trails": [], "carparks": [],
    }
    match = re.search(r"\b(OPEN|CLOSED)\b\s*\n\s*Mountain status", text, re.I)
    if match: result["status"] = match.group(1).title()
    match = re.search(r"(\d+)\s*/\s*(\d+)\s*\n\s*Lifts open", text, re.I)
    if match: result["lifts_open"], result["lifts_total"] = map(int, match.groups())
    result["base_upper"] = text_value_near_label(soup, "Snow base (upper)")
    result["base_lower"] = text_value_near_label(soup, "Snow base (lower)")
    result["new_snow_7d"] = text_value_near_label(soup, "Last 7 Days")
    return result


def parse_snownz(resort: str, soup: BeautifulSoup) -> dict[str, Any]:
    result = parse_summary_snownz(soup)
    result["lifts"] = parse_list_section(soup, ["All Lifts"])
    result["carparks"] = parse_list_section(soup, ["Car Parks"])
    trails: list[dict[str, str]] = []
    for heading in (["Trails"], ["Zones"], ["Parks"]):
        trails.extend(parse_list_section(soup, heading))
    # Remove accidental category leakage and assign known difficulty.
    park_names = {p["name"].lower() for p in result["carparks"]}
    clean_trails = []
    seen = set()
    for trail in trails:
        key = trail["name"].lower()
        if key in park_names or key in seen:
            continue
        seen.add(key)
        trail["difficulty"] = DIFFICULTY_MAP.get(key, "unclassified")
        clean_trails.append(trail)
    result["trails"] = clean_trails
    return result


def parse_remarkables(soup: BeautifulSoup) -> dict[str, Any]:
    text = resolve_scheduled_times(soup.get_text("\n", strip=True))
    result: dict[str, Any] = {
        "status": None, "lifts_open": None, "lifts_total": None,
        "base_lower": None, "base_upper": None, "new_snow_7d": None,
        "terrain": {}, "lifts": [], "trails": [], "carparks": [],
    }
    match = re.search(r"Mountain Status\s*\n\s*(Open|Closed)", text, re.I)
    if match: result["status"] = match.group(1).title()
    match = re.search(r"Lift Status\s*\n\s*(\d+)\s*/\s*(\d+)\s*Open", text, re.I)
    if match: result["lifts_open"], result["lifts_total"] = map(int, match.groups())
    match = re.search(r"Snow Base\s*\n\s*(\d+)\s*[-–]\s*(\d+)\s*cm", text, re.I)
    if match: result["base_lower"], result["base_upper"] = map(int, match.groups())
    for key, label in [("novice", "Novice"), ("intermediate", "Intermediate"), ("advanced", "Advanced"), ("expert", "Expert"), ("extreme", "Extreme")]:
        match = re.search(rf"(\d{{1,3}})%\s*OPEN\s*\n\s*{label}\b", text, re.I)
        if match: result["terrain"][key] = int(match.group(1))
        elif re.search(rf"CLOSED\s*\n\s*{label}\b", text, re.I): result["terrain"][key] = 0

    result["lifts"] = parse_list_section(soup, ["Lifts"])
    terrain_items = parse_list_section(soup, ["Terrain"])
    # Keep only actual terrain/park/backcountry entries; facilities belong elsewhere.
    terrain_keywords = ("park", "trail", "backcountry", "touring", "slopestyle", "stash")
    result["trails"] = [
        {**item, "difficulty": "unclassified"}
        for item in terrain_items if any(k in item["name"].lower() for k in terrain_keywords)
    ]
    result["carparks"] = parse_list_section(soup, ["Car Parks", "Parking"])
    return result



def status_from_text(value: str) -> str:
    v = clean(value).lower()
    if "wind" in v and any(x in v for x in ("hold", "held", "affected", "closed")):
        return "Wind hold"
    if "ungroomed" in v:
        return "Ungroomed"
    if any(x in v for x in ("open", "operating", "running")):
        return "Open"
    if any(x in v for x in ("closed", "not operating")):
        return "Closed"
    return clean(value) or "Unknown"


def parse_metservice_operational(soup: BeautifulSoup, resort: str) -> dict[str, Any]:
    """Parse MetService ski-field operational cards, tables or embedded JSON."""
    result: dict[str, Any] = {
        "status": None, "lifts_open": None, "lifts_total": None,
        "base_lower": None, "base_upper": None, "new_snow_7d": None,
        "terrain": {}, "lifts": [], "trails": [], "carparks": [],
    }
    text = soup.get_text("\n", strip=True)
    # Current state and temperature are commonly rendered in visible text.
    m = re.search(r"\b(Open|Closed)\b\s*(?:for the day|ski field status|field status)?", text, re.I)
    if m:
        result["status"] = m.group(1).title()

    # Read structured rows. MetService has used table rows and card-like list items.
    seen_lifts: set[str] = set(); seen_trails: set[str] = set()
    containers = soup.select("tr, li, [class*='lift'], [class*='trail'], [class*='run'], [class*='facility']")
    lift_terms = ("chair", "express", "quad", "chondola", "conveyor", "carpet", "platter", "t-bar", "t bar", "gondola")
    for node in containers:
        label = clean(node.get_text(" "))
        if len(label) < 3 or len(label) > 180:
            continue
        status = status_from_text(label)
        # Strip the final status phrase from the item name.
        name = re.sub(r"\s+(open|closed|operating|running|wind\s*(?:hold|held)|on hold|ungroomed|not operating).*$", "", label, flags=re.I).strip(" -–|")
        if not name or name.lower() in {"lifts", "trails", "runs", "status"}:
            continue
        lower = name.lower()
        # Applied before classification (not just inside the trail branch) so a
        # facility item can't slip through by accidentally containing a lift
        # word like "express" (e.g. a shuttle/transfer service).
        if any(bad in lower for bad in EXCLUDE_TERMS):
            continue
        if any(term in lower for term in lift_terms):
            if lower not in seen_lifts:
                seen_lifts.add(lower); result["lifts"].append({"name": name, "status": status})
        elif any(k in (" "+label.lower()+" ") for k in (" open ", " closed ", " ungroomed ", " wind hold ", " wind held ")):
            if lower not in seen_trails:
                seen_trails.add(lower)
                result["trails"].append({"name": name, "status": status, "difficulty": DIFFICULTY_MAP.get(lower, "unclassified")})

    # Embedded JSON can contain cleaner lift/run records.
    for script in soup.find_all("script"):
        raw = script.string or script.get_text("", strip=True)
        if not raw or len(raw) < 20:
            continue
        if not any(term in raw.lower() for term in ("lift", "trail", "run")):
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack=[data]
        while stack:
            obj=stack.pop()
            if isinstance(obj, dict):
                name = clean(str(obj.get("name") or obj.get("title") or obj.get("label") or ""))
                stat = clean(str(obj.get("status") or obj.get("state") or obj.get("condition") or ""))
                typ = clean(str(obj.get("type") or obj.get("category") or "")).lower()
                if name and stat:
                    lower=name.lower()
                    if any(bad in lower for bad in EXCLUDE_TERMS):
                        stack.extend(obj.values()); continue
                    record={"name":name,"status":status_from_text(stat)}
                    if "lift" in typ or any(t in lower for t in lift_terms):
                        if lower not in seen_lifts: seen_lifts.add(lower); result["lifts"].append(record)
                    elif any(t in typ for t in ("trail","run","terrain")):
                        if lower not in seen_trails:
                            seen_trails.add(lower); record["difficulty"]=DIFFICULTY_MAP.get(lower,"unclassified"); result["trails"].append(record)
                stack.extend(obj.values())
            elif isinstance(obj, list): stack.extend(obj)

    if result["lifts"]:
        result["lifts_total"] = len(result["lifts"])
        result["lifts_open"] = sum(1 for x in result["lifts"] if x["status"] == "Open")
    # Cardrona's park/feature list is not wanted. Keep only classified pistes there.
    if resort == "cardrona":
        result["trails"] = [t for t in result["trails"] if t.get("difficulty") != "unclassified"]
    return result


def parse_carparks_strict(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Only accept explicit car-park rows, never nearby trail or road prose."""
    items = parse_list_section(soup, ["Car Parks", "Carparks", "Parking"])
    out=[]; seen=set()
    for item in items:
        name=clean(item.get("name")); status=status_from_text(item.get("status", ""))
        if not re.search(r"\b(car\s*park|carpark|parking|the yard|the pines|valley view)\b", name, re.I):
            continue
        # Carpool/space-requirement phrases (e.g. "3 or more per car") are
        # real statuses some resorts use instead of simple Open/Closed —
        # previously coerced to generic "Unknown" by a fixed whitelist that
        # didn't include them. Just a length sanity check now instead of a
        # fixed vocabulary, so real short phrases pass through unchanged.
        if not status or len(status) > 40:
            status = "Unknown"
        if name.lower() not in seen:
            seen.add(name.lower()); out.append({"name":name,"status":status})
    return out


def scrape_onthesnow(resort: str) -> dict[str, Any] | None:
    """OnTheSnow.com mirrors the same OpenSnow-sourced data as the official
    cardrona-treblecone.com report widget — but unlike the official site
    (which loads that widget via client-side JS a plain fetch can't see),
    OnTheSnow's own page is server-rendered. This is the new primary status/
    lift-count/summary source for Cardrona and Treble Cone, replacing the
    previous unanchored 'first Open or Closed word anywhere on the page'
    regex, which was the most likely cause of resorts showing Open when they
    were actually fully closed.

    Patterns below are built from one real fetch of the Treble Cone page —
    unverified against what BeautifulSoup's own get_text() produces from the
    raw HTML (a different rendering pipeline caught me out on this exact
    mistake earlier — see parse_facility_section's history). Debug prints
    included so a mismatch shows real evidence instead of needing another
    guess."""
    try:
        soup, _ = fetch(ONTHESNOW_URLS[resort])
    except Exception as exc:
        print(f"  [debug] OnTheSnow fetch failed for {resort}: {exc}", file=sys.stderr)
        return None
    text = soup.get_text("\n", strip=True)
    out: dict[str, Any] = {"lifts_open": None, "lifts_total": None,
                            "runs_open": None, "runs_total": None,
                            "base_cm": None, "summary": None}

    m = re.search(r"Lifts\s*Open\s*\n+\s*(\d+)\s*/\s*(\d+)\s*open", text, re.I)
    if m:
        out["lifts_open"], out["lifts_total"] = int(m.group(1)), int(m.group(2))
    else:
        print(f"  [debug] OnTheSnow {resort}: 'Lifts Open X/Y' pattern not found", file=sys.stderr)

    m = re.search(r"Runs\s*Open\s*\n+\s*(\d+)\s*/\s*(\d+)\s*open", text, re.I)
    if m:
        out["runs_open"], out["runs_total"] = int(m.group(1)), int(m.group(2))

    m = re.search(r'Base\s*\n+\s*(\d+)\s*"', text, re.I)
    if m:
        out["base_cm"] = round(int(m.group(1)) * 2.54)

    m = re.search(
        r"Snow Reporter Comments:?\s*(?:Last Updated:?\s*([^\n]{3,40}))?\s*\n+(.+?)"
        r"(?=\n(?:Try SkiGPT|How are conditions|Provide Feedback|Resort Overview|#))",
        text, re.I | re.S,
    )
    if m:
        date_part = clean(m.group(1) or "")
        body = clean(re.sub(r"\n+", " ", m.group(2)))
        out["summary"] = (f"{date_part}: " if date_part else "") + body
    else:
        print(f"  [debug] OnTheSnow {resort}: 'Snow Reporter Comments' block not found", file=sys.stderr)

    if all(v is None for v in out.values()):
        return None
    return out


def apply_uniform_status(names: list[str], open_count: int | None, total: int | None) -> list[dict[str, str]]:
    """Builds a [{name, status}] list from hardcoded names, using an
    aggregate open-count as the only signal available (no source publishes
    per-item status for these that a plain fetch can reach). Accurate at the
    two extremes — everything open, or everything closed — which covers the
    common cases (pre-season, storm closures, or full operation). Genuinely
    unknown on a partial-open day, and honestly marked as such rather than
    guessing which specific ones."""
    if open_count == 0:
        return [{"name": n, "status": "Closed"} for n in names]
    if open_count is not None and total is not None and open_count >= len(names):
        return [{"name": n, "status": "Open"} for n in names]
    return [{"name": n, "status": "Unknown"} for n in names]


def scrape_grasshopper_headline() -> dict[str, Any] | None:
    """Pull the actual current forecast text + link from the Grasshopper's NZ
    forecast page. Two things fixed from the previous version: it searched
    for a headline LINK matching news-listing patterns, but
    mountainwatch.com/grasshopper is one continuous forecast page, not an
    index of articles, so that search matched nothing. Then, once fixed to
    read the page directly, it still prioritised the page's generic meta
    description over the actual forecast paragraphs — a meta description is
    static SEO text written once, not updated per-forecast, which is very
    likely why it read as 'some random sentence' rather than real content.
    Now prioritises real forecast prose, preferring a paragraph that
    actually mentions the Southern Lakes (the region Queenstown/Wanaka sit
    in) since that's what's relevant to these four resorts specifically,
    falling back to the first substantial paragraph, and only using the meta
    description as a last resort if no real paragraph is found at all."""
    try:
        soup, _ = fetch(GRASSHOPPER_URL)
        paras = [clean(p.get_text(" ")) for p in soup.find_all("p")]
        paras = [p for p in paras if len(p) > 80]
        summary = next((p for p in paras if "southern lakes" in p.lower()), None)
        if not summary and paras:
            summary = paras[0]
        if not summary:
            meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
            if meta and meta.get("content"):
                summary = clean(meta["content"])
        if not summary:
            return None
        issued = None
        match = re.search(r"Issued:?\s*([^\n]{5,40})", soup.get_text("\n", strip=True), re.I)
        if match:
            issued = clean(match.group(1))
        title = "The Grasshopper — NZ forecast" + (f" (issued {issued})" if issued else "")
        return {"title": title, "summary": summary, "url": GRASSHOPPER_URL, "source": "The Grasshopper · Mountainwatch"}
    except Exception as exc:
        print(f"Grasshopper headline failed: {exc}", file=sys.stderr)
        return None

def snowing_from_condition(condition: str | None) -> bool:
    value = (condition or "").lower()
    snow_terms = ("snow", "flurr", "sleet", "wintry")
    negations = ("no snow", "snow unlikely", "snow clearing")
    return any(term in value for term in snow_terms) and not any(term in value for term in negations)


def parse_current_weather(soup: BeautifulSoup, source_url: str, source_name: str) -> dict[str, Any] | None:
    """Read the current mountain temperature and condition from the resort report."""
    text = soup.get_text("\n", strip=True)
    temp: float | None = None
    condition: str | None = None

    patterns = [
        r"CURRENTLY\s*\n\s*(-?\d+(?:\.\d+)?)°c\s*\n\s*([^\n]+)",
        r"Today\s*\n\s*(-?\d+(?:\.\d+)?)°c\s*\n\s*([^\n]+)",
        r"Weather:?\s*\n\s*(-?\d+(?:\.\d+)?)°\s*([^\n]+)",
        r"(-?\d+(?:\.\d+)?)°\s*([^\n]+)\s*\n\s*Weather",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            temp = float(match.group(1))
            condition = clean(match.group(2))
            break

    if temp is None:
        node = soup.select_one("[class*='temperature'], [class*='current-temp'], [class*='weather-temp']")
        if node:
            found = re.search(r"-?\d+(?:\.\d+)?", node.get_text(" "))
            if found:
                temp = float(found.group())

    if condition is None:
        for selector in ("[class*='weather-condition']", "[class*='condition']", "[class*='weather-description']"):
            node = soup.select_one(selector)
            if node:
                condition = clean(node.get_text(" "))
                break

    if temp is None:
        return None
    return {
        "temperature_c": round(temp, 1),
        "snowing": snowing_from_condition(condition),
        "condition": condition,
        "source": source_name,
        "source_url": source_url,
    }

def open_meteo_current_weather(name: str) -> dict[str, Any] | None:
    """Return current mountain weather from Open-Meteo using the resort
    coordinates and real elevation. This is now the single weather source
    for all four resorts (previously tried each resort's own on-site
    reading first via parse_current_weather, which is no longer called —
    left defined below in case it's useful again later, but every resort
    now goes through this same path so results are directly comparable
    rather than depending on which page's text structure happened to parse
    that run)."""
    lat, lon = RESORT_COORDS[name]
    response = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "elevation": RESORT_ELEVATIONS[name],
            "current": "temperature_2m,weather_code,snowfall",
            "temperature_unit": "celsius",
            "precipitation_unit": "mm",
            "timezone": "Pacific/Auckland",
            "forecast_days": 1,
        },
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    current = payload.get("current") or {}
    temp = current.get("temperature_2m")
    code = current.get("weather_code")
    snowfall = current.get("snowfall")
    if temp is None:
        return None

    snow_codes = {71, 73, 75, 77, 85, 86}
    try:
        code_number = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_number = None
    try:
        snowfall_number = float(snowfall or 0)
    except (TypeError, ValueError):
        snowfall_number = 0.0

    snowing = snowfall_number > 0 or code_number in snow_codes
    return {
        "temperature_c": round(float(temp), 1),
        "snowing": snowing,
        "condition": "Snow" if snowing else "Not snowing",
        "weather_code": code_number,
        "snowfall_cm": snowfall_number,
        "observed": current.get("time"),
        "source": "Open-Meteo",
        "source_url": "https://open-meteo.com/",
    }

def find_webcam_image(soup: BeautifulSoup, base_url: str) -> str | None:
    """Return the latest Mountainwatch camera still, not a page hero image."""
    candidates: list[tuple[int, str]] = []
    for img in soup.find_all("img"):
        sources = [img.get("data-src"), img.get("data-lazy-src"), img.get("data-original"), img.get("src")]
        src = next((x for x in sources if x), None)
        if not src:
            continue
        full = urljoin(base_url, src)
        attrs = " ".join([clean(img.get("alt")), clean(" ".join(img.get("class", []))), clean(src)]).lower()
        if any(bad in attrs for bad in ("logo", "avatar", "icon", "newsletter", "advert", "banner", "hero", "travel", "deal")):
            continue
        score = 0
        if any(word in attrs for word in ("snowcam", "snow-cam", "webcam", "camera", "cam-image")): score += 12
        if any(word in attrs for word in ("basin", "base", "chair", "express", "summit", "captain", "home basin", "saddle")): score += 4
        parent_text = clean(img.parent.get_text(" ") if img.parent else "").lower()
        if any(word in parent_text for word in ("snow cams", "webcam", "camera")): score += 5
        if re.search(r"\.(?:jpe?g|png|webp)(?:\?|$)", full, re.I): score += 1
        if score >= 5: candidates.append((score, full))
    return max(candidates, default=(0, None), key=lambda x:x[0])[1]


SNOWFORECAST_URLS = {
    "remarkables": "https://www.snow-forecast.com/resorts/Remarkables/6day/mid",
    "coronetpeak": "https://www.snow-forecast.com/resorts/Coronet-Peak/6day/mid",
    "cardrona": "https://www.snow-forecast.com/resorts/Cardrona/6day/mid",
    "treblecone": "https://www.snow-forecast.com/resorts/Treble-Cone/6day/mid",
}

def scrape_snow_forecast(url: str) -> dict[str, Any] | None:
    soup, _ = fetch(url)
    issued = None
    match = re.search(r"Issued:\s*([^\n]+)", soup.get_text("\n", strip=True), re.I)
    if match:
        issued = clean(match.group(1))
    day_spans: list[tuple[str, int]] = []
    for row in soup.find_all("tr"):
        candidate = []
        for cell in row.find_all(["td", "th"]):
            label = clean(cell.get_text(" "))
            dm = re.match(r"([A-Za-z]+)\s+(\d{1,2})$", label)
            if dm:
                candidate.append((f"{dm.group(1)[:3]} {dm.group(2)}", int(cell.get("colspan", 1))))
        if len(candidate) >= 4:
            day_spans = candidate
            break
    if not day_spans:
        return None
    def value_row(unit: str) -> list[int | None] | None:
        for row in soup.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if cells and clean(cells[0].get_text(" ")).lower() == unit:
                values=[]
                for cell in cells[1:]:
                    raw=clean(cell.get_text(" ")).replace("—", "").replace("-", "")
                    values.append(int(raw) if raw.isdigit() else None)
                return values
        return None
    cms=value_row("cm")
    if not cms:
        return None
    mms=value_row("mm") or [None]*len(cms)
    days=[]; index=0
    for label, span in day_spans[:6]:
        cm_slice=cms[index:index+span]; mm_slice=mms[index:index+span]; index += span
        known_cm=[v for v in cm_slice if v is not None]
        known_mm=[v for v in mm_slice if v is not None]
        cm=sum(known_cm) if known_cm else 0
        mm=sum(known_mm) if known_mm else 0
        days.append({"label": label, "cm": cm, "mm": mm, "rain": mm > 0 and cm == 0})
    return {"issued": issued, "days": days}

def forecast_stub(previous: dict[str, Any] | None) -> dict[str, Any] | None:
    # Forecast scraping is independent and can remain from the previous valid run.
    return deepcopy(previous.get("forecast")) if previous else None


def load_previous() -> dict[str, Any]:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"resorts": {}}


def merge_fallback(name: str, current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """Status, lifts, and trails all describe the SAME moment and must stay
    internally consistent with each other. The previous version of this
    function patched each field independently from the last good run —
    meaning if only the lift-list parse failed on a given run while status
    succeeded, the result was today's fresh status stitched to an old lift
    list from a previous run. That produces exactly the kind of
    self-contradictory output that was reported: resorts showing Closed
    overall while individual lifts still showed Open, and lift/trail chips
    that didn't track live reality. Now: if the core operational fields
    didn't all come through together on this run, the WHOLE previous
    snapshot is used instead of cherry-picking field by field, so
    status/lifts/trails always describe one consistent point in time.
    Independent fields (forecast, webcam, carparks) still prefer whatever
    this run actually got, since they come from separate sources/requests
    and don't need to match the operational snapshot's timing.

    'trails' is resort-specific: Remarkables' trails list is legitimately
    near-empty by design (parse_remarkables only keeps park/backcountry-
    keyword items, which often matches nothing) — treating that as a core
    field there meant Remarkables failed this check on nearly every run,
    even fully successful ones, which is why it kept showing 'delayed' for
    no real reason. Its real per-run signal is the terrain % dict instead."""
    if not previous:
        return current
    core_fields = ("status", "lifts", "terrain") if name == "remarkables" else ("status", "lifts", "trails")
    core_ok = all(current.get(k) not in (None, [], {}) for k in core_fields)
    if not core_ok:
        restored = deepcopy(previous)
        for key in ("weather", "forecast", "carparks", "park", "webcam"):
            if current.get(key):
                restored[key] = current[key]
        restored["stale"] = True
        return restored
    if not current.get("forecast"):
        current["forecast"] = deepcopy(previous.get("forecast"))
    if not current.get("weather"):
        current["weather"] = deepcopy(previous.get("weather"))
    return current


def scrape_resort(name: str, previous: dict[str, Any] | None) -> dict[str, Any]:
    soup, html = fetch(URLS[name])
    fallback_soup = None
    if name == "remarkables":
        data = parse_remarkables(soup)
        source_name = "Official resort report"
    elif name == "coronetpeak":
        data = parse_snownz(name, soup)
        data["carparks"] = parse_carparks_strict(soup)
        source_name = "SnowNZ mountain report"
    else:
        # MetService's DOM scan (previous version) used an unanchored 'first
        # Open or Closed word anywhere on the page' regex for overall status,
        # which is very likely what caused resorts to show Open when fully
        # closed. Replaced with two sources, correctly prioritised this time:
        # SnowNZ's individual Cardrona/Treble Cone page (the SAME mechanism
        # already proven reliable for Coronet Peak — an explicit 'OPEN/CLOSED
        # \n Mountain status' label, not a guess, and its lifts_open/total has
        # matched the hardcoded name-list counts below exactly in every
        # confirmed-good run) is now the PRIMARY source for status and lift
        # count. OnTheSnow.com only fills in if SnowNZ's fetch fails
        # entirely, and is still used for the summary text either way — but
        # it must never be allowed to override an already-successful SnowNZ
        # reading, which is what the previous version did by accident (and
        # is the direct cause of resorts still showing the wrong open/closed
        # state after the last fix: OnTheSnow returned the same "Jul 13"
        # content on a second fetch just now, a day stale, and was
        # unconditionally overwriting a correct SnowNZ status with it).
        source_name = "SnowNZ mountain report"
        data = {"status": None, "lifts_open": None, "lifts_total": None,
                "base_lower": None, "base_upper": None, "new_snow_7d": None,
                "terrain": {}, "lifts": [], "trails": [], "park": [], "carparks": []}
        try:
            fallback_soup, _ = fetch(FALLBACK_URLS[name])
            data["carparks"] = parse_carparks_strict(fallback_soup)
            summary = parse_summary_snownz(fallback_soup)
            for key in ("status", "lifts_open", "lifts_total", "base_lower", "base_upper", "new_snow_7d"):
                if data.get(key) is None: data[key] = summary.get(key)
        except Exception as exc:
            print(f"SnowNZ fallback failed for {name}: {exc}", file=sys.stderr)

        onthesnow = scrape_onthesnow(name)
        known_total = len(KNOWN_LIFTS[name])
        if data.get("status") is None and onthesnow and onthesnow.get("lifts_open") is not None:
            # SnowNZ gave us nothing this run — OnTheSnow as last resort.
            source_name = "OnTheSnow / OpenSnow (SnowNZ unavailable this run)"
            data["lifts_open"] = min(onthesnow["lifts_open"], known_total)
            data["lifts_total"] = known_total
            data["status"] = "Closed" if data["lifts_open"] == 0 else "Open"
        lifts_open = data.get("lifts_open")
        # Only populate the hardcoded name lists when there's an actual status
        # signal — otherwise leave them empty so validate_resort() correctly
        # detects a total scrape failure rather than this always appearing
        # 'valid' just because the hardcoded names are always there.
        if data.get("status") is not None:
            data["lifts"] = apply_uniform_status(KNOWN_LIFTS[name], lifts_open, known_total)
            data["trails"] = [
                {**item, "difficulty": None}  # None (not "unclassified") signals: render flat, no difficulty grouping
                for item in apply_uniform_status(TERRAIN_ZONES[name], lifts_open, known_total)
            ]
            data["park"] = apply_uniform_status(PARK_FEATURES[name], lifts_open, known_total)
        if onthesnow and onthesnow.get("base_cm") and data.get("base_upper") is None:
            data["base_upper"] = data["base_lower"] = onthesnow["base_cm"]
        if onthesnow and onthesnow.get("summary"):
            data["onthesnow_summary"] = onthesnow["summary"]

    # All four resorts now use the same weather source (Open-Meteo, with each
    # resort's real elevation for an accurate reading) rather than trying an
    # on-site reading per resort first — that meant each resort's weather
    # depended on a different page's text structure succeeding or failing
    # independently, which was part of why results looked inconsistent
    # across resorts. One source, same method, every time.
    try:
        data["weather"] = open_meteo_current_weather(name)
    except Exception as exc:
        print(f"Open-Meteo weather failed for {name}: {exc}", file=sys.stderr)
        data["weather"] = None
    data["trails"] = apply_trail_colours(data.get("trails", []))
    if data.get("onthesnow_summary"):
        data["summary"] = data.pop("onthesnow_summary")
    else:
        try:
            data["summary"] = find_summary_text(soup)
        except Exception as exc:
            print(f"Summary text extraction failed for {name}: {exc}", file=sys.stderr)
            data["summary"] = None
    try:
        webcam_soup, _ = fetch(WEBCAM_PAGES[name])
        webcam_image = find_webcam_image(webcam_soup, WEBCAM_PAGES[name])
    except Exception as exc:
        print(f"Mountainwatch webcam fetch failed for {name}: {exc}", file=sys.stderr)
        webcam_image = None
    data["webcam"] = {
        "page_url": WEBCAM_PAGES[name],
        "image_url": webcam_image,
        "embeddable": bool(webcam_image),
        "source": "Mountainwatch",
    }
    try:
        data["forecast"] = scrape_snow_forecast(SNOWFORECAST_URLS[name])
    except Exception as exc:
        print(f"Forecast fetch failed for {name}: {exc}", file=sys.stderr)
        data["forecast"] = forecast_stub(previous)
    data["stale"] = False
    return merge_fallback(name, data, previous)


def validate_resort(data: dict[str, Any]) -> bool:
    return bool(data.get("status") or data.get("lifts") or data.get("base_upper") is not None)


def main() -> int:
    previous_doc = load_previous()
    previous_resorts = previous_doc.get("resorts", {})
    resorts: dict[str, Any] = {}
    stale: list[str] = []
    for name in URLS:
        try:
            resort = scrape_resort(name, previous_resorts.get(name))
            if not validate_resort(resort):
                raise ValueError("no usable operational fields parsed")
            resort = apply_hours_backstop(name, resort)
        except Exception as exc:
            print(f"Scrape failed for {name}: {exc}", file=sys.stderr)
            resort = deepcopy(previous_resorts.get(name, {}))
            resort["stale"] = True
            stale.append(name)
        resorts[name] = resort

    now = datetime.now(timezone.utc)
    SCHEDULE_MINUTES = 5
    next_run = now.replace(second=0, microsecond=0)
    minutes_past = next_run.minute % SCHEDULE_MINUTES
    next_run += timedelta(minutes=(SCHEDULE_MINUTES - minutes_past) or SCHEDULE_MINUTES)
    usable = sum(validate_resort(r) for r in resorts.values())
    news = scrape_grasshopper_headline() or deepcopy(previous_doc.get("news"))
    output = {
        "updated": now.isoformat(),
        "news": news,
        "next_scheduled_update": next_run.isoformat(),
        "schedule_minutes": SCHEDULE_MINUTES,
        "health": {"usable_resorts": usable, "total_resorts": 4, "stale_resorts": stale},
        "resorts": resorts,
    }
    if usable == 0:
        raise RuntimeError("No usable resort data; refusing to overwrite data.json")
    DATA_FILE.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(output["health"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
