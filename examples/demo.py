#!/usr/bin/env python3
"""End-to-end demo on synthetic data — safe to run, touches no real records.

Run from the repository root::

    python3 examples/demo.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from care_handoff.cli import main as cli_main  # noqa: E402
from care_handoff.render import render_board  # noqa: E402
from care_handoff.sources import load_batch  # noqa: E402
from care_handoff.ledger import build_ledger  # noqa: E402
from care_handoff.workflow import CareState  # noqa: E402


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def demo() -> int:
    section("1. 读入第一周的合成材料")
    batch = load_batch(ROOT / "data" / "care.json")
    ledger = build_ledger(batch)
    state = CareState()
    state.ingest(ledger, batch_label="care.json")

    print(render_board(state.board("林宁")))

    section("2. 确认一项；缺失的负责人员必须显式核对")
    try:
        state.confirm("林宁::复诊", by="王护士")
    except Exception as exc:  # noqa: BLE001 - demo wants to show the refusal
        print("拒绝确认：", exc)
    state.confirm(
        "林宁::复诊",
        by="王护士",
        acknowledge=["missing:responsible"],
        note="已电话确认由社区护士负责，记录待补",
    )
    print("已确认：林宁::复诊")

    section("3. 完成一项；完成依据被冻结")
    state.confirm(
        "林宁::血压测量", by="王护士", acknowledge=["missing:responsible"]
    )
    state.complete("林宁::血压测量", by="王护士", note="已上门指导并完成首次测量")

    section("4. 更正材料在另一批到达：未完成事项重新展开")
    with tempfile.TemporaryDirectory() as directory:
        state_path = Path(directory) / "state.json"
        correction_path = Path(directory) / "correction.json"
        correction_path.write_text(
            json.dumps(
                [
                    {
                        "id": "care-a5",
                        "kind": "discharge_correction",
                        "version": 3,
                        "patient": "林宁",
                        "text": "更正：复诊时间改为四日后；测量安排未作调整。",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        state.save(state_path)
        exit_code = cli_main(
            ["ingest", str(correction_path), "--state", str(state_path)]
        )
        print("ingest 退出码：", exit_code)
        state = CareState.load(state_path)
        print(render_board(state.board("林宁")))

    section("5. 机器可读输出（节选）")
    board = state.board("林宁")
    pending = [item.to_dict() for item in board["pending"]]
    print(json.dumps(pending, ensure_ascii=False, indent=2)[:1600] + "\n...")
    return 0


if __name__ == "__main__":
    raise SystemExit(demo())
