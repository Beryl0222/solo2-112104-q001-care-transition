"""Command-line entry point for the community care handoff tool.

Typical use::

    python -m care_handoff ingest data/care.json --state handoff_state.json
    python -m care_handoff board 林宁 --state handoff_state.json
    python -m care_handoff show 林宁::复诊 --state handoff_state.json
    python -m care_handoff confirm 林宁::复诊 --by 王护士 \
        --acknowledge missing:responsible --state handoff_state.json
    python -m care_handoff complete 林宁::复诊 --by 王护士 --state handoff_state.json

Every command is deterministic, uses only the standard library, and refuses
to write clinical conclusions on its own.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .ledger import build_ledger
from .models import STATUS_LABELS, SYNTHETIC_DATA_NOTICE
from .render import board_to_json, render_board, render_item
from .sources import load_batch
from .workflow import CareState, WorkflowError


def _load_state(path: Path) -> CareState:
    if path.exists():
        return CareState.load(path)
    return CareState()


def _save_state(state: CareState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state.save(path)


def cmd_ingest(args: argparse.Namespace) -> int:
    batch = load_batch(args.source)
    ledger = build_ledger(batch)
    state = _load_state(args.state)
    notices = state.ingest(ledger, batch_label=args.label or str(args.source))
    _save_state(state, args.state)
    print(f"已读入 {len(ledger.sources)} 条来源材料，当前共 {len(state.items)} 项交接事项。")
    patients = state.patients()
    if patients:
        print("患者：" + "、".join(label for _, label in patients))
    for notice in notices:
        print(f"[{STATUS_LABELS.get('pending', '提示')}] {notice.message}")
    if ledger.issues:
        print(f"核对提示 {len(ledger.issues)} 条；用 board 查看详情。")
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    state = _load_state(args.state)
    board = state.board(args.patient)
    if not board["pending"] and not board["confirmed"] and not board["completed"] and not board["observations"] and not board["notes"]:
        print(f"没有找到患者 {args.patient} 的任何记录。", file=sys.stderr)
        return 2
    if args.json:
        print(board_to_json(board))
    else:
        print(render_board(board))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    state = _load_state(args.state)
    try:
        item = state.require_item(args.item)
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(item.to_dict(include_history=args.history), ensure_ascii=False, indent=2))
    else:
        print(render_item(item))
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    state = _load_state(args.state)
    try:
        item = state.confirm(
            args.item,
            by=args.by,
            note=args.note,
            acknowledge=args.acknowledge or (),
        )
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _save_state(state, args.state)
    print(f"{item.item_id} 已确认（{item.latest_confirmation.by}）。")
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    state = _load_state(args.state)
    try:
        item = state.complete(args.item, by=args.by, note=args.note)
    except WorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _save_state(state, args.state)
    print(f"{item.item_id} 已完成（{item.completion.by}），完成依据已冻结。")
    return 0


def cmd_patients(args: argparse.Namespace) -> int:
    state = _load_state(args.state)
    patients = state.patients()
    if not patients:
        print("状态文件中还没有患者。")
        return 0
    for key, label in patients:
        counts = {status: 0 for status in ("pending", "confirmed", "completed")}
        for item in state.items.values():
            if item.snapshot.patient_key == key:
                counts[item.status] += 1
        print(
            f"{label}（{key}）：待确认 {counts['pending']}，"
            f"已确认 {counts['confirmed']}，已完成 {counts['completed']}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="care_handoff",
        description="把合成出院材料整理为有出处、可确认、可完成的交接事项。",
    )
    parser.add_argument("--version", action="version", version="care_handoff 1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="读入一批来源材料并合并到状态文件")
    ingest.add_argument("source", type=Path)
    ingest.add_argument("--state", type=Path, default=Path("handoff_state.json"))
    ingest.add_argument("--label", help="批次标签，便于追溯")
    ingest.set_defaults(func=cmd_ingest)

    board = sub.add_parser("board", help="按患者查看交接单")
    board.add_argument("patient")
    board.add_argument("--state", type=Path, default=Path("handoff_state.json"))
    board.add_argument("--json", action="store_true")
    board.set_defaults(func=cmd_board)

    show = sub.add_parser("show", help="查看单项事项及其出处")
    show.add_argument("item")
    show.add_argument("--state", type=Path, default=Path("handoff_state.json"))
    show.add_argument("--json", action="store_true")
    show.add_argument("--history", action="store_true")
    show.set_defaults(func=cmd_show)

    confirm = sub.add_parser("confirm", help="确认一项交接事项")
    confirm.add_argument("item")
    confirm.add_argument("--by", required=True, help="确认人")
    confirm.add_argument("--note")
    confirm.add_argument(
        "--acknowledge",
        action="append",
        metavar="问题代码",
        help="显式核对冲突/缺失，例如 conflict:timing 或 missing:responsible",
    )
    confirm.add_argument("--state", type=Path, default=Path("handoff_state.json"))
    confirm.set_defaults(func=cmd_confirm)

    complete = sub.add_parser("complete", help="标记一项交接事项已完成")
    complete.add_argument("item")
    complete.add_argument("--by", required=True, help="执行人")
    complete.add_argument("--note")
    complete.add_argument("--state", type=Path, default=Path("handoff_state.json"))
    complete.set_defaults(func=cmd_complete)

    patients = sub.add_parser("patients", help="列出全部患者及各类事项数量")
    patients.add_argument("--state", type=Path, default=Path("handoff_state.json"))
    patients.set_defaults(func=cmd_patients)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "ingest":
        print(f"> {SYNTHETIC_DATA_NOTICE}", file=sys.stderr)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
