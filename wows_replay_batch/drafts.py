"""Normalize public tournament voting records without inferring draft actions."""
from __future__ import annotations

from collections import Counter


def team_label(opponent_id, teams, fallback=""):
    return next((t["tag"] for t in teams if t["id"] == opponent_id), fallback or "公共 / 自动")


def normalize_ban(item, teams, scope=None):
    return {"ship_id": item.get("shipId"), "ship": item.get("shipName") or "未知舰船",
            "actor_id": item.get("opponentId"), "actor": team_label(item.get("opponentId"), teams, item.get("teamTag", "")),
            "order": item.get("order"), "scope": item.get("voteMapId", scope)}


def normalize_draft(match, games, vote=None, vote_error=""):
    teams = [{"id": o["id"], "tag": (o.get("signup") or {}).get("teamTag") or "TBD",
              "name": (o.get("signup") or {}).get("teamName") or "TBD"} for o in match.get("matchOpponents", [])]
    raw_bans = [normalize_ban(b, teams) for b in (vote or {}).get("shipBans", [])]
    bans = [b for b in raw_bans if b["scope"] is None]
    events = []
    for field, kind in (("bannedMaps", "BAN"), ("pickedMaps", "PICK")):
        for item in (vote or {}).get(field, []):
            actor = team_label(item.get("opponentId"), teams)
            if item.get("static"):
                actor = "赛事指定"
            map_bans = [normalize_ban(b, teams, item.get("id")) for b in item.get("shipBans", [])]
            for ban in raw_bans:
                if ban["scope"] is not None and ban["scope"] == item.get("id") and not any((b["ship_id"], b["actor_id"]) == (ban["ship_id"], ban["actor_id"]) for b in map_bans):
                    map_bans.append(ban)
            events.append({"order": item.get("order"), "kind": kind, "map": item.get("mapName") or "未选择 / 超时",
                           "map_id": item.get("mapId"), "actor_id": item.get("opponentId"), "actor": actor,
                           "spawn": item.get("spawn"), "spawn_actor": team_label(item.get("spawnOpponentId"), teams),
                           "static": bool(item.get("static")),
                           "ship_bans": map_bans})
    events.sort(key=lambda e: (e["order"] is None, e["order"] or 0))
    normalized_games = []
    for short in match.get("matchGames", []):
        game = games.get(short["id"], {})
        lineups = []
        for team in teams:
            reports = [game.get("report1"), game.get("report2")]
            report = next((r for r in reports if r and r.get("opponentId") == team["id"]), None)
            players = [{"name": (p.get("user") or {}).get("name") or "未知玩家", "ship": p.get("shipName") or "未知舰船",
                        "account_id": (p.get("user") or {}).get("id")} for p in (report or {}).get("players", [])]
            applied = [normalize_ban(b, teams) for key in ("shipBans1", "shipBans2") for b in game.get(key, [])
                       if b.get("opponentId") == team["id"]]
            if not applied:
                # Global bans plus bans attached to this map. Never carry a map's
                # bans over to another game in the series.
                applicable = [b for b in bans if b["scope"] is None]
                for event in events:
                    if event["kind"] == "PICK" and event["map_id"] == short.get("mapId"):
                        applicable += event["ship_bans"]
                applied = [b for b in applicable if b["actor_id"] == team["id"]]
            lineups.append({**team, "players": players, "reported": report is not None, "bans": applied})
        normalized_games.append({"id": short["id"], "number": short.get("number", 1),
                                 "map": short.get("mapName") or next((r.get("mapName") for r in (game.get("report1"), game.get("report2")) if r), "未知地图"),
                                 "map_id": short.get("mapId"), "teams": lineups,
                                 "played": any(t["reported"] for t in lineups)})
    if not bans and not vote:
        seen = set()
        for game in normalized_games:
            for team in game["teams"]:
                for ban in team["bans"]:
                    key = (ban["actor_id"], ban["ship_id"])
                    if key not in seen:
                        seen.add(key)
                        bans.append(ban)
    return {"match_id": match["id"], "teams": teams, "stage": match.get("stageName", ""),
            "group": match.get("groupName", ""), "round": match.get("roundName", ""),
            "scheduled_time": match.get("scheduledTime", ""), "ship_bans": bans, "map_events": events,
            "games": normalized_games, "has_vote": bool(match.get("hasVote")),
            "vote_complete": bool((vote or {}).get("complete")), "vote_error": vote_error,
            "ship_bans_enabled": bool(match.get("stageShipBanCount", 0)),
            "map_votes_enabled": bool(match.get("stageMapVotes"))}


def game_overlay(draft, game_id):
    game = next(g for g in draft["games"] if g["id"] == game_id)
    return {"match_id": draft["match_id"], "game_id": game_id, "game_number": game["number"], "map": game["map"],
            "enabled": draft["ship_bans_enabled"], "error": draft["vote_error"],
            "teams": [{"tag": t["tag"], "bans": [b["ship"] for b in t["bans"]]} for t in game["teams"]]}


def statistics(drafts, team_tag):
    counts = {"禁船": Counter(), "地图 BAN": Counter(), "地图 PICK": Counter(), "实际出场舰船": Counter()}
    for draft in drafts:
        target = {t["id"] for t in draft["teams"] if t["tag"].casefold() == team_tag.casefold()}
        for ban in draft["ship_bans"]:
            if ban["actor_id"] in target:
                counts["禁船"][ban["ship"]] += 1
        for event in draft["map_events"]:
            if event["actor_id"] in target and not event["static"]:
                counts["地图 " + event["kind"]][event["map"]] += 1
            for ban in event["ship_bans"]:
                if ban["actor_id"] in target:
                    counts["禁船"][ban["ship"]] += 1
        for game in draft["games"]:
            for team in game["teams"]:
                if team["id"] in target:
                    counts["实际出场舰船"].update(p["ship"] for p in team["players"])
    return counts
