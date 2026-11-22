"""Workflow state for handoff items.

Rules implemented here:

* Pending -> confirmed requires a named coordinator.  If the item has
  conflicts or missing required fields, the coordinator must explicitly
  acknowledge each one; acknowledgement is recorded verbatim alongside the
  frozen basis and does not resolve the conflict.
* Confirmed -> completed freezes the confirmed snapshot.  Later material does
  not rewrite that record.
* Confirmed -> completed is refused if the item changed after confirmation
  unless the caller re-confirms first (``--force`` is deliberately absent).
* New material re-opens *unfinished* items: confirmed-but-not-completed items
  whose content fingerprint changed return to pending with the reasons shown.
  Completed items keep their frozen basis and receive a separate
  ``post_completion_changes`` flag instead.
* State is serialisable to JSON so a duty team can pass the handoff on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .ledger import Ledger
from .models import (
    STATUS_COMPLETED,
    STATUS_CONFIRMED,
    STATUS_LABELS,
    STATUS_PENDING,
    CareItem,
    Confirmation,
    Completion,
    Issue,
    Note,
    Observation,
    TaskSnapshot,
    completion_from_dict,
    confirmation_from_dict,
    snapshot_from_dict,
)

STATE_VERSION = 1


class WorkflowError(ValueError):
    """A caller action that the workflow rules do not allow."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def snapshot_changes(old: TaskSnapshot, new: TaskSnapshot) -> list[str]:
    """Human-readable differences, used both for review reasons and audit."""

    changes: list[str] = []
    for field_name in ("timing", "responsible", "medication"):
        before = old.field(field_name)
        after = new.field(field_name)
        # Compare clinical values only.  A new source repeating the same
        # value must not look like a changed order.
        before_values = {value.value for value in before.distinct}
        after_values = {value.value for value in after.distinct}
        if before_values == after_values and before.is_missing == after.is_missing:
            continue
        changes.append(f"{field_name}: “{before.display}” -> “{after.display}”")
    if old.medication_key != new.medication_key:
        changes.append(f"medication_key: {old.medication_key} -> {new.medication_key}")
    return changes


