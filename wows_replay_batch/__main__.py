import argparse
import sys
import json
from pathlib import Path

from .core import AppError, Cancelled, Options, Runner, write_json


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--apply-update":
        from .install_update import helper_main
        helper_main(sys.argv[2])
        return
    parser = argparse.ArgumentParser(description="WoWS 队伍回放助手：按队名下载比赛回放并批量转换小地图视频")
    parser.add_argument("--update-health", help=argparse.SUPPRESS)
    parser.add_argument("--team", help="队伍名称或队标；不填写时打开桌面界面")
    parser.add_argument("--url", default=Options.url)
    parser.add_argument("--output", default="")
    parser.add_argument("--both", action="store_true", help="下载每局双方视角")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--convert-only", metavar="MANIFEST", help="仅转换本地 manifest.json 或其所在目录，不查询比赛或下载 REP")
    parser.add_argument("--report-id", type=int, action="append", help="仅转换指定回放编号，可重复填写")
    parser.add_argument("--perspective", choices=["all", "team", "opponent"], default="all", help="本地转换时筛选视角")
    parser.add_argument("--search-only", action="store_true")
    parser.add_argument("--game-dir", default="")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--no-trails", action="store_true")
    parser.add_argument("--no-bans", action="store_true", help="视频中不添加 BAN 面板")
    parser.add_argument("--ban-pick-only", action="store_true", help="只查询并保存 BAN/PICK 和阵容")
    parser.add_argument("--trail-mode", choices=["all", "selected", "none"], default="all")
    parser.add_argument("--trail-config", help="由舰船选择界面保存的 settings.json 或 report_id -> entity_id 列表 JSON")
    parser.add_argument("--match-id", type=int, help="仅处理指定比赛编号")
    args = parser.parse_args()
    if not args.team and not args.convert_only:
        from .gui import run_gui
        run_gui(update_health=args.update_health)
        return
    def emit(kind, data):
        if kind == "log":
            print(data, flush=True)
    try:
        selections = {}
        if args.trail_config:
            data = json.loads(Path(args.trail_config).read_text("utf-8"))
            selections = data.get("trail_selections", data)
            if not isinstance(selections, dict):
                raise AppError("轨迹配置必须是回放编号到舰船实体编号列表的 JSON 对象。")
        options = Options(team=args.team or "DEMO", url=args.url, output=args.output, both=args.both,
                          download_only=args.download_only, game_dir=args.game_dir, proxy=args.proxy,
                          trails=not args.no_trails, show_bans=not args.no_bans,
                          trail_mode="none" if args.no_trails else args.trail_mode, trail_selections=selections)
        runner = Runner.from_manifest(args.convert_only, options, emit=emit) if args.convert_only else Runner(options, emit=emit)
        if args.convert_only:
            from .core import load_library
            _, library = load_library(args.convert_only)
            selected = [j["report_id"] for j in library["jobs"]
                        if (args.report_id is None or j["report_id"] in args.report_id)
                        and (args.match_id is None or j["match_id"] == args.match_id)
                        and (args.perspective == "all" or (j["team"].casefold() == j.get("target_team", library["team"]).strip("[]").casefold()) == (args.perspective == "team"))]
            manifest = runner.convert(selected)
            if manifest["summary"]["failed"]:
                sys.exit(1)
        elif args.ban_pick_only:
            rows = runner.client.drafts(runner.search())
            path = runner.output / "ban-pick.json"
            write_json(path, rows)
            print(f"已保存 {len(rows)} 场比赛的 BAN/PICK：{path}")
        elif args.search_only:
            for m in runner.search():
                print(m["id"], m.get("scheduledTime"), " vs ".join(o["signup"]["teamTag"] for o in m["matchOpponents"]))
        else:
            matches = None
            if args.match_id is not None:
                matches = [m for m in runner.search() if m["id"] == args.match_id]
                if not matches:
                    raise AppError("此队伍的比赛列表中没有找到指定比赛编号。")
            manifest = runner.run(matches)
            if manifest["summary"]["failed"]:
                sys.exit(1)
    except (AppError, Cancelled, KeyboardInterrupt, OSError, ValueError) as e:
        print(str(e) or "任务已停止。", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
