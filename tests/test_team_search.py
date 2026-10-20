import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from wows_replay_batch.core import AmbiguousTeam, AppError, Options, Runner, Tournament, TournamentClient, team_search_score


def match(mid, tag, name=None, signup_id=None):
    return {"id": mid, "scheduledTime": f"2026-10-{mid:02}", "matchOpponents": [
        {"id": mid * 10, "signup": {"id": signup_id or mid, "teamTag": tag, "teamName": name or f"[{tag}] Team"}},
        {"id": mid * 10 + 1, "signup": {"id": 999, "teamTag": "OPP", "teamName": "Opponent"}}]}


class FuzzySearchTests(unittest.TestCase):
    def client(self, rows, extra_pages=None):
        net = Mock()
        net.session.cookies.get.return_value = "anonymous-token"
        net.json.side_effect = [{}, {"items": rows, "totalCount": len(rows)}] + (extra_pages or [])
        return TournamentClient(Tournament("king-of-the-sea-xix", "apac"), net)

    def test_partial_tag_resolves_decorated_tag(self):
        client = self.client([match(1, "-SAMPLE-")])
        self.assertEqual([m["id"] for m in client.search("sample")], [1])
        self.assertEqual(client.resolved_team, "-SAMPLE-")

    def test_chinese_partial_name_and_fullwidth_characters(self):
        signup = {"teamTag": "DEMO", "teamName": "[DEMO] 示例测试队"}
        self.assertEqual(team_search_score(signup, "测试"), 1)
        self.assertEqual(team_search_score(signup, "ＤＥＭＯ"), 2)
        client = self.client([match(1, "DEMO", "[DEMO] 示例测试队")])
        self.assertEqual(client.find_teams("测试")[0]["tag"], "DEMO")

    def test_ambiguous_keyword_returns_separate_candidates_without_mixing(self):
        client = self.client([match(1, "DEMO"), match(2, "PDEMO")])
        with self.assertRaises(AmbiguousTeam) as cm:
            client.search("dem")
        candidates = cm.exception.candidates
        self.assertEqual([c["tag"] for c in candidates], ["DEMO", "PDEMO"])
        self.assertEqual([[m["id"] for m in c["matches"]] for c in candidates], [[1], [2]])

    def test_exact_tag_wins_over_longer_tags_and_decorated_variants(self):
        client = self.client([match(1, "DEMO"), match(2, "PDEMO"), match(3, "-DEMO-")])
        self.assertEqual([m["id"] for m in client.search("DEMO")], [1])

    def test_candidate_collects_all_matches_for_same_signup(self):
        client = self.client([match(1, "DEMO", signup_id=123), match(2, "DEMO", signup_id=123)])
        candidates = client.find_teams("dem")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(candidates[0]["matches"]), 2)
        self.assertEqual(candidates[0]["signup_id"], 123)

    def test_symbol_only_keyword_is_rejected_before_network(self):
        client = self.client([])
        with self.assertRaises(AppError):
            client.find_teams("---")
        client.net.json.assert_not_called()

    def test_bracketed_partial_query_can_retry_without_decoration(self):
        client = self.client([], [{}, {"items": [match(1, "DEMO")], "totalCount": 1}])
        self.assertEqual(client.search("[DEM]")[0]["id"], 1)
        self.assertEqual(client.net.json.call_args.kwargs["json"]["team"], "dem")

    def test_resolved_team_uses_canonical_output_folder(self):
        with tempfile.TemporaryDirectory() as d:
            runner = Runner(Options(team="sample", output=d), root=d)
            runner.client = self.client([match(1, "-SAMPLE-")])
            runner.search()
            self.assertEqual(runner.options.team, "-SAMPLE-")
            self.assertEqual(runner.output.name, "-SAMPLE-")
            self.assertEqual(runner.output.parent, (Path(d) / "king-of-the-sea-xix/apac").resolve())


if __name__ == "__main__":
    unittest.main()
