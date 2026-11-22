"""Human-readable and machine-readable rendering of the handoff board.

The markdown view is the artifact a duty team actually passes on.  Every
clinical line is followed by the supporting fragment, and the view never
renders a conflict as if it were a settled order.
"""

from __future__ import annotations

import json
from typing import Any

from .models import (
    FIELD_LABELS,
    STATUS_COMPLETED,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    SYNTHETIC_DATA_NOTICE,
    CareItem,
    FieldResolution,
    TaskSnapshot,
)

AUTHORITY_LABELS = {
    "professional": "正式记录",
    "family": "家属补充",
    "note": "行政/交接备注",
}


def _field_line(resolution: FieldResolution) -> list[str]:
    label = FIELD_LABELS.get(resolution.field, resolution.field)
    if resolution.is_missing:
        return [f"- **{label}**：信息缺失（未见明确记录，需人工补齐）"]
    if resolution.is_conflict:
        lines = [f"- **{label}**：存在冲突，系统未自行选择"]
        for value in resolution.distinct:
            lines.extend(_value_quote(value, indent="  "))
        return lines
    lines = [f"- **{label}**：{resolution.display}"]
    for value in resolution.distinct:
        lines.extend(_value_quote(value, indent="  ", compact=True))
    if resolution.superseded:
        lines.append("  - 已被明确更正替换的旧值（保留备查）：")
        for value in resolution.superseded:
            lines.extend(_value_quote(value, indent="    ", compact=True))
    if resolution.preserved_by:
        sources = "、".join(
            dict.fromkeys(p.source_id for p in resolution.preserved_by)
        )
        lines.append(f"  - 另有材料明确说明此项未调整：{sources}")
    return lines


def _value_quote(value, indent: str, compact: bool = False) -> list[str]:
    provenance = value.provenance
    authority = AUTHORITY_LABELS.get(provenance.authority, provenance.authority)
    source = (
        f"{provenance.source_id} · {authority} · 版本 {provenance.version} · "
        f"字符 {provenance.start}-{provenance.end}"
    )
    if compact:
        return [f"{indent}- 原文：「{provenance.quote}」（{source}）"]
    return [
        f"{indent}- 「{provenance.quote}」",
        f"{indent}  — {source}",
    ]


def _item_block(item: CareItem, *, show_latest: bool = True) -> list[str]:
    snapshot = item.display_snapshot
    title = snapshot.action_label
    if snapshot.medication_label:
        title += f"（{snapshot.medication_label}）"
    lines = [f"### {title}"]
    lines.append(f"- 事项编号：`{item.item_id}`　状态：**{item.status_label}**　修订：{item.revision}")
    lines.append(f"- 摘要：{snapshot.display}")
    for resolution in snapshot.fields:
        if resolution.field == "medication" and not resolution.values:
            continue
        lines.extend(_field_line(resolution))

    if snapshot.action_provenance:
        # These fragments establish that the item exists.  Some may also
        # contain a value that a later correction has since replaced, so they
        # are labelled as item-level sources, not as the current order.
        lines.append("- 提及该事项的记录（含已被更正的原始记录，仅说明事项来源）：")
        for provenance in snapshot.action_provenance:
            lines.append(
                f"  - 「{provenance.quote}」（{provenance.source_id}，"
                f"{AUTHORITY_LABELS.get(provenance.authority, provenance.authority)}）"
            )
    if snapshot.patient_provenance:
        names = "、".join(
            dict.fromkeys(
                f"{p.quote}（{p.source_id}）" for p in snapshot.patient_provenance
            )
        )
        lines.append(f"- 患者归属依据：{names}")

    if item.status == STATUS_PENDING and item.review_reasons:
        lines.append("- 需重新核对的原因：")
        for reason in item.review_reasons:
            lines.append(f"  - {reason}")

    confirmation = item.latest_confirmation
    if confirmation and item.status in (STATUS_CONFIRMED, STATUS_COMPLETED):
        lines.append(
            f"- 确认：{confirmation.by} 于 {confirmation.at}"
            + (f"；备注：{confirmation.note}" if confirmation.note else "")
        )
        if confirmation.acknowledged_issues:
            lines.append(
                "- 确认时已读但未解决的问题："
                + "、".join(confirmation.acknowledged_issues)
            )
        if item.status == STATUS_PENDING and item.review_reasons:
            lines.append("- 注意：确认后材料有变化，当前状态已退回待确认。")

    if item.status == STATUS_COMPLETED and item.completion is not None:
        completion = item.completion
        lines.append(
            f"- 完成：{completion.by} 于 {completion.at}"
            + (f"；备注：{completion.note}" if completion.note else "")
        )
        lines.append("- 完成时采用的依据（冻结，不随后续材料改写）：")
        lines.extend(_frozen_basis(completion.basis))
        if item.post_completion_changes:
            lines.append("- 完成之后到达的材料与当时依据不同（仅供医护人员复核，不改写记录）：")
            for change in item.post_completion_changes:
                lines.append(f"  - {change}")
            if show_latest and item.snapshot.fingerprint != completion.basis.fingerprint:
                lines.append(f"  - 最新材料所示：{item.snapshot.display}")

    if item.post_completion_changes and item.status != STATUS_COMPLETED:
        lines.append("- 事后变化：")
        for change in item.post_completion_changes:
            lines.append(f"  - {change}")
    lines.append("")
    return lines