@dataclass
class CareState:
    items: dict[str, CareItem] = field(default_factory=dict)
    applied_source_ids: list[str] = field(default_factory=list)
    processed_batches: list[dict[str, Any]] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    # The complete synthetic corpus seen so far.  A correction that arrives
    # in its own file must still be reconciled against earlier material, so
    # the state keeps the sources and rebuilds the whole ledger on ingest.
    source_records: list[dict[str, Any]] = field(default_factory=list)

    # -- persistence ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "applied_source_ids": list(self.applied_source_ids),
            "processed_batches": list(self.processed_batches),
            "items": [item.to_dict(include_history=True) for item in self.items.values()],
            "observations": [item.to_dict() for item in self.observations],
            "notes": [item.to_dict() for item in self.notes],
            "issues": [issue.to_dict() for issue in self.issues],
            "source_records": list(self.source_records),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CareState":
        state = cls()
        state.applied_source_ids = list(data.get("applied_source_ids", []))
        state.processed_batches = list(data.get("processed_batches", []))
        state.observations = [
            _observation_from_dict(item) for item in data.get("observations", [])
        ]
        state.notes = [_note_from_dict(item) for item in data.get("notes", [])]
        state.issues = [_issue_from_dict(item) for item in data.get("issues", [])]
        state.source_records = list(data.get("source_records", []))
        for raw in data.get("items", []):
            state.items[raw["item_id"]] = _care_item_from_dict(raw)
        return state

    @classmethod
    def load(cls, path: str | Path) -> "CareState":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("version") != STATE_VERSION:
            raise WorkflowError(f"不支持的状态文件版本：{payload.get('version')}")
        return cls.from_dict(payload)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # -- ingestion ------------------------------------------------------

    def ingest(
        self,
        ledger: Ledger,
        *,
        batch_label: str | None = None,
        vocabulary=None,
    ) -> list[Issue]:
        """Add a ledger's sources to the corpus and reconcile everything.

        Corrections usually arrive in a later file than the record they
        change.  Rebuilding the ledger from the *whole* accumulated corpus is
        what lets such a correction find its target; merging only the newest
        file would leave the correction unmatched and the old value live.

        Returns the notices produced by this ingestion.  Items already
        completed are never rewritten; their ``post_completion_changes``
        records what arrived later.
        """

        from .ledger import build_ledger
        from .sources import SourceRecord, batch_from_records

        known = {record["id"] for record in self.source_records}
        for source in ledger.sources.values():
            if source.id not in known:
                self.source_records.append(dict(source.raw))
                known.add(source.id)

        corpus = [
            SourceRecord.from_mapping(raw, index)
            for index, raw in enumerate(self.source_records)
        ]
        full_batch = batch_from_records(corpus)
        rebuilt = build_ledger(full_batch, vocabulary=vocabulary)

        notices = self._merge_snapshots(rebuilt.snapshots)
        self.observations = list(rebuilt.observations)
        self.notes = list(rebuilt.notes)
        self.issues = _merge_unique([], rebuilt.issues, key=_issue_key)
        self.applied_source_ids = sorted(
            set(self.applied_source_ids) | set(rebuilt.sources)
        )
        if batch_label:
            self.processed_batches.append({"label": batch_label})
        return notices

    def _merge_snapshots(
        self, snapshots: dict[str, TaskSnapshot]
    ) -> list[Issue]:
        notices: list[Issue] = []
        for item_id, snapshot in snapshots.items():
            existing = self.items.get(item_id)
            if existing is None:
                self.items[item_id] = CareItem(
                    item_id=item_id,
                    status=STATUS_PENDING,
                    snapshot=snapshot,
                    applied_source_ids=list(snapshot.considered_source_ids),
                )
                continue

            content_changed = (
                existing.snapshot.fingerprint != snapshot.fingerprint
            )
            if not content_changed:
                # A duplicate source may add evidence without changing the
                # clinical content; that alone must not force re-confirmation.
                existing.snapshot = snapshot
                existing.applied_source_ids = sorted(
                    set(existing.applied_source_ids)
                    | set(snapshot.considered_source_ids)
                )
                continue

            existing.history.append(existing.snapshot)
            existing.revision += 1

            if existing.status == STATUS_COMPLETED:
                changes = snapshot_changes(existing.completion.basis, snapshot)
                existing.post_completion_changes = changes
                notices.append(
                    Issue(
                        code="post_completion_change",
                        message=(
                            f"{item_id} 已完成，之后到达的材料含不同内容；"
                            "保留完成时的依据，新材料单独标注，"
                            "是否调整已执行的照护由有权限的医护人员决定。"
                        ),
                        severity="info",
                        patient_key=snapshot.patient_key,
                    )
                )
            elif existing.status == STATUS_CONFIRMED:
                changes = snapshot_changes(existing.snapshot, snapshot)
                existing.status = STATUS_PENDING
                existing.review_reasons = changes + ["新材料到达，需重新确认"]
                notices.append(
                    Issue(
                        code="reopened_for_review",
                        message=f"{item_id} 的已确认内容发生变化，已退回待确认："
                        + "；".join(changes),
                        severity="warning",
                        patient_key=snapshot.patient_key,
                    )
                )
            else:
                # Already pending: keep it pending, but say why it moved.
                changes = snapshot_changes(existing.snapshot, snapshot)
                if changes:
                    existing.review_reasons = changes

            existing.snapshot = snapshot
            existing.applied_source_ids = sorted(
                set(existing.applied_source_ids)
                | set(snapshot.considered_source_ids)
            )
        return notices

    # -- workflow actions ----------------------------------------------

    def confirm(
        self,
        item_id: str,
        *,
        by: str,
        note: str | None = None,
        acknowledge: Iterable[str] = (),
        at: str | None = None,
    ) -> CareItem:
        item = self._require(item_id)
        if item.status == STATUS_COMPLETED:
            raise WorkflowError(f"{item_id} 已完成；如需更改请先由医护人员开具新记录。")
        if not by or not by.strip():
            raise WorkflowError("确认必须记录确认人。")

        required = self._acknowledgement_requirements(item)
        supplied = set(acknowledge)
        missing_acks = [key for key in required if key not in supplied]
        if missing_acks:
            raise WorkflowError(
                "确认前需显式核对并承认以下问题："
                + "、".join(missing_acks)
                + "。这不会解决冲突，只会记录已读。"
            )

        confirmation = Confirmation(
            by=by.strip(),
            at=at or utc_now(),
            basis=item.snapshot,
            note=note,
            acknowledged_issues=tuple(sorted(supplied & set(required))),
        )
        item.confirmations.append(confirmation)
        item.status = STATUS_CONFIRMED
        item.review_reasons = []
        return item

    def complete(
        self,
        item_id: str,
        *,
        by: str,
        note: str | None = None,
        at: str | None = None,
    ) -> CareItem:
        item = self._require(item_id)
        if item.status == STATUS_PENDING:
            raise WorkflowError(f"{item_id} 尚未确认，不能直接标记完成。")
        if item.status == STATUS_COMPLETED:
            raise WorkflowError(f"{item_id} 已经完成。")
        if item.snapshot.fingerprint != item.confirmed_basis.fingerprint:
            raise WorkflowError(
                f"{item_id} 在确认后已有新材料，需重新确认后再完成。"
            )
        if not by or not by.strip():
            raise WorkflowError("完成必须记录执行人。")
        item.completion = Completion(
            by=by.strip(),
            at=at or utc_now(),
            basis=item.confirmed_basis,
            note=note,
        )
        item.status = STATUS_COMPLETED
        item.review_reasons = []
        return item

    # -- queries -------------------------------------------------------

    def require_item(self, item_id: str) -> CareItem:
        return self._require(item_id)

    def _require(self, item_id: str) -> CareItem:
        try:
            return self.items[item_id]
        except KeyError as exc:
            raise WorkflowError(f"没有名为 {item_id} 的事项") from exc

    @staticmethod
    def _acknowledgement_requirements(item: CareItem) -> list[str]:
        required: list[str] = []
        for conflict in item.snapshot.conflicts:
            required.append(f"conflict:{conflict.field}")
        for name in item.snapshot.missing:
            required.append(f"missing:{name}")
        return required

    def patients(self) -> list[tuple[str, str]]:
        seen: dict[str, str] = {}
        for item in self.items.values():
            seen.setdefault(item.snapshot.patient_key, item.snapshot.patient_label)
        for observation in self.observations:
            seen.setdefault(observation.patient_key, observation.patient_label)
        for note in self.notes:
            if note.patient_key:
                seen.setdefault(note.patient_key, note.patient_label)
        return sorted(seen.items())

    def board(self, patient_key: str) -> dict[str, Any]:
        items = [
            item
            for item in self.items.values()
            if item.snapshot.patient_key == patient_key
        ]
        pending = [item for item in items if item.status == STATUS_PENDING]
        confirmed = [item for item in items if item.status == STATUS_CONFIRMED]
        completed = [item for item in items if item.status == STATUS_COMPLETED]
        label = patient_key
        for item in items:
            label = item.snapshot.patient_label or label
            break
        return {
            "patient_key": patient_key,
            "patient": label,
            "pending": pending,
            "confirmed": confirmed,
            "completed": completed,
            "observations": [
                o for o in self.observations if o.patient_key == patient_key
            ],
            "notes": [n for n in self.notes if n.patient_key == patient_key],
            "issues": [
                i
                for i in self.issues
                if i.patient_key in (None, patient_key)
            ],
        }


