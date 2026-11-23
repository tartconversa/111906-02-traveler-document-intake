"""资料员命令行入口：扫描、领取、送审、核对、补件、查询进度。

普通列表只显示材料状态与收件号，证件号码统一遮蔽；需要查看完整原件信息时，
原件仍只保存在隔离下载目录中，不在终端打印。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from .browser import PortalSession
from .config import Config
from .masking import MaskingFilter, mask_text
from .service import CollectorService
from .store import Store


def _setup_logging(config: Config, verbose: bool) -> None:
    config.ensure_dirs()
    logger = logging.getLogger("collector")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s")
    filter_ = MaskingFilter()

    file_handler = logging.FileHandler(config.log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(filter_)
    logger.addHandler(file_handler)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    stream.addFilter(filter_)
    logger.addHandler(stream)


def _short_sha(sha: str | None) -> str:
    return (sha or "")[:8]


def render_status(view: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    if not view:
        return "（本地尚无旅客记录，请先执行 scan）"
    for t in view:
        delegation = "有效" if t["delegation_active"] else "已过期"
        lines.append(
            f"旅客 {t['traveler']}（家庭 {t['family']}）批次 {t['trip']} "
            f"用途 {t['purpose']} 代理人 {t['delegate']} "
            f"委托至 {t['delegate_until']} [{delegation}]")
        for item in t["requirements"]:
            head = f"  - {item['kind']:<8} {item['document_id']}"
            if item["material_state"] == "collected":
                lines.append(f"{head} 已领取 sha256={_short_sha(item.get('sha256'))}…")
            elif item["material_state"] == "blocked":
                lines.append(
                    f"{head} 暂不能交：{CollectorService.reason_text(item.get('reason'))}"
                    f"（{item.get('reason')}）")
            else:
                lines.append(f"{head} 缺件：{CollectorService.reason_text('not_collected')}")
        if not t["packages"]:
            lines.append("  送审：尚无送审包")
        for pkg in t["packages"]:
            receipt = pkg["receipt"] or "待核对（未取得收件号）"
            lines.append(
                f"  送审包 {pkg['package_ref']} 状态={pkg['state']} 收件号={receipt}")
            for f in pkg["files"]:
                lines.append(
                    f"      · {f['kind']:<8} {f['document_id']} "
                    f"sha256={_short_sha(f['sha256'])}…")
            if pkg["reviews"]:
                for kind, result in sorted(pkg["reviews"].items()):
                    lines.append(f"      结论 {kind}: {result}")
            else:
                lines.append("      结论：待审")
            for supp in pkg["supplements"]:
                lines.append(
                    f"      补件 {supp['supp_ref']} {supp['kind']} "
                    f"{supp['document_id']} 状态={supp['state']}")
        lines.append("")
    return mask_text("\n".join(lines))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collector",
        description="经 Chromium 领取出行证件原件并移交本地受理台")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--state-dir", default=".collector-state")
    parser.add_argument("--headed", action="store_true",
                        help="显示浏览器窗口（默认无头）")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="扫描门户当前旅客与批次契约")

    p_collect = sub.add_parser("collect", help="按旅客领取当前批次全部所需原件")
    p_collect.add_argument("traveler")
    p_collect.add_argument("--trip")

    p_submit = sub.add_parser("submit", help="组卷并向受理台送审")
    p_submit.add_argument("traveler")
    p_submit.add_argument("--trip")
    p_submit.add_argument("--lose-response", action="store_true",
                          help="演练：触发受理台先收件后 503")

    sub.add_parser("reconcile", help="用受理台历史核对未确认送审")

    p_poll = sub.add_parser("poll", help="查询各收件号逐项受理结论")
    p_poll.add_argument("--traveler")
    p_poll.add_argument("--trip")

    p_supp = sub.add_parser("supplement", help="针对收件号的补件要求提交指定原件")
    p_supp.add_argument("receipt")
    p_supp.add_argument("kind")
    p_supp.add_argument("traveler")
    p_supp.add_argument("--trip")

    p_status = sub.add_parser("status", help="按旅客/批次查看进度与缺件原因")
    p_status.add_argument("--traveler")
    p_status.add_argument("--trip")

    p_run = sub.add_parser("run", help="扫描→领取→送审→核对→查结论 一次跑完")
    p_run.add_argument("--lose-response", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config(
        base_url=args.base_url.rstrip("/"),
        state_dir=Path(args.state_dir).resolve(),
        headless=not args.headed,
    )
    _setup_logging(config, args.verbose)
    store = Store(config.db_path)
    code = 0
    try:
        with PortalSession(config) as session:
            service = CollectorService(config, store)
            if args.command == "scan":
                travelers = service.scan(session)
                print(render_status(service.status()))
                print(f"扫描完成：{len(travelers)} 名旅客")
            elif args.command == "collect":
                result = service.collect(session, args.traveler, args.trip)
                print(render_status(service.status(args.traveler)))
                print(f"领取 {len(result['downloaded'])} 件，"
                      f"阻断 {len(result['blocked'])} 项，"
                      f"复用 {len(result['skipped'])} 件")
            elif args.command == "submit":
                result = service.submit(
                    session, args.traveler, args.trip,
                    lose_response=args.lose_response)
                print(render_status(service.status(args.traveler)))
                if result["submitted"]:
                    print(f"送审包 {result['package_ref']} 已提交，"
                          f"响应={result['response'].get('status')}")
                else:
                    print("本次未产生新送审包："
                          + result.get("note", "暂无可交文件"))
            elif args.command == "reconcile":
                rows = service.reconcile(session)
                print(render_status(service.status()))
                for row in rows:
                    if row["found"]:
                        print(f"核对成功 {row['package_ref']} -> {row['receipt']}")
                    else:
                        print(f"仍待核对 {row['package_ref']}（受理台历史无记录）")
            elif args.command == "poll":
                rows = service.poll_reviews(session, args.traveler, args.trip)
                print(render_status(service.status(args.traveler)))
                for row in rows:
                    print(f"收件 {row['receipt']} 结论 {row['results'] or '待审'}")
            elif args.command == "supplement":
                result = service.supplement(
                    session, args.receipt, args.kind,
                    args.traveler, args.trip or store.latest_trip(args.traveler))
                print(render_status(service.status(args.traveler)))
                print(f"补件 {result['supp_ref']} 已提交" if result.get("submitted")
                      else f"补件未重复提交：{result.get('note')}")
            elif args.command == "status":
                print(render_status(service.status(args.traveler, args.trip)))
            elif args.command == "run":
                code = _run_pipeline(session, service, args.lose_response)
    finally:
        store.close()
    return code


def _run_pipeline(
    session: PortalSession, service: CollectorService, lose_response: bool,
) -> int:
    travelers = service.scan(session)
    for traveler in travelers:
        tid, trip = traveler["id"], traveler["trip"]
        service.collect(session, tid, trip)
        service.submit(session, tid, trip, lose_response=lose_response)
    service.reconcile(session)
    service.poll_reviews(session)
    print(render_status(service.status()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
