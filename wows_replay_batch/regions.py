"""Selectable tournament regions, shared by the download form and tests."""

REGIONS = {
    "亚服": "https://wows-tournaments.com/king-of-the-sea-xix/apac/matches",
    "欧服": "https://wows-tournaments.com/king-of-the-sea-xix/eu/matches",
    "美服": "https://wows-tournaments.com/king-of-the-sea-xix/na/matches",
}


def region_for_url(url):
    return next((name for name, value in REGIONS.items() if value == str(url).strip().rstrip("/")), "亚服")
