"""Queenstown Snow Watch scraper.

Uses DOM sections and list items rather than flattened page text. This prevents
adjacent lifts, trails, facilities and car parks being merged together.
"""
from __future__ import annotations

import json
import re
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

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
    "cardrona": "https://www.snow.nz/area/nz/wanaka/cardrona/",
    "treblecone": "https://www.snow.nz/area/nz/wanaka/treble-cone/",
}
WEATHER_URLS = {
    # Resort POIs where AccuWeather provides them; nearest named location otherwise.
    "remarkables": "https://www.accuweather.com/en/nz/queenstown/249932/current-weather/249932",
    "coronetpeak": "https://www.accuweather.com/en/nz/coronet-peak-ski-resort/67336_poi/current-weather/67336_poi",
    "cardrona": "https://www.accuweather.com/en/nz/cardrona-alpine-resort/64922_poi/current-weather/64922_poi",
    "treblecone": "https://www.accuweather.com/en/nz/wanaka/250069/current-weather/250069",
}
WEBCAM_PAGES = {
    "remarkables": URLS["remarkables"],
    "coronetpeak": "https://www.coronetpeak.co.nz/weather-report",
    "cardrona": "https://cardrona-treblecone.com/webcams",
    "treblecone": "https://cardrona-treblecone.com/webcams",
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

STATUS_PHRASES = sorted([
    "Wind may affect chairlift ops", "Advanced Riders Only", "Closed for Racing",
    "Ungroomed fun bumps", "Chains or 4WD", "Opening Delayed", "On Hold",
    "Wind Hold", "Limited Spaces", "Filling Fast", "Ungroomed", "Available",
    "Variable", "Spaces", "Full", "Open", "Closed",
], key=len, reverse=True)


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
    label = clean(label)
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
    text = soup.get_text("\n", strip=True)
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
    text = soup.get_text("\n", strip=True)
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


def parse_accuweather(soup: BeautifulSoup, html: str, source_url: str) -> dict[str, Any] | None:
    text = clean(soup.get_text(" "))
    # Prefer semantic current-condition selectors where present.
    current = soup.select_one(".current-weather-card, .cur-con-weather-card")
    scope = clean(current.get_text(" ")) if current else text
    temp = None
    for selector in (".temperature", ".temp", "[class*='temperature']"):
        node = current.select_one(selector) if current else soup.select_one(selector)
        if node:
            temp = parse_number(node.get_text(" "))
            if temp is not None: break
    if temp is None:
        match = re.search(r"Current Weather.*?(-?\d+)°\s*C?", scope, re.I)
        if not match:
            match = re.search(r"currently\s+.+?temperature of\s+(-?\d+)°", text, re.I)
        temp = parse_number(match.group(1)) if match else None

    condition = None
    node = current.select_one(".phrase") if current else soup.select_one(".phrase")
    if node: condition = clean(node.get_text(" "))
    if not condition:
        match = re.search(r"Current Weather.*?°\s*C?\s+RealFeel.*?°\s+([^\d]+?)\s+More Details", scope, re.I)
        if match: condition = clean(match.group(1))
        else:
            match = re.search(r"is currently ([A-Za-z ,&-]+?) with a temperature", text, re.I)
            if match: condition = clean(match.group(1))

    feels = None
    match = re.search(r"RealFeel(?:®)?\s*(-?\d+)°", scope, re.I)
    if match: feels = parse_number(match.group(1))
    wind = None
    match = re.search(r"Wind\s+([A-Z]{1,3}\s+\d+\s*km/h)", scope, re.I)
    if match: wind = clean(match.group(1))
    observed = None
    match = re.search(r"Current Weather\s+(\d{1,2}:\d{2}\s*[AP]M)", scope, re.I)
    if match: observed = match.group(1)
    if temp is None and not condition:
        return None
    return {
        "temperature_c": temp, "condition": condition, "feels_like_c": feels,
        "wind": wind, "observed": observed, "source": "AccuWeather", "source_url": source_url,
    }


def parse_snownz_weather(soup: BeautifulSoup, source_url: str) -> dict[str, Any] | None:
    text = soup.get_text("\n", strip=True)
    match = re.search(r"(-?\d+(?:\.\d+)?)°\s*([^\n]+)\s*\n\s*Weather", text, re.I)
    if not match:
        match = re.search(r"Weather:\s*\n\s*(-?\d+(?:\.\d+)?)°\s*([^\n]+)", text, re.I)
    if not match: return None
    return {"temperature_c": round(float(match.group(1))), "condition": clean(match.group(2)), "feels_like_c": None, "wind": None, "source": "SnowNZ fallback", "source_url": source_url}


def find_webcam_image(soup: BeautifulSoup, base_url: str) -> str | None:
    for img in soup.find_all("img"):
        attrs = " ".join([clean(img.get("alt")), clean(img.get("class") and " ".join(img.get("class"))), clean(img.get("src"))]).lower()
        if "webcam" not in attrs and "camera" not in attrs:
            continue
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if src and not any(x in src.lower() for x in ("logo", "icon", "hero")):
            return urljoin(base_url, src)
    return None


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


def merge_fallback(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if not previous:
        return current
    for key in ("status", "lifts_open", "lifts_total", "base_lower", "base_upper", "new_snow_7d", "weather"):
        if current.get(key) is None:
            current[key] = deepcopy(previous.get(key))
    for key in ("terrain", "lifts", "trails", "carparks"):
        if not current.get(key):
            current[key] = deepcopy(previous.get(key, current.get(key)))
    if not current.get("forecast"):
        current["forecast"] = deepcopy(previous.get("forecast"))
    return current


def scrape_resort(name: str, previous: dict[str, Any] | None) -> dict[str, Any]:
    soup, html = fetch(URLS[name])
    data = parse_remarkables(soup) if name == "remarkables" else parse_snownz(name, soup)
    try:
        weather_soup, weather_html = fetch(WEATHER_URLS[name])
        data["weather"] = parse_accuweather(weather_soup, weather_html, WEATHER_URLS[name])
    except Exception as exc:
        print(f"Weather fetch failed for {name}: {exc}", file=sys.stderr)
        data["weather"] = None
    if not data.get("weather") and name != "remarkables":
        data["weather"] = parse_snownz_weather(soup, URLS[name])
    data["webcam"] = {
        "page_url": WEBCAM_PAGES[name], "image_url": find_webcam_image(soup, URLS[name]), "embeddable": False,
    }
    try:
        data["forecast"] = scrape_snow_forecast(SNOWFORECAST_URLS[name])
    except Exception as exc:
        print(f"Forecast fetch failed for {name}: {exc}", file=sys.stderr)
        data["forecast"] = forecast_stub(previous)
    data["stale"] = False
    return merge_fallback(data, previous)


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
        except Exception as exc:
            print(f"Scrape failed for {name}: {exc}", file=sys.stderr)
            resort = deepcopy(previous_resorts.get(name, {}))
            resort["stale"] = True
            stale.append(name)
        resorts[name] = resort

    now = datetime.now(timezone.utc)
    next_hour = now.replace(minute=17, second=0, microsecond=0)
    if next_hour <= now:
        next_hour += timedelta(hours=1)
    usable = sum(validate_resort(r) for r in resorts.values())
    output = {
        "updated": now.isoformat(),
        "next_scheduled_update": next_hour.isoformat(),
        "schedule_minutes": 60,
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
