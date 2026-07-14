"""Southern Lakes Snow Watch scraper.

Collects resort operations, snowfall, parking, compact current weather and
webcam references. Failed fields preserve their previous valid values rather
than replacing them with misleading zeroes.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
}
TIMEOUT = 25

URLS = {
    "remarkables": "https://www.theremarkables.co.nz/weather-report/",
    "coronetpeak": "https://www.snow.nz/area/nz/queenstown/coronetpeak/",
    "cardrona": "https://www.snow.nz/area/nz/wanaka/cardrona/",
    "treblecone": "https://www.snow.nz/area/nz/wanaka/treble-cone/",
}
FORECAST_URLS = {
    "remarkables": "https://www.snow-forecast.com/resorts/Remarkables/6day/mid",
    "coronetpeak": "https://www.snow-forecast.com/resorts/Coronet-Peak/6day/mid",
    "cardrona": "https://www.snow-forecast.com/resorts/Cardrona/6day/mid",
    "treblecone": "https://www.snow-forecast.com/resorts/Treble-Cone/6day/mid",
}
ACCUWEATHER_URLS = {
    "remarkables": "https://www.accuweather.com/en/nz/the-remarkables/2524446/weather-forecast/2524446",
    "coronetpeak": "https://www.accuweather.com/en/nz/coronet-peak-ski-resort/67336_poi/weather-forecast/67336_poi",
    "cardrona": "https://www.accuweather.com/en/nz/cardrona-alpine-resort/64922_poi/weather-forecast/64922_poi",
    "treblecone": "https://www.accuweather.com/en/nz/treble-cone-ski-area/64923_poi/weather-forecast/64923_poi",
}
WEBCAM_PAGES = {
    "remarkables": "https://www.theremarkables.co.nz/weather-report/",
    "coronetpeak": "https://www.coronetpeak.co.nz/weather-report",
    "cardrona": "https://cardrona-treblecone.com/webcams",
    "treblecone": "https://cardrona-treblecone.com/webcams",
}

STATUS_PHRASES = [
    "Advanced Riders Only", "Ungroomed fun bumps", "Ski Patrol Only",
    "Wind Hold", "Weather Hold", "On Hold", "Opens Tuesday", "Variable",
    "Limited", "Filling", "Spaces Available", "Available", "Full", "Open", "Closed",
]

CORONET_DIFFICULTY = {
    # Green
    "big easy": "green", "little easy": "green", "gentle annie": "green",
    "fun zone": "green", "easy rider": "green", "dual carpet slopes": "green",
    # Blue
    "m1": "blue", "shirt front": "blue", "brough's lane": "blue",
    "million dollar": "blue", "pro am": "blue", "carpet rail garden": "blue",
    # Red / advanced
    "greengates bumps": "red", "wall street": "red", "eighth basin": "red",
    "mid gully": "red", "overrun": "red", "west gates": "red", "sugar's run": "red",
    # Black
    "the chimney": "black", "the hurdle": "black", "exchange drop": "black",
    "tuck": "black", "upper walkabout": "black", "rocky return": "black",
    "race arena": "black", "walkabout park": "black",
    # Double black
    "powder run": "double-black", "back bowls": "double-black",
}


def fetch(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response


def soup_and_text(url: str) -> tuple[BeautifulSoup, str]:
    soup = BeautifulSoup(fetch(url).text, "html.parser")
    return soup, soup.get_text("\n", strip=True)


def integer_before_label(text: str, label: str) -> int | None:
    patterns = [
        rf"(\d+)\s*cm\s*\n+\s*{re.escape(label)}",
        rf"{re.escape(label)}\s*\n+\s*(\d+)\s*cm",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return int(match.group(1))
    return None


def clean_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" :-\n")
    value = re.sub(r"^(?:N/A|Status)\s+", "", value, flags=re.I)
    return value


def extract_status_items(block: str, max_name_len: int = 70) -> list[dict[str, str]]:
    anchor = "|".join(re.escape(x) for x in sorted(STATUS_PHRASES, key=len, reverse=True))
    matches = list(re.finditer(rf"\b({anchor})\b", block, re.I))
    items: list[dict[str, str]] = []
    previous = 0
    for match in matches:
        name = clean_name(block[previous:match.start()])
        if name and len(name) <= max_name_len:
            items.append({"name": name, "status": match.group(1).title()})
        previous = match.end()
    return dedupe_items(items)


def dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output = []
    for item in items:
        key = re.sub(r"\W+", "", str(item.get("name", "")).lower())
        if key and key not in seen:
            seen.add(key)
            output.append(item)
    return output


def section(text: str, starts: list[str], stops: list[str], limit: int = 6000) -> str:
    positions = [(text.find(marker), marker) for marker in starts if text.find(marker) >= 0]
    if not positions:
        return ""
    start, marker = min(positions)
    start += len(marker)
    end = min([p for p in (text.find(stop, start) for stop in stops) if p >= 0] or [start + limit])
    return text[start:end]


def parse_carparks(text: str, all_headers: list[str]) -> list[dict[str, str]]:
    block = section(text, ["Car Parks :", "Car Parks", "Carparks"], all_headers)
    if not block:
        return []
    items = extract_status_items(block)
    # Some feeds provide one overall status rather than named car parks.
    if not items:
        match = re.search(r"\b(Spaces Available|Available|Filling|Limited|Full|Closed|Open)\b", block, re.I)
        if match:
            items = [{"name": "Car parks", "status": match.group(1).title()}]
    return items


def parse_facilities(text: str, headers: list[str], stops: list[str]) -> list[dict[str, str]]:
    block = section(text, headers, stops)
    return extract_status_items(block) if block else []


def parse_webcam_preview(page_url: str) -> dict[str, Any]:
    result = {"page_url": page_url, "image_url": None, "embeddable": False}
    try:
        soup = BeautifulSoup(fetch(page_url).text, "html.parser")
        meta = soup.select_one('meta[property="og:image"], meta[name="twitter:image"]')
        if meta and meta.get("content"):
            result["image_url"] = urljoin(page_url, meta["content"])
        # A direct image is safer than an iframe; resort pages frequently block framing.
        for image in soup.find_all("img"):
            source = image.get("src") or image.get("data-src") or image.get("data-lazy-src")
            alt = (image.get("alt") or "").lower()
            if source and ("webcam" in alt or "snow cam" in alt or "camera" in alt):
                result["image_url"] = urljoin(page_url, source)
                break
    except Exception as exc:
        result["error"] = str(exc)
    return result


def parse_accuweather(url: str) -> dict[str, Any] | None:
    try:
        soup, text = soup_and_text(url)
    except Exception as exc:
        print(f"  ! AccuWeather failed: {exc}", file=sys.stderr)
        return None

    weather: dict[str, Any] = {"source": "AccuWeather", "source_url": url}
    temp = None
    for selector in (".display-temp", ".cur-con-weather-card__panel .temp", ".temp"):
        node = soup.select_one(selector)
        if node:
            match = re.search(r"-?\d+", node.get_text(" ", strip=True))
            if match:
                temp = int(match.group())
                break
    if temp is None:
        match = re.search(r"(?:Currently|Current Weather).*?(-?\d+)°", text, re.I | re.S)
        if match:
            temp = int(match.group(1))
    weather["temperature_c"] = temp

    phrase = None
    for selector in (".phrase", ".cur-con-weather-card__panel .phrase", ".current-weather-info .phrase"):
        node = soup.select_one(selector)
        if node:
            phrase = clean_name(node.get_text(" ", strip=True))
            if phrase:
                break
    weather["condition"] = phrase

    realfeel = re.search(r"RealFeel®?\s*(-?\d+)°", text, re.I)
    weather["feels_like_c"] = int(realfeel.group(1)) if realfeel else None
    wind = re.search(r"Wind\s+([A-Z]{1,3})\s+(\d+)\s*km/h", text, re.I)
    if wind:
        weather["wind"] = f"{wind.group(1).upper()} {wind.group(2)} km/h"

    return weather if any(weather.get(k) is not None for k in ("temperature_c", "condition", "feels_like_c")) else None


def scrape_remarkables() -> dict[str, Any]:
    output: dict[str, Any] = blank_resort()
    soup, text = soup_and_text(URLS["remarkables"])

    status = re.search(r"Mountain Status\s*\n+\s*(Open|Closed)", text, re.I)
    if status:
        output["status"] = status.group(1).title()
    lifts = re.search(r"Lift Status\s*\n+\s*(\d+)\s*/\s*(\d+)\s*Open", text, re.I)
    if lifts:
        output["lifts_open"], output["lifts_total"] = map(int, lifts.groups())
    base = re.search(r"Snow Base\s*\n+\s*(\d+)\s*-\s*(\d+)\s*cm", text, re.I)
    if base:
        output["base_lower"], output["base_upper"] = map(int, base.groups())

    for key, label in (("novice", "Novice"), ("intermediate", "Intermediate"),
                       ("advanced", "Advanced"), ("expert", "Expert"),
                       ("extreme", "Extreme")):
        match = re.search(rf"(\d{{1,3}})%\s*OPEN\s*\n+\s*{label}", text, re.I)
        if match:
            output["terrain"][key] = int(match.group(1))
        elif re.search(rf"CLOSED\s*\n+\s*{label}", text, re.I):
            output["terrain"][key] = 0

    output["lifts"] = parse_facilities(text, ["Lifts\n", "Lifts"], ["Terrain\n", "Terrain"])
    terrain_block = section(text, ["Terrain\n", "Terrain"], ["Road Conditions", "Webcams", "Getting Here"])
    named = extract_status_items(terrain_block, 85)
    difficulty_labels = {"Novice", "Intermediate", "Advanced", "Expert", "Extreme"}
    output["trails"] = [item for item in named if item["name"] not in difficulty_labels and "%" not in item["name"]]
    output["carparks"] = parse_carparks(text, ["Lifts", "Terrain", "Road", "Webcams", "Car Parks"])
    output["new_snow_7d"] = integer_before_label(text, "Last 7 Days")
    output["webcam"] = parse_webcam_preview(WEBCAM_PAGES["remarkables"])
    output["weather"] = parse_accuweather(ACCUWEATHER_URLS["remarkables"])
    return output


def blank_resort() -> dict[str, Any]:
    return {
        "status": None, "lifts_open": None, "lifts_total": None,
        "base_lower": None, "base_upper": None, "new_snow_7d": None,
        "terrain": {}, "lifts": [], "trails": [], "carparks": [],
        "weather": None, "webcam": None,
    }


def scrape_snownz(name: str, lift_headers: list[str], trail_headers: list[str]) -> dict[str, Any]:
    output = blank_resort()
    _, text = soup_and_text(URLS[name])
    headers = ["Road :", "All Lifts :", "Chair Lifts :", "Facilities :", "Car Parks :",
               "Trails :", "Zones :", "Parks :", "Snow report", "Webcams"]

    if re.search(r"OPEN\s*\n+\s*Mountain status", text, re.I):
        output["status"] = "Open"
    elif re.search(r"CLOSED\s*\n+\s*Mountain status", text, re.I):
        output["status"] = "Closed"
    lifts = re.search(r"(\d+)\s*/\s*(\d+)\s*\n+\s*Lifts open", text, re.I)
    if lifts:
        output["lifts_open"], output["lifts_total"] = map(int, lifts.groups())
    output["base_upper"] = integer_before_label(text, "Snow base (upper)")
    output["base_lower"] = integer_before_label(text, "Snow base (lower)")
    output["new_snow_7d"] = integer_before_label(text, "Last 7 Days")
    output["lifts"] = parse_facilities(text, lift_headers, headers)

    trails: list[dict[str, Any]] = []
    for heading in trail_headers:
        trails.extend(parse_facilities(text, [heading], headers))
    trails = dedupe_items(trails)
    if name == "coronetpeak":
        for trail in trails:
            trail["difficulty"] = CORONET_DIFFICULTY.get(trail["name"].lower(), "unclassified")
    output["trails"] = trails
    output["carparks"] = parse_carparks(text, headers)
    output["webcam"] = parse_webcam_preview(WEBCAM_PAGES[name])
    output["weather"] = parse_accuweather(ACCUWEATHER_URLS[name])
    return output


def scrape_forecast(url: str) -> dict[str, Any] | None:
    try:
        soup = BeautifulSoup(fetch(url).text, "html.parser")
    except Exception as exc:
        print(f"  ! forecast failed: {exc}", file=sys.stderr)
        return None
    text = soup.get_text(" ", strip=True)
    issued_match = re.search(r"Issued:\s*([^|]{5,50}?\d{4})", text, re.I)
    issued = clean_name(issued_match.group(1)) if issued_match else None

    day_spans: list[tuple[str, int]] = []
    for row in soup.find_all("tr"):
        candidate = []
        for cell in row.find_all(["td", "th"]):
            match = re.match(r"([A-Za-z]+)\s+(\d{1,2})$", cell.get_text(" ", strip=True))
            if match:
                candidate.append((f"{match.group(1)[:3]} {match.group(2)}", int(cell.get("colspan", 1))))
        if len(candidate) >= 4:
            day_spans = candidate[:6]
            break
    if not day_spans:
        return None

    def value_row(label: str) -> list[int | None]:
        for row in soup.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if cells and cells[0].get_text(" ", strip=True).lower() == label:
                values = []
                for cell in cells[1:]:
                    match = re.search(r"\d+", cell.get_text(" ", strip=True))
                    values.append(int(match.group()) if match else None)
                return values
        return []

    cms, mms = value_row("cm"), value_row("mm")
    if not cms:
        return None
    days, index = [], 0
    for label, span in day_spans:
        snow = [x for x in cms[index:index + span] if x is not None]
        rain = [x for x in mms[index:index + span] if x is not None]
        index += span
        cm, mm = sum(snow), sum(rain)
        days.append({"label": label, "cm": cm, "mm": mm, "rain": mm > 0 and cm == 0})
    return {"issued": issued, "days": days}


def load_previous(path: str = "data.json") -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def merge_previous(name: str, current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    old = (previous.get("resorts") or {}).get(name) or {}
    # Preserve individual failed fields, but never manufacture zero.
    for field in ("status", "lifts_open", "lifts_total", "base_lower", "base_upper", "new_snow_7d",
                  "terrain", "lifts", "trails", "carparks", "weather", "webcam", "forecast"):
        empty = current.get(field) is None or current.get(field) == [] or current.get(field) == {}
        if empty and old.get(field) not in (None, [], {}):
            current[field] = old[field]
            current.setdefault("stale_fields", []).append(field)
    current["stale"] = not any(current.get(field) not in (None, [], {}) for field in ("status", "base_upper", "lifts"))
    return current


def write_json(path: str, data: dict[str, Any]) -> None:
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temp, path)


def main() -> None:
    previous = load_previous()
    scrapers = {
        "remarkables": scrape_remarkables,
        "coronetpeak": lambda: scrape_snownz("coronetpeak", ["All Lifts :"], ["Trails :", "Zones :", "Parks :"]),
        "cardrona": lambda: scrape_snownz("cardrona", ["Chair Lifts :"], ["Trails :", "Parks :"]),
        "treblecone": lambda: scrape_snownz("treblecone", ["Chair Lifts :"], ["Trails :"]),
    }
    resorts: dict[str, Any] = {}
    for name, scraper in scrapers.items():
        print(f"Scraping {name}...")
        try:
            resort = scraper()
        except Exception as exc:
            print(f"  ! {name} operational scrape failed: {exc}", file=sys.stderr)
            resort = blank_resort()
            resort["scrape_error"] = str(exc)
        resort["forecast"] = scrape_forecast(FORECAST_URLS[name])
        resorts[name] = merge_previous(name, resort, previous)

    usable = sum(bool(r.get("status") or r.get("base_upper") is not None or r.get("lifts")) for r in resorts.values())
    if usable == 0:
        raise RuntimeError("No usable resort data was produced; existing data.json retained.")

    now = datetime.now(timezone.utc)
    data = {
        "updated": now.isoformat(),
        "next_scheduled_update": now.replace(minute=17, second=0, microsecond=0).isoformat(),
        "schedule_minutes": 60,
        "health": {
            "usable_resorts": usable,
            "total_resorts": len(resorts),
            "stale_resorts": [name for name, resort in resorts.items() if resort.get("stale")],
        },
        "resorts": resorts,
    }
    write_json("data.json", data)
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
