import copy
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

from wows_replay_batch.core import Options, mp4_valid
from wows_replay_batch.drafts import game_overlay, normalize_draft, statistics
from wows_replay_batch.video import extract_tracks, fingerprint, map_space_size, normalized_pixel


def data():
    match = {"id": 1234, "hasVote": True, "stageShipBanCount": 2, "stageMapVotes": True,
             "matchOpponents": [{"id": 10, "signup": {"teamTag": "DEMO"}}, {"id": 20, "signup": {"teamTag": "RIVAL"}}],
             "matchGames": [{"id": 101, "number": 1, "mapId": 14, "mapName": "Land of Fire"},
                            {"id": 102, "number": 2, "mapId": 27, "mapName": "Loop"}]}
    games = {101: {"report2": {"opponentId": 10, "players": [{"user": {"id": 1, "name": "Player A"}, "shipName": "Stalingrad"}]},
                   "report1": {"opponentId": 20, "players": [{"user": {"id": 2, "name": "Player B"}, "shipName": "Napoli"}]}},
             102: {"report1": None, "report2": None}}
    vote = {"complete": True, "shipBans": [{"opponentId": 10, "shipId": 1, "shipName": "Delny", "order": 1, "voteMapId": None},
                                           {"opponentId": 20, "shipId": 2, "shipName": "Brisbane", "order": 2, "voteMapId": None}],
            "bannedMaps": [{"id": 1, "mapId": 23, "mapName": "Okinawa", "opponentId": 10, "order": 1}],
            "pickedMaps": [{"id": 7, "mapId": 14, "mapName": "Land of Fire", "opponentId": 10, "order": 2, "shipBans": []},
                           {"id": 8, "mapId": 27, "mapName": "Loop", "opponentId": None, "teamTag": "DEMO (Random)", "order": 3}]}
    return match, games, vote


class DraftTests(unittest.TestCase):
    def test_ban_actor_is_preserved_in_video_caption(self):
        draft = normalize_draft(*data())
        overlay = game_overlay(draft, 101)
        self.assertEqual(overlay["teams"], [{"tag": "DEMO", "bans": ["Delny"]}, {"tag": "RIVAL", "bans": ["Brisbane"]}])

    def test_report_order_does_not_swap_actual_ship_lineups(self):
        draft = normalize_draft(*data())
        self.assertEqual(draft["games"][0]["teams"][0]["players"][0]["ship"], "Stalingrad")
        self.assertEqual(draft["games"][0]["teams"][1]["players"][0]["ship"], "Napoli")

    def test_automatic_map_is_not_attributed_to_the_named_team(self):
        draft = normalize_draft(*data())
        self.assertEqual(draft["map_events"][-1]["actor"], "公共 / 自动")
        counts = statistics([draft], "DEMO")
        self.assertEqual(dict(counts["地图 PICK"]), {"Land of Fire": 1})

    def test_per_map_ban_does_not_leak_into_other_games(self):
        match, games, vote = data()
        vote["pickedMaps"][0]["shipBans"] = [{"opponentId": 10, "shipId": 3, "shipName": "Kléber", "voteMapId": 7}]
        draft = normalize_draft(match, games, vote)
        self.assertIn("Kléber", game_overlay(draft, 101)["teams"][0]["bans"])
        self.assertNotIn("Kléber", game_overlay(draft, 102)["teams"][0]["bans"])

    def test_flat_and_nested_per_map_bans_are_counted_once(self):
        match, games, vote = data()
        ban = {"opponentId": 10, "shipId": 3, "shipName": "Kléber", "voteMapId": 7}
        vote["shipBans"].append(ban)
        vote["pickedMaps"][0]["shipBans"] = [ban]
        counts = statistics([normalize_draft(match, games, vote)], "DEMO")
        self.assertEqual(counts["禁船"]["Kléber"], 1)

    def test_unplayed_game_does_not_invent_picks(self):
        draft = normalize_draft(*data())
        self.assertFalse(draft["games"][1]["played"])
        self.assertEqual(draft["games"][1]["teams"][0]["players"], [])
        self.assertEqual(dict(statistics([draft], "DEMO")["实际出场舰船"]), {"Stalingrad": 1})

    def test_qualifier_without_voting_is_explicit(self):
        match, games, _ = data()
        match.update(hasVote=False, stageShipBanCount=0, stageMapVotes=False)
        draft = normalize_draft(match, games)
        self.assertFalse(draft["ship_bans_enabled"])
        self.assertFalse(game_overlay(draft, 101)["enabled"])

    def test_missing_vote_still_preserves_report_bans(self):
        match, games, _ = data()
        games[101]["shipBans1"] = [{"opponentId": 10, "shipId": 7, "shipName": "Delny"}]
        draft = normalize_draft(match, games, None, "temporary error")
        self.assertEqual(game_overlay(draft, 101)["teams"][0]["bans"], ["Delny"])
        self.assertTrue(draft["vote_error"])


