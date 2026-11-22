"""Extraction from synthetic Chinese discharge and handoff text.

The extractor is intentionally conservative and rule-based:

* it only recognises a small, documented vocabulary;
* it never infers a patient from a pronoun or a medication from a similar
  name;
* every extracted value keeps exact character offsets into the source text.

When a sentence does not match a supported pattern the caller receives an
``Issue`` rather than a guessed value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .models import Issue, Timing

# ---------------------------------------------------------------------------
# Vocabulary and aliases
# ---------------------------------------------------------------------------

# Canonical actions and the surface forms that refer to the same nursing act.
# Aliases are explicit.  Similar-looking names that are not listed here are
# kept as separate actions on purpose.
ACTION_ALIASES: dict[str, tuple[str, ...]] = {
    "血压测量": (
        "测量血压",
        "测血压",
        "量血压",
        "测量一次血压",
        "血压测量",
        "监测血压",
        "自测血压",
    ),
    "复诊": ("复诊", "门诊复诊", "返院复诊", "到门诊复查", "复查"),
    "血糖测量": ("测量血糖", "测血糖", "血糖监测", "血糖测量"),
    "服药": ("按时服药", "服药", "吃药", "用药", "服用", "口服"),
    "伤口换药": ("换药", "伤口换药", "更换敷料", "敷料更换"),
    "体温测量": ("测量体温", "测体温", "体温测量", "监测体温"),
    "体重测量": ("测量体重", "测体重", "称体重", "体重测量"),
    "饮水记录": ("记录饮水量", "饮水记录", "记录喝水"),
}

# Medications are a separate namespace.  Do not infer a medication from an
# action word, and do not merge drugs that merely share a prefix.
MEDICATION_ALIASES: dict[str, tuple[str, ...]] = {
    "氨氯地平": ("氨氯地平", "苯磺酸氨氯地平", "络活喜"),
    "二甲双胍": ("二甲双胍", "格华止"),
    "阿司匹林": ("阿司匹林", "拜阿司匹灵"),
    "华法林": ("华法林",),
    "胰岛素": ("胰岛素",),
}

# Roles, not named people.  A role is the responsible party when the record
# names no person.
ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "社区护士": ("社区护士", "责任护士", "护士"),
    "家属": ("家属", "患者家属", "女儿", "儿子", "配偶", "老伴"),
    "患者本人": ("患者本人", "患者自己", "病人自己", "自行"),
    "全科医生": ("全科医生", "家庭医生", "社区医生", "医生"),
    # Bare "门诊" is excluded: "门诊记录："/"门诊安排：" is a document header,
    # not a responsible party.
    "门诊团队": ("门诊团队", "复诊门诊", "门诊护士"),
}

# Person names are recognised only when they appear in a known patient list.
# Patterns are loaded from the data file's explicit ``patients`` metadata if
# present; otherwise names must match this conservative form and be announced
# as a patient ("患者X"/"X患者") rather than inferred from a bare name.

PATIENT_PATTERNS = (
    # "患者林宁" / "患者林宁主诉" — the name must be followed by a boundary
    # word, so "患者昨晚头晕" is not misread as a patient named 昨晚头晕.
    re.compile(
        r"(?:患者|病人)\s*(?P<name>[一-鿿]{2,3})"
        r"(?=出院|，|,|。|；|;|主诉|家属|说|今日|今早|昨晚|复诊|入院)"
    ),
    # "林宁患者"
    re.compile(
        r"(?P<name>[一-鿿]{2,3})(?=患者|病人)"
    ),
    # "林宁出院" at the start of a record
    re.compile(r"^(?P<name>[一-鿿]{2,3})(?=出院)"),
)

# Tokens that the loose 2-4 character patient patterns can capture but that
# are not names.  A bare unlisted token is never accepted as a patient.
BARE_NAME_BOUNDARIES = set("，,。；;：:、\n ”」》说称")

# A bare known name is accepted only when followed by one of these starter
# characters.  "林宁复诊" is a continuation; "林宁安复诊" is not "林宁".
BARE_NAME_FOLLOWERS = set("复出家门按由测服血体换饮明今次周每早晚饭睡，,。；;")

NOT_A_NAME = {
    "昨晚",
    "昨晚起",
    "今早",
    "今天",
    "今日",
    "明天",
    "明日",
    "昨天",
    "目前",
    "现在",
    "近日",
    "近期",
    "家属",
    "本人",
    "患者",
    "病人",
    "出院",
    "门诊",
    "社区",
    "医院",
    "护士",
    "医生",
    "家属说",
    "家属称",
    "家属反映",
    "患者说",
    "患者称",
    "昨日",
    "昨日",
    "次日",
    "当天",
    "当日",
}

TIMING_PATTERNS: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    # pattern, dimension, key kind, label template; "{n}" is the repeat count
    (re.compile(r"每日\s*(?P<n>[一二三四五六七八九十两\d]+)\s*次"), "frequency", "daily", "每日{n}次"),
    (re.compile(r"每天\s*(?P<n>[一二三四五六七八九十两\d]+)\s*次"), "frequency", "daily", "每日{n}次"),
    (re.compile(r"早晚各?\s*(?P<n>[一二三四五六七八九十两\d]+)?\s*次?"), "frequency", "morning_evening", "早晚各{n}次"),
    (re.compile(r"每晚\s*(?P<n>[一二三四五六七八九十两\d]+)?\s*次?"), "frequency", "nightly", "每晚{n}次"),
    (re.compile(r"每早\s*(?P<n>[一二三四五六七八九十两\d]+)?\s*次?"), "frequency", "morning", "每早{n}次"),
    (re.compile(r"隔日\s*(?P<n>[一二三四五六七八九十两\d]+)?\s*次?"), "frequency", "every_other_day", "隔日{n}次"),
    (re.compile(r"每周\s*(?P<n>[一二三四五六七八九十两\d]+)\s*次"), "frequency", "weekly", "每周{n}次"),
    (re.compile(r"饭前(?:半小时)?"), "relation", "before_meals", "饭前"),
    (re.compile(r"饭后(?:半小时)?"), "relation", "after_meals", "饭后"),
    (re.compile(r"睡前"), "relation", "bedtime", "睡前"),
    (re.compile(r"(?P<n>[一二三四五六七八九十两\d]+)\s*日后"), "anchor", "after_days", "{n}日后"),
    (re.compile(r"(?P<n>[一二三四五六七八九十两\d]+)\s*天后"), "anchor", "after_days", "{n}日后"),
    (re.compile(r"次日"), "anchor", "next_day", "次日"),
    (re.compile(r"明日"), "anchor", "tomorrow", "明日"),
    (re.compile(r"今日"), "anchor", "today", "今日"),
    (re.compile(r"出院后"), "anchor", "after_discharge", "出院后"),
    (re.compile(r"持续"), "duration", "continuous", "持续"),
)

CN_NUMBERS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

# Sentence splitting keeps the delimiter out of the quote, so quotes are short
# and copy-pasteable into the original record.
SENTENCE_SPLIT = re.compile(r"(?<=[。；;！？!?\n])")


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Vocabulary:
    """Explicit alias tables used for one deployment.

    Aliases are opt-in: two surface forms are the same action only when the
    deployment lists them together.  Callers can supply their own tables
    without editing this module.
    """

    actions: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(ACTION_ALIASES)
    )
    medications: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(MEDICATION_ALIASES)
    )
    roles: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(ROLE_ALIASES))
    timing_patterns: tuple[tuple[re.Pattern[str], str, str, str], ...] = TIMING_PATTERNS

    def find_actions(self, text: str) -> list[tuple[str, str, int, int]]:
        return find_actions(text, self)

    def find_medications(self, text: str) -> list[tuple[str, int, int]]:
        return find_medications(text, self)

    def find_role(self, text: str) -> tuple[str, int, int] | None:
        return find_role(text, self)

    def action_for_surface(self, surface: str) -> str | None:
        for canonical, aliases in self.actions.items():
            if surface == canonical or surface in aliases:
                return canonical
        return None


DEFAULT_VOCABULARY = Vocabulary()


def _vocabulary_or_default(vocabulary: Vocabulary | None) -> Vocabulary:
    return vocabulary if vocabulary is not None else DEFAULT_VOCABULARY


def _cn_to_int(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return CN_NUMBERS.get(token)


def _timing_from_match(
    dimension: str, kind: str, label: str, token: str | None
) -> Timing | None:
    number = _cn_to_int(token) if token else None
    if kind in {"daily", "weekly", "after_days"} and number is None:
        return None
    if token is not None and number is None:
        return None

    if kind in {"morning_evening", "nightly", "morning", "every_other_day"}:
        number = number or 1
        display = label.format(n=number)
    elif number is not None:
        display = label.format(n=number)
    else:
        display = label
    value_key = f"{kind}:{number}" if number is not None else kind
    return Timing(((dimension, value_key, display),))


def normalise_timings(text: str) -> list[tuple[Timing, int, int]]:
    """Find every non-overlapping timing token and merge its dimensions.

    A clause can legitimately carry several dimensions at once, e.g.
    "每日两次，饭后服用".  Keeping the dimensions separate avoids silently
    dropping the meal relation when the frequency is the more obvious match.
    """

    matches: list[tuple[int, int, Timing]] = []
    for pattern, dimension, kind, label in TIMING_PATTERNS:
        for match in pattern.finditer(text):
            timing = _timing_from_match(
                dimension, kind, label, match.groupdict().get("n")
            )
            if timing is None:
                continue
            matches.append((match.start(), match.end(), timing))
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    chosen: list[tuple[int, int, Timing]] = []
    for start, end, timing in matches:
        if any(start < other_end and other_start < end for other_start, other_end, _ in chosen):
            continue
        chosen.append((start, end, timing))
    chosen.sort(key=lambda item: item[0])

    merged: Timing | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    for token_start, token_end, timing in chosen:
        merged = timing if merged is None else merged.with_dimensions(timing)
        start_offset = token_start if start_offset is None else min(start_offset, token_start)
        end_offset = token_end if end_offset is None else max(end_offset, token_end)
    if merged is None:
        return []
    return [(merged, start_offset or 0, end_offset or 0)]


def normalise_timing(text: str) -> tuple[Timing, int, int] | None:
    """Return the combined timing dimensions of a clause."""

    found = normalise_timings(text)
    return found[0] if found else None


def find_medications(
    text: str, vocabulary: Vocabulary | None = None
) -> list[tuple[str, int, int]]:
    vocabulary = _vocabulary_or_default(vocabulary)
    taken: list[tuple[int, int]] = []
    candidates: list[tuple[int, int, str]] = []
    for canonical, aliases in vocabulary.medications.items():
        for alias in aliases:
            for match in re.finditer(re.escape(alias), text):
                candidates.append((match.start(), match.end(), canonical))
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    found: list[tuple[str, int, int]] = []
    for start, end, canonical in candidates:
        if any(a <= start < b or a < end <= b for a, b in taken):
            continue
        found.append((canonical, start, end))
        taken.append((start, end))
    return sorted(found, key=lambda item: item[1])


MEASUREMENT_OBJECTS: dict[str, str] = {
    "血压": "血压测量",
    "血糖": "血糖测量",
    "体温": "体温测量",
    "体重": "体重测量",
}
MEASUREMENT_VERBS = ("测量", "监测", "自测", "测", "量")


def _coordinated_measurements(text: str) -> list[tuple[str, str, int, int]]:
    """Handle "测量血压和血糖" without inventing tasks from bare nouns.

    Two similar names are never merged; instead each listed object becomes
    its own action.  A standalone "血压偏高" has no verb and is left alone.
    """

    output: list[tuple[str, str, int, int]] = []
    pattern = re.compile(
        r"(?P<verb>" + "|".join(MEASUREMENT_VERBS) + r")"
        r"\s*(?P<objects>(?:血压|血糖|体温|体重)"
        r"(?:[、和及与](?:血压|血糖|体温|体重))+)"
    )
    for match in pattern.finditer(text):
        objects_part = match.group("objects")
        base_start = match.start("objects")
        for object_match in re.finditer(r"血压|血糖|体温|体重", objects_part):
            canonical = MEASUREMENT_OBJECTS[object_match.group(0)]
            start = base_start + object_match.start()
            end = base_start + object_match.end()
            output.append((canonical, object_match.group(0), start, end))
    return output


def find_actions(
    text: str, vocabulary: Vocabulary | None = None
) -> list[tuple[str, str, int, int]]:
    """Return canonical action, matched surface form, start, end.

    Longer aliases win so "血压测量" does not also produce the bare "测量".
    """

    vocabulary = _vocabulary_or_default(vocabulary)
    candidates: list[tuple[int, int, str, str]] = []
    for canonical, aliases in vocabulary.actions.items():
        for alias in aliases:
            for match in re.finditer(re.escape(alias), text):
                candidates.append((match.start(), match.end(), canonical, alias))
    for canonical, alias, start, end in _coordinated_measurements(text):
        candidates.append((start, end, canonical, alias))
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    output: list[tuple[str, str, int, int]] = []
    covered: list[tuple[int, int]] = []
    for start, end, canonical, alias in candidates:
        if any(start < other_end and other_start < end for other_start, other_end in covered):
            continue
        output.append((canonical, alias, start, end))
        covered.append((start, end))
    return output


def find_role(
    text: str, vocabulary: Vocabulary | None = None
) -> tuple[str, int, int] | None:
    vocabulary = _vocabulary_or_default(vocabulary)
    best: tuple[str, int, int] | None = None
    for canonical, aliases in vocabulary.roles.items():
        for alias in aliases:
            for match in re.finditer(re.escape(alias), text):
                if best is None or match.start() < best[1]:
                    best = (canonical, match.start(), match.end())
    return best


def find_patient(
    text: str, known_patients: Iterable[str] = ()
) -> tuple[str, str, int, int] | None:
    """Return patient key/surface form only when a patient is announced.

    A bare name is not enough.  ``known_patients`` may contain canonical names
    that were established by an earlier explicit mention in the same batch.
    """

    known = sorted((name for name in known_patients if name), key=len, reverse=True)
    for pattern in PATIENT_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group("name")
            for role in ("出院后", "出院", "家属", "本人", "患者", "病人"):
                if name.endswith(role) and len(name) > len(role) + 1:
                    name = name[: -len(role)]
                    break
            name = name.strip()
            if not name or name in NOT_A_NAME:
                continue
            if any(name.startswith(prefix) for prefix in NOT_A_NAME):
                continue
            return name, name, match.start(), match.end()
    # Only explicitly listed names are accepted from bare mentions, and only
    # at a plausible boundary.  The follower check alone prevents "林宁" from
    # matching inside "林宁安": a longer known name is tried first, and an
    # unknown continuation character makes the shorter match invalid.
    for name in known:
        for match in re.finditer(re.escape(name), text):
            before = text[match.start() - 1] if match.start() > 0 else ""
            after = text[match.end() : match.end() + 1]
            if before and before not in BARE_NAME_BOUNDARIES:
                continue
            if after and after not in BARE_NAME_FOLLOWERS:
                continue
            # A phrase like "家属说林宁..." names the patient after a speech
            # verb.  Accept it, but never let a longer unlisted token swallow
            # the name (checked by the follower set above).
            return name, name, match.start(), match.end()
    return None


@dataclass
class Sentence:
    text: str
    start: int
    end: int


SENTENCE_TAIL = "。；;！？!?\n "


def split_sentences(text: str) -> list[Sentence]:
    """Split on sentence delimiters and drop the delimiter from the quote.

    Quotes stay copy-pasteable into the original record, and a trailing "；"
    does not make two correction clauses look like one fragment.
    """

    sentences: list[Sentence] = []
    cursor = 0
    for chunk in SENTENCE_SPLIT.split(text):
        if not chunk.strip():
            cursor += len(chunk)
            continue
        stripped = chunk.strip().rstrip(SENTENCE_TAIL).strip()
        if not stripped:
            cursor += len(chunk)
            continue
        offset = chunk.index(stripped)
        start = cursor + offset
        sentences.append(Sentence(text=stripped, start=start, end=start + len(stripped)))
        cursor += len(chunk)
    return sentences


CLAUSE_SPLIT = re.compile(r"(?<=[，,、])")


def split_clauses(sentence: Sentence) -> list[Sentence]:
    """Split one sentence on commas, preserving offsets.

    Clause-level splitting is what keeps "早晚测量血压，三日后复诊" from
    attaching both timings to both actions.
    """

    clauses: list[Sentence] = []
    cursor = 0
    for chunk in CLAUSE_SPLIT.split(sentence.text):
        if not chunk.strip():
            cursor += len(chunk)
            continue
        stripped = chunk.strip().rstrip("，,、").strip()
        if not stripped:
            cursor += len(chunk)
            continue
        offset = chunk.index(stripped)
        start = sentence.start + cursor + offset
        clauses.append(Sentence(stripped, start, start + len(stripped)))
        cursor += len(chunk)
    return clauses


@dataclass
class ParsedInstruction:
    """One formal or family statement about an action."""

    action_key: str | None
    action_label: str | None
    medication_key: str | None
    medication_label: str | None
    timing: Timing | None
    responsible: str | None
    action_negated: bool = False
    timing_is_negated: bool = False
    medication_is_negated: bool = False
    responsible_is_negated: bool = False
    responsible_is_accompaniment: bool = False
    quote: str = ""
    quote_start: int = 0
    quote_end: int = 0
    sentence: Sentence | None = None
    issues: list[Issue] = field(default_factory=list)


NEGATION_PREFIXES = ("不", "未", "无需", "不必", "暂停", "停用", "取消", "别")


def _is_negated(text: str, start: int, window: int = 4) -> bool:
    before = text[max(0, start - window) : start]
    return any(before.rstrip().endswith(prefix) for prefix in NEGATION_PREFIXES)


ACCOMPANIMENT = ("陪同", "陪伴", "护送", "随行")


def _is_accompaniment(text: str, role_end: int) -> bool:
    after = text[role_end : role_end + 4]
    return any(word in after for word in ACCOMPANIMENT)


def parse_instruction_sentence(
    sentence: Sentence,
    *,
    source_id: str,
    source_kind: str,
    version: int,
    authority: str,
) -> list[ParsedInstruction]:
    """Extract every action mentioned in one *clause*.

    Callers pass clauses rather than whole sentences so that
    "早晚测量血压，三日后复诊" does not attach both timings to both actions.
    A clause can still contain two actions ("测量血压和血糖"); each gets its
    own instruction and shares the clause's timing when there is only one.
    """

    text = sentence.text
    actions = find_actions(text)
    medications = find_medications(text)
    role = find_role(text)

    instructions: list[ParsedInstruction] = []

    if not actions:
        # A statement with no recognised action cannot become a task.  It is
        # still returned so callers can retain it as a note or observation.
        return instructions

    for action_key, alias, act_start, act_end in actions:
        # A drug applies to the nearest action.  "饭前服用二甲双胍" puts the
        # verb before the drug; "二甲双胍饭前服用" puts it after.
        preceding = [
            (canonical, start, end)
            for canonical, start, end in medications
            if start < act_start
        ]
        following = [
            (canonical, start, end)
            for canonical, start, end in medications
            if start >= act_end
        ]
        med_pairing = preceding[-1:] if preceding else following[:1]
        med_key = med_pairing[0][0] if med_pairing else None

        instruction = ParsedInstruction(
            action_key=action_key,
            action_label=action_key,
            medication_key=med_key,
            medication_label=med_key,
            timing=None,
            responsible=None,
            quote=text,
            quote_start=0,
            quote_end=len(text),
            sentence=sentence,
        )

        timing = normalise_timings(text)
        if timing:
            if NO_CHANGE_MARKER.search(text):
                instruction.timing_is_negated = True
            elif len(actions) > 1 and len(medications) == 0:
                # Several actions share one clause: the timing plausibly
                # applies to all of them, but the pairing is not certain.
                instruction.issues.append(
                    Issue(
                        code="shared_timing",
                        message="同一分句含多个护理动作，时段是否适用于每一项需人工核对。",
                        source_id=source_id,
                        quote=text,
                    )
                )
                instruction.timing = timing[0][0]
            else:
                instruction.timing = timing[0][0]
        elif not NO_CHANGE_MARKER.search(text):
            instruction.issues.append(
                Issue(
                    code="missing_timing",
                    message=f"语句提到“{action_key}”，但没有可识别的执行时段。",
                    source_id=source_id,
                    quote=text,
                )
            )

        if role is not None:
            role_key, role_start, role_end = role
            if _is_negated(text, role_start):
                instruction.responsible_is_negated = True
            elif _is_accompaniment(text, role_end):
                # "由家属陪同" names a companion, not the person responsible
                # for the nursing action.  Do not invent an assignment.
                instruction.responsible_is_accompaniment = True
                instruction.issues.append(
                    Issue(
                        code="accompaniment_not_responsible",
                        message=f"“{text}”只说明陪同，未据此认定负责人员。",
                        severity="info",
                        source_id=source_id,
                        quote=text,
                    )
                )
            else:
                instruction.responsible = role_key
        else:
            instruction.issues.append(
                Issue(
                    code="missing_responsible",
                    message=f"语句提到“{action_key}”，但没有可识别的负责人员或角色。",
                    source_id=source_id,
                    quote=text,
                )
            )

        if med_key and any(_is_negated(text, start) for _, start, _ in med_pairing):
            instruction.medication_is_negated = True
            instruction.medication_key = None
            instruction.medication_label = None

        instruction.quote = text
        instructions.append(instruction)

    if len(actions) > 1 and len(medications) > len(actions):
        for instruction in instructions:
            instruction.issues.append(
                Issue(
                    code="ambiguous_medication",
                    message="同一句中出现多个药物，无法可靠判断各自对应哪项护理动作。",
                    source_id=source_id,
                    quote=text,
                )
            )

    return instructions


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------

CORRECTION_MARKER = re.compile(r"更正|修正|更改|改为|调整为|订正")
CORRECTION_TARGET = re.compile(
    r"(?P<field>复诊时间|复诊|测量安排|血压安排|血压测量|测量|服药时间|服药|用药|药物|剂量|频次|时间|负责人员|责任人)"
)
NO_CHANGE_MARKER = re.compile(r"(?:未作调整|未调整|不变|无变化|照旧|维持原状|仍按原安排)")


@dataclass
class Correction:
    """An explicit replacement extracted from a correction source.

    ``field`` is ``timing``, ``medication``, ``responsible`` or ``no_change``.
    ``target_action`` names the action only when the source names it.  A
    generic target such as "测量安排" becomes ``target_hint`` instead; the
    ledger resolves it only when exactly one existing item matches, never by
    guessing between several similar actions.
    """

    field: str
    target_action: str | None = None
    target_hint: str | None = None
    target_medication: str | None = None
    value: str | None = None
    timing: Timing | None = None
    quote: str = ""
    quote_start: int = 0
    quote_end: int = 0
    no_change_fields: tuple[str, ...] = ()
    version: int = 1
    source_id: str = ""


CHANGE_PATTERN = re.compile(r"(?:改为|调整为|改成|变更为|订正为)")


def parse_correction(
    text: str, *, source_id: str, version: int
) -> tuple[list[Correction], list[Issue]]:
    """Parse an explicit correction statement.

    Supported shape: "更正：复诊时间改为两日后；测量安排未作调整。"
    Each clause is scoped on its own.  "未作调整" confirms that the named
    action keeps its prior values; it never becomes a new value.
    """

    corrections: list[Correction] = []
    issues: list[Issue] = []
    if not CORRECTION_MARKER.search(text):
        return corrections, issues

    for sentence in split_sentences(text):
        for clause in split_clauses(sentence):
            clause_text = clause.text
            if CORRECTION_MARKER.search(clause_text):
                clause_text = CORRECTION_MARKER.sub("", clause_text, count=1)
                clause_text = clause_text.lstrip("：:，,、 ")

            if NO_CHANGE_MARKER.search(clause_text):
                corrections.append(_no_change_correction(clause_text, clause, source_id, version))
                continue

            change = CHANGE_PATTERN.search(clause_text)
            if not change:
                if "更正" in sentence.text or "修正" in sentence.text:
                    issues.append(
                        Issue(
                            code="unparsed_correction",
                            message="更正语句未说明被替换的新值，已保留原文但未自动应用。",
                            source_id=source_id,
                            quote=clause_text,
                        )
                    )
                continue

            before = clause_text[: change.start()]
            after = clause_text[change.end() :].strip()
            target_action, target_hint, target_medication = _correction_target(before)

            timing = normalise_timing(after)
            medication = find_medications(after)
            role = find_role(after)

            if timing is not None:
                field_name = "timing"
                value = timing[0].key
                timing_value: Timing | None = timing[0]
            elif medication:
                field_name = "medication"
                value = medication[0][0]
                timing_value = None
            elif role is not None and ("负责" in before or "责任" in before):
                field_name = "responsible"
                value = role[0]
                timing_value = None
            else:
                field_name = "timing"
                value = after or None
                timing_value = None
                issues.append(
                    Issue(
                        code="ambiguous_correction_value",
                        message="更正语句给出了新表述，但无法对应到受支持的字段。",
                        source_id=source_id,
                        quote=clause_text,
                    )
                )
                if not after:
                    continue

            corrections.append(
                Correction(
                    field=field_name,
                    target_action=target_action,
                    target_hint=target_hint,
                    target_medication=target_medication,
                    value=value,
                    timing=timing_value,
                    quote=clause_text,
                    quote_start=clause.start,
                    quote_end=clause.end,
                    version=version,
                    source_id=source_id,
                )
            )
    if not corrections:
        issues.append(
            Issue(
                code="unparsed_correction",
                message="检测到更正标记，但没有可应用的更正条款。",
                source_id=source_id,
                quote=text,
            )
        )
    return corrections, issues


def _no_change_correction(
    clause_text: str, clause: Sentence, source_id: str, version: int
) -> Correction:
    target_action, target_hint, target_medication = _correction_target(clause_text)
    no_change_fields: list[str] = []
    if any(word in clause_text for word in ("测量", "血压", "血糖", "体温", "体重", "时间", "频次")):
        no_change_fields.append("timing")
    if "复诊" in clause_text:
        no_change_fields.append("timing")
    if "药" in clause_text or "剂量" in clause_text:
        no_change_fields.append("medication")
    if "负责" in clause_text or "责任人" in clause_text:
        no_change_fields.append("responsible")
    if not no_change_fields:
        # "测量安排未作调整" and similar scope statements preserve every
        # clinical field of the named action, not just timing.
        no_change_fields.extend(("timing", "responsible", "medication"))
    return Correction(
        field="no_change",
        target_action=target_action,
        target_hint=target_hint,
        target_medication=target_medication,
        quote=clause_text,
        quote_start=clause.start,
        quote_end=clause.end,
        no_change_fields=tuple(no_change_fields),
        version=version,
        source_id=source_id,
    )


def _correction_target(
    before: str,
) -> tuple[str | None, str | None, str | None]:
    """Return the correction's explicit target.

    The third value is a medication named on its own ("二甲双胍改为每日
    两次").  Without it, such a correction could only fall back to "服药"
    and would silently rewrite every medication item.
    """

    actions = find_actions(before)
    medications = find_medications(before)
    target_medication = medications[0][0] if medications else None
    if actions:
        return actions[0][0], None, target_medication
    target = CORRECTION_TARGET.search(before)
    token = target.group("field") if target else None
    if token is None:
        if target_medication is not None:
            return "服药", None, target_medication
        return None, None, None
    canonical = _canonicalise_target(token)
    if canonical is not None:
        return canonical, None, target_medication
    if "测量" in token:
        return None, "measurement", target_medication
    return None, None, target_medication


def _canonicalise_target(target: str | None) -> str | None:
    if not target:
        return None
    if "复诊" in target or "复查" in target:
        return "复诊"
    if "血压" in target:
        return "血压测量"
    if "血糖" in target:
        return "血糖测量"
    if "体温" in target:
        return "体温测量"
    if "体重" in target:
        return "体重测量"
    if "换药" in target or "敷料" in target:
        return "伤口换药"
    if "饮水" in target:
        return "饮水记录"
    if "服药" in target or "药物" in target or "剂量" in target or "用药" in target:
        return "服药"
    return None


__all__ = [
    "ACTION_ALIASES",
    "MEDICATION_ALIASES",
    "ROLE_ALIASES",
    "Correction",
    "ParsedInstruction",
    "Sentence",
    "find_actions",
    "find_medications",
    "find_patient",
    "find_role",
    "normalise_timing",
    "parse_correction",
    "parse_instruction_sentence",
    "split_clauses",
    "split_sentences",
]
