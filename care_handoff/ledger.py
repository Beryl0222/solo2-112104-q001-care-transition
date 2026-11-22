"""Build handoff items from a batch of synthetic source records.

Pipeline
--------

1. Every professional statement about a recognised action becomes a
   ``FieldValue`` with exact provenance.
2. Explicit corrections are applied *narrowly*: the corrected clause names
   its target, and only that action's named field is replaced.  A correction
   never re-interprets the rest of a record and never becomes a new order.
3. A correction that names no specific action but a category ("测量安排")
   is applied only when exactly one current item matches.  Ambiguity is
   surfaced, not guessed.
4. A correction is *not* applied to a field that already has conflicting
   professional values: the team must see the conflict and decide which
   record the correction supersedes.
5. Distinct professional values for the same field remain a conflict.  No
   value is chosen because it is newer or because its source has a higher
   version.
6. Family notes become observations.  They never create or override an
   order; when they report a different schedule the observation is marked
   ``differs_from_formal`` for review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .extract import (
    DEFAULT_VOCABULARY,
    NO_CHANGE_MARKER,
    Correction,
    ParsedInstruction,
    Sentence,
    Vocabulary,
    find_actions,
    find_medications,
    find_patient,
    find_role,
    normalise_timing,
    parse_correction,
    parse_instruction_sentence,
    split_clauses,
    split_sentences,
)
from .models import (
    AUTHORITY_FAMILY,
    AUTHORITY_NOTE,
    AUTHORITY_PROFESSIONAL,
    FIELD_LABELS,
    Conflict,
    FieldResolution,
    FieldValue,
    Issue,
    Note,
    Observation,
    Provenance,
    TaskSnapshot,
    Timing,
    item_id_for,
    snapshot_fingerprint,
)
from .sources import Batch, SourceRecord

FIELD_ORDER = ("timing", "responsible", "medication")
REQUIRED_FIELDS = ("timing", "responsible")
MEDICATION_ACTIONS = {"服药"}


@dataclass
class DraftItem:
    patient_key: str
    patient_label: str
    action_key: str
    medication_key: str | None
    values: dict[str, list[FieldValue]] = field(default_factory=dict)
    superseded: dict[str, list[FieldValue]] = field(default_factory=dict)
    preserved_by: dict[str, list[Provenance]] = field(default_factory=dict)
    source_ids: set[str] = field(default_factory=set)
    action_provenance: list[Provenance] = field(default_factory=list)
    patient_provenance: list[Provenance] = field(default_factory=list)

    @property
    def item_id(self) -> str:
        return item_id_for(self.patient_key, self.action_key, self.medication_key)

    def add_value(self, field_name: str, value: FieldValue) -> None:
        self.values.setdefault(field_name, []).append(value)
        self.source_ids.add(value.provenance.source_id)
        self.action_provenance.append(value.provenance)

    def add_patient_provenance(self, provenance: Provenance) -> None:
        if provenance.quote and provenance not in self.patient_provenance:
            self.patient_provenance.append(provenance)


@dataclass
class Ledger:
    snapshots: dict[str, TaskSnapshot]
    observations: list[Observation]
    notes: list[Note]
    issues: list[Issue]
    sources: dict[str, SourceRecord]

    @property
    def patients(self) -> list[tuple[str, str]]:
        seen: dict[str, str] = {}
        for snapshot in self.snapshots.values():
            seen.setdefault(snapshot.patient_key, snapshot.patient_label)
        for observation in self.observations:
            seen.setdefault(observation.patient_key, observation.patient_label)
        for note in self.notes:
            if note.patient_key:
                seen.setdefault(note.patient_key, note.patient_label)
        return sorted(seen.items(), key=lambda pair: pair[0])

    def items_for(self, patient_key: str) -> list[TaskSnapshot]:
        return sorted(
            (s for s in self.snapshots.values() if s.patient_key == patient_key),
            key=lambda s: (s.action_key, s.medication_key or ""),
        )

    def observations_for(self, patient_key: str) -> list[Observation]:
        return [o for o in self.observations if o.patient_key == patient_key]

    def notes_for(self, patient_key: str) -> list[Note]:
        return [n for n in self.notes if n.patient_key == patient_key]

    def issues_for(self, patient_key: str) -> list[Issue]:
        return [
            issue for issue in self.issues if issue.patient_key in (None, patient_key)
        ]


def _provenance_for(
    record: SourceRecord, text: str, start: int, end: int
) -> Provenance:
    return Provenance(
        source_id=record.id,
        source_kind=record.kind,
        quote=text[start:end],
        start=start,
        end=end,
        version=record.version,
        author=record.author,
        authority=record.authority,
    )


def _instruction_provenance(record: SourceRecord, sentence: Sentence) -> Provenance:
    return Provenance(
        source_id=record.id,
        source_kind=record.kind,
        quote=sentence.text,
        start=sentence.start,
        end=sentence.end,
        version=record.version,
        author=record.author,
        authority=record.authority,
    )


def _existing_patients(records: Iterable[SourceRecord]) -> list[str]:
    known: list[str] = []
    for record in records:
        if record.patient and record.patient not in known:
            known.append(record.patient)
            continue
        found = find_patient(record.text, known)
        if found and found[0] not in known:
            known.append(found[0])
    return known


def build_ledger(
    batch: Batch, *, vocabulary: Vocabulary | None = None
) -> Ledger:
    vocabulary = vocabulary or DEFAULT_VOCABULARY
    sources = batch.by_id()
    drafts: dict[str, DraftItem] = {}
    observations: list[Observation] = []
    notes: list[Note] = []
    issues: list[Issue] = list(batch.issues)

    known_patients = _existing_patients(batch.records)
    current_patient: str | None = None
    # Corrections are applied after the base statements so that file order
    # inside one batch cannot make a statement silently win over another.
    pending_corrections: list[tuple[SourceRecord, Correction]] = []

    for record in batch.records:
        patient = _resolve_patient(record, known_patients, current_patient)
        if patient is not None and patient[0]:
            current_patient = patient[0]

        if record.is_correction:
            corrections, correction_issues = parse_correction(
                record.text, source_id=record.id, version=record.version
            )
            issues.extend(correction_issues)
            for correction in corrections:
                pending_corrections.append((record, correction))
            continue

        if record.authority == AUTHORITY_FAMILY:
            observations.extend(
                _family_observations(record, patient, vocabulary, issues)
            )
            continue

        if record.authority == AUTHORITY_NOTE:
            notes.append(_note(record, patient))
            continue

        if patient is None:
            issues.append(
                Issue(
                    code="missing_patient",
                    message="正式记录没有可识别的患者，未生成交接事项。",
                    severity="error",
                    source_id=record.id,
                    quote=record.text,
                )
            )
            continue

        issues.extend(_formal_instructions(record, patient, drafts, vocabulary))

    for record, correction in pending_corrections:
        issues.extend(_apply_correction(record, correction, drafts, sources))

    observations = [_attach_observation(item, drafts) for item in observations]

    snapshots = {
        item_id: _snapshot_from_draft(draft)
        for item_id, draft in sorted(drafts.items())
        if draft.values
    }
    issues.extend(_global_issues(snapshots, observations))

    return Ledger(
        snapshots=snapshots,
        observations=observations,
        notes=notes,
        issues=issues,
        sources=sources,
    )


def _resolve_patient(
    record: SourceRecord, known_patients: list[str], current: str | None
) -> tuple[str, str, int, int] | None:
    if record.patient:
        found = find_patient(record.text, [record.patient])
        start = found[2] if found else 0
        end = found[3] if found else 0
        return record.patient, record.patient, start, end
    found = find_patient(record.text, known_patients)
    if found is not None:
        return found
    if current is not None and not record.is_correction:
        # Continuation notes often omit the name.  Carry the patient only
        # within a batch whose file order already established one.
        return current, current, 0, 0
    return None


def _formal_instructions(
    record: SourceRecord,
    patient: tuple[str, str, int, int],
    drafts: dict[str, DraftItem],
    vocabulary: Vocabulary,
) -> list[Issue]:
    patient_key, patient_label, patient_start, patient_end = patient
    issues: list[Issue] = []
    for sentence in split_sentences(record.text):
        clauses = split_clauses(sentence)
        instructions: list[ParsedInstruction] = []
        for clause in clauses:
            parsed = parse_instruction_sentence(
                clause,
                source_id=record.id,
                source_kind=record.kind,
                version=record.version,
                authority=record.authority,
            )
            for instruction in parsed:
                instruction.sentence = clause
                instructions.append(instruction)
        if not instructions:
            continue

        # A timing clause split off by a comma still belongs to the action:
        # "出院后每日两次，饭后测量血压" and "测量血压，每日两次".  Attach
        # it to the nearest action clause, and flag the pairing when the
        # sentence contains several equally close actions.
        action_clauses = {id(instruction.sentence) for instruction in instructions}
        for clause in clauses:
            if id(clause) in action_clauses:
                continue
            timing = normalise_timing(clause.text)
            if timing is None or NO_CHANGE_MARKER.search(clause.text):
                continue
            candidates = sorted(
                (
                    instruction
                    for instruction in instructions
                    if instruction.sentence is not None
                ),
                key=lambda instruction: abs(instruction.sentence.start - clause.start),
            )
            if not candidates:
                continue
            target = candidates[0]
            distances = [
                abs(instruction.sentence.start - clause.start)
                for instruction in candidates
            ]
            if distances.count(distances[0]) > 1:
                issues.append(
                    Issue(
                        code="ambiguous_timing_attachment",
                        message=(
                            f"“{clause.text}”中的时段与多个护理动作等距，"
                            "未自动附加，需人工核对。"
                        ),
                        severity="warning",
                        source_id=record.id,
                        quote=clause.text,
                    )
                )
                continue
            if target.timing is None:
                target.timing = timing[0]
            else:
                target.timing = target.timing.with_dimensions(timing[0])
            target.issues = [
                issue for issue in target.issues if issue.code != "missing_timing"
            ]

        # "……测量血压，由社区护士负责。"  A role-only clause belongs to the
        # action nearest *before* it in the same sentence, not to whichever
        # action happened to be parsed last.  A clause that merely repeats an
        # unchanged scope ("测量安排未作调整") is not a role clause either.
        action_clauses = {id(instruction.sentence) for instruction in instructions}
        for clause in clauses:
            if id(clause) in action_clauses:
                continue
            if NO_CHANGE_MARKER.search(clause.text):
                continue
            role = find_role(clause.text, vocabulary)
            if role is None:
                continue
            canonical, role_start, role_end = role
            if _role_is_negated(clause.text, role_start):
                continue
            if _is_accompaniment(clause.text, role_end):
                issues.append(
                    Issue(
                        code="accompaniment_not_responsible",
                        message=f"“{clause.text}”只说明陪同，未据此认定负责人员。",
                        severity="info",
                        source_id=record.id,
                        quote=clause.text,
                    )
                )
                continue
            # Only actions that appear *before* the role clause can be its
            # subject.  A later action must not inherit a role stated earlier.
            previous = [
                instruction
                for instruction in instructions
                if instruction.sentence is not None
                and instruction.sentence.end <= clause.start
            ]
            targets = [
                instruction
                for instruction in previous
                if instruction.responsible is None
            ]
            if not targets:
                # Do not overwrite a role stated next to the action itself.
                # A different role in a following clause is a genuine
                # difference for the team to see, not a silent replacement.
                continue
            for target in targets:
                target.responsible = canonical
                target.issues = [
                    issue
                    for issue in target.issues
                    if issue.code != "missing_responsible"
                ]
            if len(targets) > 1:
                # "……服用A，……服用B，由社区护士负责。"  The trailing role
                # plausibly covers both, but the record does not say so
                # explicitly.  Attach it to both and let the team verify.
                issues.append(
                    Issue(
                        code="shared_responsible",
                        message=(
                            f"“{clause.text}”出现在多个护理动作之后，"
                            "已暂按共同负责人员记录，需人工核对是否均适用。"
                        ),
                        severity="warning",
                        source_id=record.id,
                        quote=clause.text,
                    )
                )

        for instruction in instructions:
            issues.extend(instruction.issues)
            if instruction.action_key is None:
                continue
            if instruction.action_negated:
                issues.append(
                    Issue(
                        code="negated_instruction",
                        message="正式记录含否定表述，未据此生成新事项。",
                        severity="info",
                        source_id=record.id,
                        quote=instruction.quote,
                    )
                )
                continue
            if instruction.timing_is_negated:
                # "频次不变，由社区护士负责" still assigns a responsible
                # person; only the timing is left as previously recorded.
                issues.append(
                    Issue(
                        code="unchanged_timing",
                        message="记录明确说明时段未调整，保留原时段，不新增取值。",
                        severity="info",
                        source_id=record.id,
                        quote=instruction.quote,
                    )
                )

            medication_key = instruction.medication_key
            if instruction.medication_is_negated:
                medication_key = None
            item_id = item_id_for(patient_key, instruction.action_key, medication_key)
            draft = drafts.get(item_id)
            if draft is None:
                draft = DraftItem(
                    patient_key=patient_key,
                    patient_label=patient_label,
                    action_key=instruction.action_key,
                    medication_key=medication_key,
                )
                drafts[item_id] = draft

            provenance = _instruction_provenance(
                record, instruction.sentence or sentence
            )
            if patient_start != patient_end:
                draft.add_patient_provenance(
                    _provenance_for(record, record.text, patient_start, patient_end)
                )
            _add_instruction_values(draft, instruction, provenance)
    return issues


def _role_is_negated(text: str, start: int) -> bool:
    before = text[max(0, start - 4) : start]
    return any(before.rstrip().endswith(prefix) for prefix in ("不", "未", "无需", "不必"))


ACCOMPANIMENT = ("陪同", "陪伴", "护送", "随行")


def _is_accompaniment(text: str, role_end: int) -> bool:
    """Return True when the role only accompanies the patient.

    "复诊由家属陪同" says who comes along, not who is responsible for the
    nursing action.  Treating it as the responsible party would invent an
    assignment the record never made.
    """

    after = text[role_end : role_end + 4]
    return any(word in after for word in ACCOMPANIMENT)


def _add_instruction_values(
    draft: DraftItem,
    instruction: ParsedInstruction,
    provenance: Provenance,
) -> None:
    if instruction.timing is not None:
        draft.add_value(
            "timing",
            FieldValue(
                field="timing",
                value=instruction.timing.key,
                label=instruction.timing.label,
                provenance=provenance,
                timing=instruction.timing,
            ),
        )
    if instruction.responsible is not None:
        draft.add_value(
            "responsible",
            FieldValue(
                field="responsible",
                value=instruction.responsible,
                label=instruction.responsible,
                provenance=provenance,
            ),
        )
    if instruction.medication_key is not None:
        draft.add_value(
            "medication",
            FieldValue(
                field="medication",
                value=instruction.medication_key,
                label=instruction.medication_label or instruction.medication_key,
                provenance=provenance,
            ),
        )


def _apply_correction(
    record: SourceRecord,
    correction: Correction,
    drafts: dict[str, DraftItem],
    sources: dict[str, SourceRecord],
) -> list[Issue]:
    issues: list[Issue] = []
    targets = _matching_drafts(correction, drafts, issues, record)
    if not targets:
        if not any(i.source_id == record.id for i in issues):
            issues.append(
                Issue(
                    code="correction_target_not_found",
                    message="更正未能唯一对应到现有事项，原文已保留，未自动改写任何事项。",
                    severity="warning",
                    source_id=record.id,
                    quote=correction.quote,
                )
            )
        return issues

    provenance = Provenance(
        source_id=record.id,
        source_kind=record.kind,
        quote=correction.quote,
        start=correction.quote_start,
        end=correction.quote_end,
        version=record.version,
        author=record.author,
        authority=AUTHORITY_PROFESSIONAL,
    )

    for draft in targets:
        draft.source_ids.add(record.id)
        if correction.field == "no_change":
            for field_name in correction.no_change_fields:
                draft.preserved_by.setdefault(field_name, []).append(provenance)
            continue

        previous = draft.values.get(correction.field, [])
        distinct_previous = _distinct(previous)
        if len(distinct_previous) > 1:
            # Two professional records already disagree.  A correction with
            # no explicit source link cannot tell us which one it replaces;
            # keep the conflict and let a clinician choose.
            issues.append(
                Issue(
                    code="correction_ambiguous_due_to_conflict",
                    message=(
                        f"更正要改写的“{FIELD_LABELS.get(correction.field, correction.field)}”"
                        "已存在相互矛盾的专业记录，系统未自行选择被更正的句子。"
                    ),
                    severity="warning",
                    source_id=record.id,
                    quote=correction.quote,
                    patient_key=draft.patient_key,
                )
            )
            continue

        if correction.field == "timing" and correction.timing is not None:
            if previous:
                new_values = [
                    _replace_timing_dimensions(value, correction.timing, provenance)
                    for value in previous
                ]
            else:
                new_values = [
                    FieldValue(
                        field="timing",
                        value=correction.timing.key,
                        label=correction.timing.label,
                        provenance=provenance,
                        correction=True,
                        timing=correction.timing,
                    )
                ]
        elif correction.field in {"medication", "responsible"} and correction.value:
            new_values = [
                FieldValue(
                    field=correction.field,
                    value=correction.value,
                    label=correction.value,
                    provenance=provenance,
                    correction=True,
                )
            ]
        else:
            issues.append(
                Issue(
                    code="unapplied_correction",
                    message="更正内容无法安全应用到现有字段，已保留原文待人工核对。",
                    source_id=record.id,
                    quote=correction.quote,
                    patient_key=draft.patient_key,
                )
            )
            continue

        _replace_field(draft, correction.field, new_values, record)
    return issues


def _matching_drafts(
    correction: Correction,
    drafts: dict[str, DraftItem],
    issues: list[Issue],
    record: SourceRecord,
) -> list[DraftItem]:
    if correction.target_action is not None:
        candidates = [
            draft
            for draft in drafts.values()
            if draft.action_key == correction.target_action
            and (not record.patient or draft.patient_key == record.patient)
            and (
                correction.target_medication is None
                or draft.medication_key == correction.target_medication
            )
        ]
        patients = {draft.patient_key for draft in candidates}
        if len(patients) > 1 and not record.patient:
            issues.append(
                Issue(
                    code="ambiguous_correction_target",
                    message="更正命名的护理动作对应多名患者，未自动改写。",
                    source_id=record.id,
                    quote=correction.quote,
                )
            )
            return []
        return candidates

    if correction.target_hint == "measurement":
        candidates = [
            draft
            for draft in drafts.values()
            if "测量" in draft.action_key
            and (not record.patient or draft.patient_key == record.patient)
        ]
        if len(candidates) == 1:
            return candidates
        issues.append(
            Issue(
                code="ambiguous_correction_target",
                message="更正只笼统提到“测量安排”，对应当前事项不唯一，未自动改写。",
                source_id=record.id,
                quote=correction.quote,
            )
        )
    return []


def _replace_timing_dimensions(
    previous: FieldValue, replacement: Timing, provenance: Provenance
) -> FieldValue:
    """Replace only the dimensions named by the correction.

    If the prior statement said "出院后，早晚各一次" and the correction only
    changes the frequency, the anchor stays.  Non-timing dimensions are never
    silently dropped by a narrower correction.
    """

    merged = _timing_of(previous).with_dimensions(replacement)
    return FieldValue(
        field="timing",
        value=merged.key,
        label=merged.label,
        provenance=provenance,
        correction=True,
        lineage=tuple(dict.fromkeys(previous.lineage + (provenance.source_id,))),
        timing=merged,
    )


def _timing_of(value: FieldValue) -> Timing:
    """Structured dimensions for a timing value.

    Prefers the stored object.  Falls back to parsing ``value`` only for
    state written by an older version; the label is rebuilt from the same
    table the extractor uses.
    """

    if value.timing is not None:
        return value.timing
    dimensions: list[tuple[str, str, str]] = []
    for chunk in value.value.split("+"):
        if ":" not in chunk:
            continue
        name, raw = chunk.split(":", 1)
        kind, _, number = raw.partition(":")
        dimensions.append((name, raw, _timing_label(kind, number)))
    return Timing(tuple(dimensions))


TIMING_LABELS = {
    "daily": "每日",
    "morning_evening": "早晚各",
    "nightly": "每晚",
    "morning": "每早",
    "every_other_day": "隔日",
    "weekly": "每周",
    "before_meals": "饭前",
    "after_meals": "饭后",
    "bedtime": "睡前",
    "after_days": "日后",
    "next_day": "次日",
    "tomorrow": "明日",
    "today": "今日",
    "after_discharge": "出院后",
    "continuous": "持续",
}


def _timing_label(kind: str, number: str) -> str:
    prefix = TIMING_LABELS.get(kind, kind)
    if not number:
        return prefix
    if kind == "after_days":
        return f"{number}日后"
    if kind in {"morning_evening"}:
        return f"早晚各{number}次"
    if kind in {"nightly", "morning", "every_other_day"}:
        return f"{prefix}{number}次"
    if kind in {"daily", "weekly"}:
        return f"{prefix}{number}次"
    return f"{prefix}{number}"


def _replace_field(
    draft: DraftItem,
    field_name: str,
    new_values: list[FieldValue],
    record: SourceRecord,
) -> None:
    old_values = draft.values.get(field_name, [])
    draft.superseded.setdefault(field_name, [])
    for old in old_values:
        draft.superseded[field_name].append(old)
    lineage: list[str] = []
    for old in old_values:
        lineage.extend(old.lineage)
    lineage.append(record.id)
    draft.values[field_name] = [
        FieldValue(
            field=value.field,
            value=value.value,
            label=value.label,
            provenance=value.provenance,
            correction=True,
            lineage=tuple(dict.fromkeys(lineage)),
            timing=value.timing,
        )
        for value in new_values
    ]
    draft.source_ids.add(record.id)


def _family_observations(
    record: SourceRecord,
    patient: tuple[str, str, int, int] | None,
    vocabulary: Vocabulary,
    issues: list[Issue],
) -> list[Observation]:
    if patient is None or not patient[0]:
        issues.append(
            Issue(
                code="missing_patient",
                message="家属补充没有可识别的患者，未归入任何患者。",
                severity="warning",
                source_id=record.id,
                quote=record.text,
            )
        )
        return []
    patient_key, patient_label, _, _ = patient
    output: list[Observation] = []
    for sentence in split_sentences(record.text):
        for clause in split_clauses(sentence):
            actions = find_actions(clause.text, vocabulary)
            medications = find_medications(clause.text, vocabulary)
            timing = normalise_timing(clause.text)
            output.append(
                Observation(
                    patient_key=patient_key,
                    patient_label=patient_label,
                    kind=record.kind,
                    text=clause.text,
                    provenance=_provenance_for(
                        record, record.text, clause.start, clause.end
                    ),
                    proposed_action=actions[0][0] if actions else None,
                    proposed_timing=timing[0] if timing else None,
                    proposed_medication=medications[0][0] if medications else None,
                )
            )
    return output


def _attach_observation(
    observation: Observation, drafts: dict[str, DraftItem]
) -> Observation:
    if observation.proposed_action is None:
        return observation
    candidates = [
        draft
        for draft in drafts.values()
        if draft.action_key == observation.proposed_action
        and draft.patient_key == observation.patient_key
    ]
    if len(candidates) != 1:
        return observation
    draft = candidates[0]
    differs = _family_differs(observation, draft)
    return Observation(
        patient_key=observation.patient_key,
        patient_label=observation.patient_label,
        kind=observation.kind,
        text=observation.text,
        provenance=observation.provenance,
        related_item_id=draft.item_id,
        proposed_action=observation.proposed_action,
        proposed_timing=observation.proposed_timing,
        proposed_medication=observation.proposed_medication,
        differs_from_formal=differs,
    )


def _family_differs(observation: Observation, draft: DraftItem) -> bool:
    if observation.proposed_timing is None:
        return False
    formal = draft.values.get("timing", [])
    if not formal:
        return False
    proposed = observation.proposed_timing
    dimension_names = {name for name, _, _ in proposed.dimensions}
    formal_dimension_values: set[str] = set()
    for value in formal:
        parsed = _timing_of(value)
        formal_dimension_values.update(
            f"{name}:{raw}"
            for name, raw, _ in parsed.dimensions
            if name in dimension_names
        )
    proposed_values = {f"{name}:{raw}" for name, raw, _ in proposed.dimensions}
    return bool(formal_dimension_values) and not proposed_values <= formal_dimension_values


def _note(record: SourceRecord, patient: tuple[str, str, int, int] | None) -> Note:
    patient_key = patient[0] if patient else ""
    patient_label = patient[1] if patient else ""
    return Note(
        patient_key=patient_key,
        patient_label=patient_label,
        kind=record.kind,
        text=record.text,
        provenance=Provenance(
            source_id=record.id,
            source_kind=record.kind,
            quote=record.text,
            start=0,
            end=len(record.text),
            version=record.version,
            author=record.author,
            authority=AUTHORITY_NOTE,
        ),
    )


def _snapshot_from_draft(draft: DraftItem) -> TaskSnapshot:
    fields: list[FieldResolution] = []
    conflicts: list[Conflict] = []
    missing: list[str] = []

    for field_name in FIELD_ORDER:
        values = tuple(_distinct(draft.values.get(field_name, [])))
        resolution = FieldResolution(
            field=field_name,
            values=values,
            superseded=tuple(_distinct(draft.superseded.get(field_name, []))),
            preserved_by=tuple(draft.preserved_by.get(field_name, [])),
        )
        fields.append(resolution)
        required = field_name in REQUIRED_FIELDS or (
            field_name == "medication" and draft.action_key in MEDICATION_ACTIONS
        )
        if required and resolution.is_missing:
            missing.append(field_name)
        if resolution.is_conflict:
            labels = " / ".join(value.label for value in resolution.distinct)
            quoted = "；".join(
                f"“{value.provenance.quote}”（{value.provenance.source_id}）"
                for value in resolution.distinct
            )
            conflicts.append(
                Conflict(
                    field=field_name,
                    values=resolution.distinct,
                    message=(
                        f"{FIELD_LABELS.get(field_name, field_name)}存在不一致：{labels}。"
                        f"原文出处：{quoted}。需由有权限的医护人员核对，系统不自行选择。"
                    ),
                )
            )

    field_map = {resolution.field: resolution for resolution in fields}
    fingerprint = snapshot_fingerprint(
        draft.action_key, draft.medication_key, field_map, missing
    )
    source_ids: set[str] = set(draft.source_ids)
    for resolution in fields:
        for value in resolution.values:
            source_ids.update(value.lineage)
        for value in resolution.superseded:
            source_ids.update(value.lineage)

    return TaskSnapshot(
        item_id=draft.item_id,
        patient_key=draft.patient_key,
        patient_label=draft.patient_label,
        action_key=draft.action_key,
        action_label=draft.action_key,
        medication_key=draft.medication_key,
        medication_label=draft.medication_key,
        fields=tuple(fields),
        conflicts=tuple(conflicts),
        missing=tuple(missing),
        source_ids=tuple(sorted(source_ids)),
        considered_source_ids=tuple(sorted(draft.source_ids)),
        fingerprint=fingerprint,
        action_provenance=tuple(dict.fromkeys(draft.action_provenance)),
        patient_provenance=tuple(dict.fromkeys(draft.patient_provenance)),
    )


def _distinct(values: list[FieldValue]) -> list[FieldValue]:
    seen: set[str] = set()
    output: list[FieldValue] = []
    for value in values:
        if value.value in seen:
            continue
        seen.add(value.value)
        output.append(value)
    return output


def _global_issues(
    snapshots: dict[str, TaskSnapshot],
    observations: list[Observation],
) -> list[Issue]:
    issues: list[Issue] = []
    for snapshot in snapshots.values():
        for conflict in snapshot.conflicts:
            issues.append(
                Issue(
                    code="field_conflict",
                    message=f"{snapshot.item_id}：{conflict.message}",
                    severity="warning",
                    patient_key=snapshot.patient_key,
                    quote=None,
                )
            )
        if snapshot.missing:
            labels = "、".join(
                FIELD_LABELS.get(name, name) for name in snapshot.missing
            )
            issues.append(
                Issue(
                    code="missing_field",
                    message=f"{snapshot.item_id} 缺少：{labels}。",
                    severity="info",
                    patient_key=snapshot.patient_key,
                )
            )
    for observation in observations:
        if observation.differs_from_formal:
            issues.append(
                Issue(
                    code="family_note_differs",
                    message=(
                        f"{observation.patient_label} 的家属补充与正式安排不一致，"
                        "已作为观察线索保留，未覆盖专业安排。"
                    ),
                    severity="info",
                    source_id=observation.provenance.source_id,
                    quote=observation.text,
                    patient_key=observation.patient_key,
                )
            )
    return issues
