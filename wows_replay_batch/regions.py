"""Public tournament catalogue and region URLs used by the download form."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from .core import AppError

HOME = "https://wows-tournaments.com"
CATALOG_URL = HOME + "/api/tournament/list"
DEFAULT_SLUG = "king-of-the-sea-xix"
REGION_IDS = {0: "eu", 1: "na", 3: "apac", 5: "int"}
REGION_NAMES = {"apac": "亚服", "eu": "欧服", "na": "美服", "int": "国际赛区"}
REGIONS = {name: f"{HOME}/{DEFAULT_SLUG}/{code}/matches" for code, name in REGION_NAMES.items() if code != "int"}


@dataclass(frozen=True)
class TournamentEntry:
    slug: str
    name: str
    regions: tuple[str, ...]

    def url(self, region):
        if region not in self.regions:
            raise AppError("所选赛事没有此服务器，请重新选择。")
        return f"{HOME}/{self.slug}/{region}/matches"

    def choose_region(self, preferred):
        return preferred if preferred in self.regions else "apac" if "apac" in self.regions else self.regions[0]


# Public homepage snapshot for offline startup; refreshed from the official API.
BUILTIN_CATALOG = (
    TournamentEntry(DEFAULT_SLUG, "King of the Sea XIX", ("eu", "na", "apac", "int")),
    TournamentEntry("warship-masters-2026", "Warship Masters 2026 - Internationals", ("int",)),
    TournamentEntry("warship-masters-2026-regionals", "Warship Masters 2026 Regionals", ("eu", "na", "apac")),
    TournamentEntry("king-of-the-sea-xviii", "King of the Sea XVIII", ("int",)),
    TournamentEntry("king-of-the-sea-xviii-qualifiers", "King of the Sea XVIII Qualifier", ("eu", "na", "apac")),
    TournamentEntry("warships-masters-2025", "Warships Masters 2025", ("int",)),
    TournamentEntry("trident-trials", "Trident Trials", ("eu", "na", "apac")),
    TournamentEntry("king-of-the-sea-xvii", "King of the Sea XVII", ("int",)),
    TournamentEntry("king-of-the-sea-xvii-qualifier", "King of the Sea XVII Qualifier", ("eu", "na", "apac")),
    TournamentEntry("warship-masters-2024", "Warship Masters 2024", ("int",)),
    TournamentEntry("beat-the-pro-2024", "Beat the Pro 2024", ("int",)),
    TournamentEntry("king-of-the-sea-xvi", "King of the Sea XVI", ("eu", "na", "apac", "int")),
    TournamentEntry("king-of-the-sea-xv", "King of the Sea XV", ("eu", "na", "apac", "int")),
    TournamentEntry("king-of-the-sea-xiv", "King of the Sea XIV", ("eu", "na", "apac", "int")),
)


def parse_catalog(data):
    if not isinstance(data, list) or len(data) > 1000:
        raise AppError("官网赛事列表格式无效，已保留现有列表。")
    result, seen = [], set()
    for row in data:
        if not isinstance(row, dict):
            raise AppError("官网赛事记录格式无效，已保留现有列表。")
        if row.get("public") is not True:
            continue
        slug, name, regions = row.get("urlName"), row.get("name"), row.get("allowedRegions")
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > 150 or slug in seen:
            raise AppError("官网赛事标识无效或重复，已保留现有列表。")
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or any(ord(c) < 32 for c in name):
            raise AppError("官网赛事名称无效，已保留现有列表。")
        if not isinstance(regions, list) or not regions or any(type(code) is not int or code not in REGION_IDS for code in regions):
            raise AppError("官网赛事赛区无法识别，已保留现有列表。请更新程序后重试。")
        result.append(TournamentEntry(slug, name.strip(), tuple(dict.fromkeys(REGION_IDS[code] for code in regions))))
        seen.add(slug)
    if not result:
        raise AppError("官网未返回可用赛事，已保留现有列表。")
    return result


def fetch_catalog(network):
    return parse_catalog(network.json("GET", CATALOG_URL))


def catalog_labels(entries):
    counts = {}
    for entry in entries:
        counts[entry.name] = counts.get(entry.name, 0) + 1
    return {entry.name if counts[entry.name] == 1 else f"{entry.name} [{entry.slug}]": entry for entry in entries}


def saved_selection(entries, url):
    entries = list(entries)
    try:
        parsed = urlsplit(str(url).strip())
    except ValueError:
        parsed = urlsplit("")
    parts = parsed.path.strip("/").split("/")
    valid = (parsed.scheme == "https" and parsed.netloc == "wows-tournaments.com" and not parsed.query and not parsed.fragment
             and len(parts) in (2, 3) and (len(parts) == 2 or parts[2] == "matches")
             and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", parts[0]) and parts[1] in REGION_NAMES)
    if valid:
        slug, region = parts[:2]
        entry = next((item for item in entries if item.slug == slug), None)
        if entry is None:
            entry = TournamentEntry(slug, slug + "（已保存赛事）", (region,))
            entries.append(entry)
        return entries, entry, entry.choose_region(region)
    entry = next((item for item in entries if item.slug == DEFAULT_SLUG), entries[0])
    return entries, entry, entry.choose_region("apac")


def region_for_url(url):
    parts = urlsplit(str(url).strip()).path.strip("/").split("/")
    return REGION_NAMES.get(parts[1] if len(parts) > 1 else "", "亚服")
