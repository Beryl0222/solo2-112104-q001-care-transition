"""Immutable value objects used by the care-handoff pipeline.

The important design choices live here:

* every resolved field keeps the exact source quote and character span;
* a confirmation stores a full snapshot of the evidence adopted at that time;
* conflicts are represented as several values, never collapsed into a newer one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_COMPLETED = "completed"

STATUS_LABELS = {
    STATUS_PENDING: "待确认",
    STATUS_CONFIRMED: "已确认",
    STATUS_COMPLETED: "已完成",
}

FIELD_LABELS = {
    "timing": "执行时段",
    "responsible": "负责人员",
    "medication": "药物",
    "patient": "患者",
}

AUTHORITY_PROFESSIONAL = "professional"
AUTHORITY_FAMILY = "family"
AUTHORITY_NOTE = "note"

SYNTHETIC_DATA_NOTICE = (
    "本项目只处理合成照护文本；任何临床处置仍须由有权限的医护人员确认。"
)


@dataclass(frozen=True)
class Timing:
    """A normalised execution time expression.

    A schedule can have several independent dimensions, e.g. frequency
    ("每日两次") plus relation to meals ("饭后").  Keeping the dimensions
    separate lets a later correction replace only one of them instead of
    forcing the whole schedule to be treated as a conflict.

    ``dimensions`` is a tuple of ``(name, value_key, value_label)``.
    ``key`` and ``label`` are derived and stay stable for comparison/display.
    """

    dimensions: tuple[tuple[str, str, str], ...] = ()

    @property
    def key(self) -> str:
        return "+".join(f"{name}:{value}" for name, value, _ in self.dimensions)

    @property
    def label(self) -> str:
        return "，".join(label for _, _, label in self.dimensions)

    def with_dimensions(self, replacements: "Timing") -> "Timing":
        """Replace dimensions by name, keeping unnamed dimensions intact."""

        replaced_names = {name for name, _, _ in replacements.dimensions}
        kept = [
            dimension
            for dimension in self.dimensions
            if dimension[0] not in replaced_names
        ]
        merged = kept + list(replacements.dimensions)
        merged.sort(key=lambda dimension: DIMENSION_ORDER.get(dimension[0], 99))
        return Timing(tuple(merged))

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "dimensions": [
                {"name": name, "value": value, "label": label}
                for name, value, label in self.dimensions
            ],
        }


DIMENSION_ORDER = {"frequency": 0, "relation": 1, "anchor": 2, "duration": 3}


@dataclass(frozen=True)
class Provenance:
    """The exact fragment that supports one judgement."""

    source_id: str
    source_kind: str
    quote: str
    start: int
    end: int
    version: int = 1
    author: str | None = None
    authority: str = AUTHORITY_PROFESSIONAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "quote": self.quote,
            "start": self.start,
            "end": self.end,
            "version": self.version,
            "author": self.author,
            "authority": self.authority,
        }


@dataclass(frozen=True)
class FieldValue:
    """One candidate value for a field, together with its evidence.

    ``lineage`` lists every source id this value stands for.  A normal value
    starts with its own source; a corrected value inherits the lineage of the
    values it replaced and adds its own.  Lineage is what lets a later
    correction target exactly the record it supersedes instead of choosing a
    newer sentence globally.
    """

    field: str
    value: str
    label: str
    provenance: Provenance
    correction: bool = False
    lineage: tuple[str, ...] = ()
    # For timing values this carries the structured dimensions, so a later
    # correction can replace one dimension without re-parsing display text.
    timing: Timing | None = None

    def __post_init__(self) -> None:
        if not self.lineage:
            object.__setattr__(self, "lineage", (self.provenance.source_id,))

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "field": self.field,
            "value": self.value,
            "label": self.label,
            "correction": self.correction,
            "lineage": list(self.lineage),
            "provenance": self.provenance.to_dict(),
        }
        if self.timing is not None:
            payload["timing"] = self.timing.to_dict()
        return payload


@dataclass(frozen=True)
class FieldResolution:
    """All candidate values for one field.

    ``values`` are the currently applicable values.  ``superseded`` keeps
    earlier values that an explicit correction replaced, so the handoff can
    show what changed instead of silently dropping it.

    More than one distinct applicable value is a conflict, not a reason to
    pick one.  Zero values means the field is missing.
    """

    field: str
    values: tuple[FieldValue, ...] = ()
    superseded: tuple[FieldValue, ...] = ()
    preserved_by: tuple[Provenance, ...] = ()

    @property
    def distinct(self) -> tuple[FieldValue, ...]:
        seen: set[str] = set()
        output: list[FieldValue] = []
        for value in self.values:
            if value.value not in seen:
                seen.add(value.value)
                output.append(value)
        return tuple(output)

    @property
    def is_missing(self) -> bool:
        return not self.values

    @property
    def is_conflict(self) -> bool:
        return len(self.distinct) > 1

    @property
    def display(self) -> str:
        if self.is_missing:
            return "信息缺失"
        if self.is_conflict:
            return "相互矛盾：" + " / ".join(v.label for v in self.distinct)
        if len(self.values) == 1:
            return self.values[0].label
        # Same value supported by several sources: display once.
        return self.values[0].label

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "field_label": FIELD_LABELS.get(self.field, self.field),
            "display": self.display,
            "missing": self.is_missing,
            "conflict": self.is_conflict,
            "values": [value.to_dict() for value in self.values],
            "superseded": [value.to_dict() for value in self.superseded],
            "preserved_by": [p.to_dict() for p in self.preserved_by],
        }


@dataclass(frozen=True)
class Conflict:
    field: str
    values: tuple[FieldValue, ...]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "field_label": FIELD_LABELS.get(self.field, self.field),
            "message": self.message,
            "values": [value.to_dict() for value in self.values],
        }


@dataclass(frozen=True)
class TaskSnapshot:
    """A resolved view of one (patient, action, medication) item.

    The snapshot is deliberately independent of workflow status.  A
    confirmation or completion freezes a copy of it as its basis.
    """

    item_id: str
    patient_key: str
    patient_label: str
    action_key: str
    action_label: str
    medication_key: str | None
    medication_label: str | None
    fields: tuple[FieldResolution, ...]
    conflicts: tuple[Conflict, ...]
    missing: tuple[str, ...]
    source_ids: tuple[str, ...]
    considered_source_ids: tuple[str, ...]
    fingerprint: str
    action_provenance: tuple[Provenance, ...] = ()
    patient_provenance: tuple[Provenance, ...] = ()

    def field(self, name: str) -> FieldResolution:
        for resolution in self.fields:
            if resolution.field == name:
                return resolution
        return FieldResolution(name, ())

    @property
    def missing_labels(self) -> tuple[str, ...]:
        return tuple(FIELD_LABELS.get(name, name) for name in self.missing)

    @property
    def display(self) -> str:
        parts = [self.action_label]
        medication_field = self.field("medication")
        # For a medication-scoped item the drug is already part of the item
        # identity; repeat it only when the record itself is in conflict.
        include_medication = medication_field.values and (
            self.medication_key is None or medication_field.is_conflict
        )
        if self.medication_label and not include_medication:
            parts[0] = f"{self.action_label} {self.medication_label}"
        parts.append("执行时段：" + self.field("timing").display)
        parts.append("负责人员：" + self.field("responsible").display)
        if include_medication:
            parts.append("药物：" + medication_field.display)
        return "；".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "patient_key": self.patient_key,
            "patient": self.patient_label,
            "action_key": self.action_key,
            "action": self.action_label,
            "medication_key": self.medication_key,
            "medication": self.medication_label,
            "display": self.display,
            "fields": {resolution.field: resolution.to_dict() for resolution in self.fields},
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "missing": list(self.missing),
            "missing_labels": list(self.missing_labels),
            "action_provenance": [p.to_dict() for p in self.action_provenance],
            "patient_provenance": [p.to_dict() for p in self.patient_provenance],
            "source_ids": list(self.source_ids),
            "considered_source_ids": list(self.considered_source_ids),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class Confirmation:
    """A frozen record of what a named person confirmed and on what basis."""

    by: str
    at: str
    basis: TaskSnapshot
    note: str | None = None
    acknowledged_issues: tuple[str, ...] = ()

    def to_dict(self, include_basis: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "by": self.by,
            "at": self.at,
            "note": self.note,
            "acknowledged_issues": list(self.acknowledged_issues),
        }
        if include_basis:
            payload["basis"] = self.basis.to_dict()
        return payload


@dataclass(frozen=True)
class Completion:
    by: str
    at: str
    basis: TaskSnapshot
    note: str | None = None

    def to_dict(self, include_basis: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {"by": self.by, "at": self.at, "note": self.note}
        if include_basis:
            payload["basis"] = self.basis.to_dict()
        return payload


@dataclass
class CareItem:
    """Workflow state around a stable item key.

    ``snapshot`` is always the latest applicable content.  Confirmed items
    show the latest content plus a review flag; completed items show the
    snapshot frozen at completion, while ``snapshot`` still carries newer
    material so nothing is silently hidden.
    """

    item_id: str
    status: str
    snapshot: TaskSnapshot
    revision: int = 1
    review_reasons: list[str] = field(default_factory=list)
    post_completion_changes: list[str] = field(default_factory=list)
    confirmations: list[Confirmation] = field(default_factory=list)
    completion: Completion | None = None
    history: list[TaskSnapshot] = field(default_factory=list)
    applied_source_ids: list[str] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def needs_review(self) -> bool:
        """True when the current content has reasons that still need a human.

        A confirmed item that changed is moved back to pending, so this is
        normally true exactly for pending items carrying review reasons.
        """

        return bool(self.review_reasons)

    @property
    def latest_confirmation(self) -> Confirmation | None:
        return self.confirmations[-1] if self.confirmations else None

    @property
    def confirmed_basis(self) -> TaskSnapshot | None:
        confirmation = self.latest_confirmation
        return confirmation.basis if confirmation else None

    @property
    def display_snapshot(self) -> TaskSnapshot:
        if self.status == STATUS_COMPLETED and self.completion is not None:
            return self.completion.basis
        return self.snapshot

    def to_dict(self, include_history: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "item_id": self.item_id,
            "status": self.status,
            "status_label": self.status_label,
            "revision": self.revision,
            "needs_review": self.needs_review,
            "review_reasons": list(self.review_reasons),
            "post_completion_changes": list(self.post_completion_changes),
            "display_snapshot": self.display_snapshot.to_dict(),
            "latest_snapshot": self.snapshot.to_dict(),
            "superseded_fingerprint": self.superseded_fingerprint(),
            "confirmed_basis": (
                self.confirmed_basis.to_dict() if self.confirmed_basis else None
            ),
            "confirmations": [
                confirmation.to_dict() for confirmation in self.confirmations
            ],
            "completion": (
                self.completion.to_dict() if self.completion is not None else None
            ),
        }
        if include_history:
            payload["history"] = [snapshot.to_dict() for snapshot in self.history]
        return payload

    def superseded_fingerprint(self) -> bool:
        """True when the item was confirmed/completed on outdated material."""

        basis = (
            self.completion.basis
            if self.status == STATUS_COMPLETED and self.completion is not None
            else self.confirmed_basis
        )
        return basis is not None and basis.fingerprint != self.snapshot.fingerprint


@dataclass(frozen=True)
class Observation:
    """A family report or clue.  It never becomes an order by itself."""

    patient_key: str
    patient_label: str
    kind: str
    text: str
    provenance: Provenance
    related_item_id: str | None = None
    proposed_action: str | None = None
    proposed_timing: Timing | None = None
    proposed_medication: str | None = None
    differs_from_formal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "patient_key": self.patient_key,
            "patient": self.patient_label,
            "kind": self.kind,
            "text": self.text,
            "related_item_id": self.related_item_id,
            "proposed_action": self.proposed_action,
            "proposed_timing": (
                self.proposed_timing.to_dict() if self.proposed_timing else None
            ),
            "proposed_medication": self.proposed_medication,
            "differs_from_formal": self.differs_from_formal,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class Note:
    """A handoff/administrative note retained verbatim with provenance."""

    patient_key: str
    patient_label: str
    kind: str
    text: str
    provenance: Provenance

    def to_dict(self) -> dict[str, Any]:
        return {
            "patient_key": self.patient_key,
            "patient": self.patient_label,
            "kind": self.kind,
            "text": self.text,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class Issue:
    """An extraction/reconciliation problem surfaced to the team."""

    code: str
    message: str
    severity: str = "warning"
    source_id: str | None = None
    quote: str | None = None
    patient_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "source_id": self.source_id,
            "quote": self.quote,
            "patient_key": self.patient_key,
        }


def snapshot_fingerprint(
    action_key: str,
    medication_key: str | None,
    fields: Mapping[str, FieldResolution],
    missing: Iterable[str],
) -> str:
    """Hash only clinical content, not which source happened to repeat it.

    This lets a newly arrived duplicate note update the evidence list without
    forcing a re-confirmation.  A changed value, conflict, or missing field
    does force review.
    """

    import hashlib
    import json

    payload = {
        "action": action_key,
        "medication": medication_key,
        "fields": {
            name: sorted(value.value for value in resolution.distinct)
            for name, resolution in sorted(fields.items())
        },
        "missing": sorted(missing),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def item_id_for(patient_key: str, action_key: str, medication_key: str | None) -> str:
    """Stable, readable key; aliases are canonicalised before this is called."""

    base = f"{patient_key or 'unassigned'}::{action_key}"
    if medication_key:
        base += f"::{medication_key}"
    return base


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------
# The state file stores confirmations as full snapshots.  These functions are
# the only place that turns those dicts back into value objects, so a frozen
# basis survives process restarts unchanged.


def provenance_from_dict(data: Mapping[str, Any]) -> Provenance:
    return Provenance(
        source_id=data["source_id"],
        source_kind=data["source_kind"],
        quote=data["quote"],
        start=int(data["start"]),
        end=int(data["end"]),
        version=int(data.get("version", 1)),
        author=data.get("author"),
        authority=data.get("authority", AUTHORITY_PROFESSIONAL),
    )


def timing_from_dict(data: Mapping[str, Any] | None) -> Timing | None:
    if not data:
        return None
    dimensions = tuple(
        (item["name"], item["value"], item["label"])
        for item in data.get("dimensions", [])
    )
    return Timing(dimensions)


def field_value_from_dict(data: Mapping[str, Any]) -> FieldValue:
    return FieldValue(
        field=data["field"],
        value=data["value"],
        label=data["label"],
        provenance=provenance_from_dict(data["provenance"]),
        correction=bool(data.get("correction", False)),
        lineage=tuple(data.get("lineage", ())),
        timing=timing_from_dict(data.get("timing")),
    )


def field_resolution_from_dict(data: Mapping[str, Any]) -> FieldResolution:
    return FieldResolution(
        field=data["field"],
        values=tuple(field_value_from_dict(item) for item in data.get("values", [])),
        superseded=tuple(
            field_value_from_dict(item) for item in data.get("superseded", [])
        ),
        preserved_by=tuple(
            provenance_from_dict(item) for item in data.get("preserved_by", [])
        ),
    )


def conflict_from_dict(data: Mapping[str, Any]) -> Conflict:
    return Conflict(
        field=data["field"],
        values=tuple(field_value_from_dict(item) for item in data.get("values", [])),
        message=data["message"],
    )


def snapshot_from_dict(data: Mapping[str, Any]) -> TaskSnapshot:
    fields_data = data.get("fields", {})
    if isinstance(fields_data, Mapping):
        fields = tuple(field_resolution_from_dict(item) for item in fields_data.values())
    else:  # tolerate an older list form
        fields = tuple(field_resolution_from_dict(item) for item in fields_data)
    return TaskSnapshot(
        item_id=data["item_id"],
        patient_key=data["patient_key"],
        patient_label=data["patient"],
        action_key=data["action_key"],
        action_label=data["action"],
        medication_key=data.get("medication_key"),
        medication_label=data.get("medication"),
        fields=fields,
        conflicts=tuple(conflict_from_dict(item) for item in data.get("conflicts", [])),
        missing=tuple(data.get("missing", ())),
        source_ids=tuple(data.get("source_ids", ())),
        considered_source_ids=tuple(data.get("considered_source_ids", ())),
        fingerprint=data["fingerprint"],
        action_provenance=tuple(
            provenance_from_dict(item) for item in data.get("action_provenance", [])
        ),
        patient_provenance=tuple(
            provenance_from_dict(item) for item in data.get("patient_provenance", [])
        ),
    )


def confirmation_from_dict(data: Mapping[str, Any]) -> Confirmation:
    return Confirmation(
        by=data["by"],
        at=data["at"],
        basis=snapshot_from_dict(data["basis"]),
        note=data.get("note"),
        acknowledged_issues=tuple(data.get("acknowledged_issues", ())),
    )


def completion_from_dict(data: Mapping[str, Any]) -> Completion:
    return Completion(
        by=data["by"],
        at=data["at"],
        basis=snapshot_from_dict(data["basis"]),
        note=data.get("note"),
    )
