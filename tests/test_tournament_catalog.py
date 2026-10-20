import unittest
from unittest.mock import Mock

from wows_replay_batch.core import AppError, Tournament, TournamentClient
from wows_replay_batch.regions import (BUILTIN_CATALOG, CATALOG_URL, DEFAULT_SLUG, REGION_NAMES,
                                       TournamentEntry, catalog_labels, fetch_catalog, parse_catalog, saved_selection)


def row(slug="demo-cup", name="Demo Cup", regions=None):
    return {"public": True, "urlName": slug, "name": name, "allowedRegions": [0, 1, 3] if regions is None else regions}


class TournamentCatalogTests(unittest.TestCase):
    def test_api_regions_and_official_matches_urls(self):
        entries = parse_catalog([row(regions=[0, 1, 3, 5])])
        entry = entries[0]
        self.assertEqual(entry.regions, ("eu", "na", "apac", "int"))
        for code in entry.regions:
            url = entry.url(code)
            self.assertEqual(url, f"https://wows-tournaments.com/demo-cup/{code}/matches")
            self.assertEqual(Tournament.parse(url).headers, {"x-tournament-name": "demo-cup", "x-tournament-region": code.upper()})

    def test_international_only_event_never_offers_regional_servers(self):
        entry = parse_catalog([row(regions=[5])])[0]
        self.assertEqual(entry.regions, ("int",))
        self.assertEqual(entry.choose_region("apac"), "int")
        self.assertEqual(REGION_NAMES[entry.regions[0]], "国际赛区")
        with self.assertRaises(AppError):
            entry.url("apac")

    def test_preferred_region_is_preserved_or_asia_is_selected_when_available(self):
        entry = parse_catalog([row()])[0]
        self.assertEqual(entry.choose_region("eu"), "eu")
        self.assertEqual(entry.choose_region("int"), "apac")
        europe_only = parse_catalog([row(regions=[0])])[0]
        self.assertEqual(europe_only.choose_region("apac"), "eu")

    def test_private_events_are_excluded_and_api_order_is_retained(self):
        data = [row("first-cup"), {**row("private-cup"), "public": False}, row("second-cup")]
        self.assertEqual([entry.slug for entry in parse_catalog(data)], ["first-cup", "second-cup"])

    def test_duplicate_region_ids_are_collapsed(self):
        self.assertEqual(parse_catalog([row(regions=[3, 3, 0])])[0].regions, ("apac", "eu"))

    def test_malformed_catalogues_do_not_return_an_empty_or_partial_replacement(self):
        for value in ({}, [], [None], [{**row(), "allowedRegions": []}], [row(regions=[True])],
                      [row(regions=[999])], [row(regions="apac")], [{**row(), "name": ""}],
                      [{**row(), "name": "bad\nname"}], [{**row(), "urlName": "../outside"}],
                      [row(), row()], [{**row(), "public": False}]):
            with self.subTest(value=value), self.assertRaises(AppError):
                parse_catalog(value)

    def test_duplicate_titles_have_distinct_choices(self):
        entries = parse_catalog([row("first-cup"), row("second-cup")])
        labels = catalog_labels(entries)
        self.assertEqual(len(labels), 2)
        self.assertEqual({item.slug for item in labels.values()}, {"first-cup", "second-cup"})

    def test_refresh_calls_public_catalogue_without_tournament_or_identity_headers(self):
        network = Mock()
        network.json.return_value = [row()]
        self.assertEqual(fetch_catalog(network)[0].slug, "demo-cup")
        network.json.assert_called_once_with("GET", CATALOG_URL)

    def test_old_saved_asia_europe_and_america_urls_are_restored(self):
        for code in ("apac", "eu", "na"):
            original = f"https://wows-tournaments.com/{DEFAULT_SLUG}/{code}/matches"
            entries, selected, region = saved_selection(BUILTIN_CATALOG, original + "/")
            self.assertEqual(selected.url(region), original)
            self.assertEqual(len(entries), len(BUILTIN_CATALOG))

    def test_historical_international_selection_is_restored(self):
        original = "https://wows-tournaments.com/king-of-the-sea-xviii/int/matches"
        _, selected, region = saved_selection(BUILTIN_CATALOG, original)
        self.assertEqual(selected.url(region), original)
        self.assertEqual(selected.name, "King of the Sea XVIII")

    def test_saved_future_event_is_preserved_until_official_refresh_arrives(self):
        url = "https://wows-tournaments.com/future-demo-cup/na/matches"
        entries, selected, region = saved_selection(BUILTIN_CATALOG, url)
        self.assertEqual(selected.url(region), url)
        self.assertEqual(len(entries), len(BUILTIN_CATALOG) + 1)
        refreshed = parse_catalog([row("future-demo-cup", "Future Demo Cup")])
        entries, selected, region = saved_selection(refreshed, url)
        self.assertEqual(len(entries), 1)
        self.assertEqual(selected.name, "Future Demo Cup")
        self.assertEqual(selected.url(region), url)

    def test_invalid_saved_sources_use_offline_default(self):
        for url in ("", "https://other.invalid/demo/apac/matches", "https://[bad", "https://wows-tournaments.com/demo/unknown/matches"):
            _, selected, region = saved_selection(BUILTIN_CATALOG, url)
            self.assertEqual(selected.slug, DEFAULT_SLUG)
            self.assertEqual(region, "apac")

    def test_removed_region_is_replaced_with_an_available_one(self):
        entries = parse_catalog([row(regions=[5])])
        _, selected, region = saved_selection(entries, "https://wows-tournaments.com/demo-cup/apac/matches")
        self.assertEqual(region, "int")
        self.assertEqual(selected.regions, ("int",))

    def test_international_team_search_uses_correct_context_for_every_request(self):
        net = Mock()
        net.session.cookies.get.return_value = "anonymous-token"
        net.json.side_effect = [{}, {"items": [], "totalCount": 0}]
        client = TournamentClient(Tournament.parse("https://wows-tournaments.com/demo-cup/int/matches"), net)
        self.assertEqual(client.find_teams("DEMO"), [])
        self.assertEqual(net.json.call_args.kwargs["headers"]["x-tournament-region"], "INT")
        self.assertEqual(net.json.call_args.kwargs["headers"]["x-tournament-name"], "demo-cup")