def _frozen_basis(snapshot: TaskSnapshot) -> list[str]:
    lines: list[str] = []
    for resolution in snapshot.fields:
        if resolution.field == "medication" and not resolution.values:
            continue
        label = FIELD_LABELS.get(resolution.field, resolution.field)
        if resolution.is_conflict:
            values = " / ".join(
                f"{v.display}（{v.provenance.source_id}）"
                for v in resolution.distinct
            )
            lines.append(f"  - {label}：仍为冲突，{values}")
            continue
        source = "、".join(
            dict.fromkeys(v.provenance.source_id for v in resolution.values)
        )
        lines.append(f"  - {label}：{resolution.display}（依据 {source or '无明确记录'}）")
    return lines


def render_board(board: dict[str, Any], *, include_completed_basis: bool = True) -> str:
    patient = board.get("patient") or board.get("patient_key") or "未命名患者"
    lines = [f"# 交接单：{patient}", ""]
    lines.append(f"> {SYNTHETIC_DATA_NOTICE}")
    lines.append("")

    counts = {
        STATUS_PENDING: len(board.get("pending", [])),
        STATUS_CONFIRMED: len(board.get("confirmed", [])),
        STATUS_COMPLETED: len(board.get("completed", [])),
    }
    lines.append(
        f"待确认 {counts[STATUS_PENDING]} 项 · 已确认 {counts[STATUS_CONFIRMED]} 项 · "
        f"已完成 {counts[STATUS_COMPLETED]} 项"
    )
    lines.append("")

    sections = (
        (STATUS_PENDING, "待确认", board.get("pending", [])),
        (STATUS_CONFIRMED, "已确认", board.get("confirmed", [])),
        (STATUS_COMPLETED, "已完成", board.get("completed", [])),
    )
    for _, title, items in sections:
        lines.append(f"## {title}")
        lines.append("")
        if not items:
            lines.append("（无）")
            lines.append("")
            continue
        for item in items:
            lines.extend(_item_block(item, show_latest=include_completed_basis))

    observations = board.get("observations", [])
    lines.append("## 观察线索（家属补充，不得覆盖专业安排）")
    lines.append("")
    if not observations:
        lines.append("（无）")
        lines.append("")
    for observation in observations:
        related = (
            f"；关联 `{observation.related_item_id}`"
            if observation.related_item_id
            else "；未自动关联到事项"
        )
        flag = "；与正式安排不一致，待医护核对" if observation.differs_from_formal else ""
        lines.append(
            f"- 「{observation.text}」（{observation.provenance.source_id}，"
            f"字符 {observation.provenance.start}-{observation.provenance.end}{related}{flag}）"
        )
    lines.append("")

    notes = board.get("notes", [])
    lines.append("## 交接备注（原文保留）")
    lines.append("")
    if not notes:
        lines.append("（无）")
        lines.append("")
    for note in notes:
        lines.append(f"- 「{note.text}」（{note.provenance.source_id}）")
    lines.append("")

    issues = board.get("issues", [])
    missing = [i for i in issues if i.code == "missing_field"]
    conflicts = [i for i in issues if i.code in {"field_conflict", "correction_ambiguous_due_to_conflict"}]
    other = [i for i in issues if i not in missing and i not in conflicts]
    lines.append("## 核对提示")
    lines.append("")
    lines.append(f"- 信息缺失：{len(missing)} 项")
    lines.append(f"- 相互矛盾：{len(conflicts)} 项")
    for issue in missing + conflicts:
        lines.append(f"  - {issue.message}")
    if other:
        lines.append("- 其他：")
        for issue in other:
            source = f"（{issue.source_id}）" if issue.source_id else ""
            lines.append(f"  - {issue.message}{source}")
    lines.append("")
    return "\n".join(lines)


def render_item(item: CareItem) -> str:
    return "\n".join(_item_block(item))


def board_to_json(board: dict[str, Any]) -> str:
    payload = {
        "patient_key": board["patient_key"],
        "patient": board["patient"],
        "pending": [item.to_dict() for item in board["pending"]],
        "confirmed": [item.to_dict() for item in board["confirmed"]],
        "completed": [item.to_dict() for item in board["completed"]],
        "observations": [item.to_dict() for item in board["observations"]],
        "notes": [item.to_dict() for item in board["notes"]],
        "issues": [item.to_dict() for item in board["issues"]],
        "counts": {
            "pending": len(board["pending"]),
            "confirmed": len(board["confirmed"]),
            "completed": len(board["completed"]),
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
