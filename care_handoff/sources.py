"""Source loading and validation.

Source files are plain JSON.  Every record must carry an id and text; the
remaining fields are optional and default conservatively.  The loader never
invents authority: an unknown ``kind`` is treated as an administrative note,
not as a professional order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .models import AUTHORITY_FAMILY, AUTHORITY_NOTE, AUTHORITY_PROFESSIONAL, Issue

PROFESSIONAL_KINDS = {"discharge", "discharge_correction", "outpatient", "clinic_note"}
FAMILY_KINDS = {"family_note", "family_update"}
NOTE_KINDS = {"handoff", "admin_note", "note"}
CORRECTION_KINDS = {"discharge_correction", "correction"}


@dataclass(frozen=True)
class SourceRecord:
    id: str
    kind: str
    text: str
    version: int = 1
    patient: str | None = None
    author: str | None = None
    received_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], index: int) -> "SourceRecord":
        missing = [key for key in ("id", "text") if not raw.get(key)]
        if missing:
            raise ValueError(f"第 {index + 1} 条记录缺少字段：{', '.join(missing)}")
        text = raw["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"记录 {raw['id']} 的 text 必须是非空字符串")
        version = raw.get("version", 1)
        if not isinstance(version, int) or version < 1:
            raise ValueError(f"记录 {raw['id']} 的 version 必须是正整数")
        return cls(
            id=str(raw["id"]),
            kind=str(raw.get("kind", "note")),
            text=text,
            version=version,
            patient=raw.get("patient"),
            author=raw.get("author"),
            received_at=raw.get("received_at"),
            raw=dict(raw),
        )

    @property
    def authority(self) -> str:
        if self.kind in PROFESSIONAL_KINDS:
            return AUTHORITY_PROFESSIONAL
        if self.kind in FAMILY_KINDS:
            return AUTHORITY_FAMILY
        return AUTHORITY_NOTE

    @property
    def is_correction(self) -> bool:
        return self.kind in CORRECTION_KINDS

    @property
    def is_family(self) -> bool:
        return self.kind in FAMILY_KINDS

    @property
    def is_professional(self) -> bool:
        return self.kind in PROFESSIONAL_KINDS


def _sort_key(record: SourceRecord) -> tuple[str, int, str]:
    # received_at is only used to order the same source's versions.  It is
    # never used to decide which conflicting order is clinically correct.
    return (record.received_at or "", record.version, record.id)


def load_sources(path: str | Path) -> list[SourceRecord]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("来源资料必须是 JSON 数组")
    records: list[SourceRecord] = []
    seen: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 条记录必须是对象")
        record = SourceRecord.from_mapping(item, index)
        if record.id in seen:
            raise ValueError(f"重复的来源 id：{record.id}")
        seen.add(record.id)
        records.append(record)
    return sorted(records, key=_sort_key)


@dataclass(frozen=True)
class Batch:
    records: tuple[SourceRecord, ...]
    issues: tuple[Issue, ...] = ()

    def by_id(self) -> dict[str, SourceRecord]:
        return {record.id: record for record in self.records}

    @property
    def patients(self) -> tuple[str, ...]:
        names = {record.patient for record in self.records if record.patient}
        return tuple(sorted(names))


def load_batch(path: str | Path) -> Batch:
    records = load_sources(path)
    return batch_from_records(records)


def batch_from_records(records: Iterable[SourceRecord]) -> Batch:
    """Build a validated batch, including unknown-kind notices.

    Used both by the file loader and when a saved corpus is re-read from the
    workflow state, so both paths produce identical issues.
    """

    ordered = sorted(records, key=_sort_key)
    issues: list[Issue] = []
    for record in ordered:
        if record.authority == AUTHORITY_NOTE and record.kind not in NOTE_KINDS:
            issues.append(
                Issue(
                    code="unknown_source_kind",
                    message=f"未知来源类型“{record.kind}”，按行政备注处理，未视为专业安排。",
                    severity="info",
                    source_id=record.id,
                )
            )
    return Batch(tuple(ordered), tuple(issues))


def records_from_mappings(items: Iterable[dict[str, Any]]) -> list[SourceRecord]:
    records = [
        SourceRecord.from_mapping(item, index) for index, item in enumerate(items)
    ]
    return sorted(records, key=_sort_key)