class TrailTests(unittest.TestCase):
    def test_minimap_origin_matches_renderer_pixel_center(self):
        stored_zero = 1023.5 / 512 - 1.5
        x, y = normalized_pixel({"x": stored_zero, "y": stored_zero}, 1600)
        self.assertAlmostEqual(x, 384)
        self.assertAlmostEqual(y, 416)

    def test_map_size_excludes_renderer_border_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            build = base / "15.9_1"
            build.mkdir()
            xml = b'<space.settings><bounds minX="-10" maxX="9" minY="-10" maxY="9"/></space.settings>'
            h = hashlib.sha256(xml).hexdigest()[:20]
            obj = base / "common" / h[:2] / h[2:]
            obj.parent.mkdir(parents=True)
            obj.write_bytes(xml)
            (build / "metadata.toml").write_text(f'[files]\n"spaces/test/space.settings" = "{h}"\n', "utf-8")
            self.assertEqual(map_space_size(build, "spaces/test"), 1600)

    def test_fog_of_war_and_long_gaps_break_trails(self):
        zero = 1023.5 / 512 - 1.5
        def update(clock, **kw):
            return {"clock": clock, "payload": {"MinimapUpdate": {"updates": [{"entity_id": 5, "position": {"x": zero, "y": zero}, **kw}]}}}
        packets = [update(1), update(2), update(3, disappearing=True), update(4), update(12)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "packets.jsonl"
            path.write_text("\n".join(json.dumps(p) for p in packets), "utf-8")
            result = extract_tracks(path, 1600)
            self.assertEqual([p[3] for p in result["tracks"]["5"]], [False, True, False, False])

    def test_start_clock_uses_first_battle_stage_zero(self):
        packets = [{"clock": t, "payload": {"EntityProperty": {"property": "battleStage", "value": 0}}} for t in [31.5, 60]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "packets.jsonl"
            path.write_text("\n".join(json.dumps(p) for p in packets), "utf-8")
            self.assertEqual(extract_tracks(path, 1600)["battle_start"], 31.5)

    def test_selected_ids_distinguish_two_instances_of_same_ship(self):
        job = {"report_id": 10, "draft": {}}
        a = Options(trail_mode="selected", trail_selections={"10": [100]})
        b = Options(trail_mode="selected", trail_selections={"10": [101]})
        self.assertNotEqual(fingerprint(a, job)[0], fingerprint(b, job)[0])
        self.assertEqual(fingerprint(a, job)[2], [100])

    def test_empty_selected_configuration_does_not_fall_back_to_all(self):
        result = fingerprint(Options(trail_mode="selected"), {"report_id": 10})
        self.assertEqual(result[1:], ("selected", []))

    def test_draft_change_invalidates_only_visible_ban_caption(self):
        a, b = {"report_id": 1, "draft": {"teams": ["a"]}}, {"report_id": 1, "draft": {"teams": ["b"]}}
        self.assertNotEqual(fingerprint(Options(), a)[0], fingerprint(Options(), b)[0])
        self.assertEqual(fingerprint(Options(show_bans=False), a)[0], fingerprint(Options(show_bans=False), b)[0])

    def test_faststart_mp4_larger_than_eight_megabytes_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "faststart.mp4"
            with path.open("wb") as f:
                f.write(struct.pack(">I4s", 16, b"ftyp") + b"isom0000")
                f.write(struct.pack(">I4s", 16, b"moov") + b"index000")
                f.write(struct.pack(">I4s", 9 * 1024 * 1024, b"mdat"))
                f.seek(9 * 1024 * 1024 + 31)
                f.write(b"\x00")
            self.assertTrue(mp4_valid(path))


if __name__ == "__main__":
    unittest.main()