def _merge_unique(existing: list[Any], new: Iterable[Any], *, key) -> list[Any]:
    seen = {key(item) for item in existing}
    output = list(existing)
    for item in new:
        item_key = key(item)
        if item_key in seen:
            continue
        seen.add(item_key)
        output.append(item)
    return output


def _issue_key(issue: Issue) -> tuple[Any, ...]:
    return (issue.code, issue.message, issue.source_id, issue.quote)


def _observation_from_dict(data: dict[str, Any]) -> Observation:
    from .models import Provenance, timing_from_dict

    provenance = data["provenance"]
    return Observation(
        patient_key=data["patient_key"],
        patient_label=data["patient"],
        kind=data["kind"],
        text=data["text"],
        provenance=Provenance(
            source_id=provenance["source_id"],
            source_kind=provenance["source_kind"],
            quote=provenance["quote"],
            start=provenance["start"],
            end=provenance["end"],
            version=provenance.get("version", 1),
            author=provenance.get("author"),
            authority=provenance.get("authority", "family"),
        ),
        related_item_id=data.get("related_item_id"),
        proposed_action=data.get("proposed_action"),
        proposed_timing=timing_from_dict(data.get("proposed_timing")),
        proposed_medication=data.get("proposed_medication"),
        differs_from_formal=bool(data.get("differs_from_formal", False)),
    )


def _note_from_dict(data: dict[str, Any]) -> Note:
    from .models import Provenance

    provenance = data["provenance"]
    return Note(
        patient_key=data["patient_key"],
        patient_label=data["patient"],
        kind=data["kind"],
        text=data["text"],
        provenance=Provenance(
            source_id=provenance["source_id"],
            source_kind=provenance["source_kind"],
            quote=provenance["quote"],
            start=provenance["start"],
            end=provenance["end"],
            version=provenance.get("version", 1),
            author=provenance.get("author"),
            authority=provenance.get("authority", "note"),
        ),
    )


def _issue_from_dict(data: dict[str, Any]) -> Issue:
    return Issue(
        code=data["code"],
        message=data["message"],
        severity=data.get("severity", "warning"),
        source_id=data.get("source_id"),
        quote=data.get("quote"),
        patient_key=data.get("patient_key"),
    )


def _care_item_from_dict(data: dict[str, Any]) -> CareItem:
    snapshot = snapshot_from_dict(data["display_snapshot"])
    latest = snapshot_from_dict(data.get("latest_snapshot", data["display_snapshot"]))
    confirmations = [
        confirmation_from_dict(raw) for raw in data.get("confirmations", [])
    ]
    completion = (
        completion_from_dict(data["completion"]) if data.get("completion") else None
    )
    return CareItem(
        item_id=data["item_id"],
        status=data["status"],
        snapshot=latest,
        revision=int(data.get("revision", 1)),
        review_reasons=list(data.get("review_reasons", [])),
        post_completion_changes=list(data.get("post_completion_changes", [])),
        confirmations=confirmations,
        completion=completion,
        history=[
            snapshot_from_dict(raw) for raw in data.get("history", [])
        ],
        applied_source_ids=list(
            data.get("applied_source_ids", snapshot.considered_source_ids)
        ),
    )


__all__ = [
    "CareState",
    "STATUS_LABELS",
    "WorkflowError",
    "snapshot_changes",
    "utc_now",
]
