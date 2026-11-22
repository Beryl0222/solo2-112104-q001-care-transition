"""Community care handoff reconciliation.

The package only processes synthetic care text.  It extracts nursing handoff
items from discharge records, corrections, family notes and outpatient
arrangements, preserves the exact supporting fragment for every judgement,
surfaces conflicts instead of resolving them, and tracks pending / confirmed /
completed work across incoming material.
"""

from .extract import (
    DEFAULT_VOCABULARY,
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
from .ledger import Ledger, build_ledger
from .models import (
    AUTHORITY_FAMILY,
    AUTHORITY_NOTE,
    AUTHORITY_PROFESSIONAL,
    FIELD_LABELS,
    STATUS_COMPLETED,
    STATUS_CONFIRMED,
    STATUS_LABELS,
    STATUS_PENDING,
    SYNTHETIC_DATA_NOTICE,
    CareItem,
    Completion,
    Confirmation,
    Conflict,
    FieldResolution,
    FieldValue,
    Issue,
    Note,
    Observation,
    Provenance,
    TaskSnapshot,
    Timing,
)
from .sources import Batch, SourceRecord, load_batch, load_sources, records_from_mappings
from .workflow import CareState, WorkflowError, snapshot_changes, utc_now

__all__ = [
    "AUTHORITY_FAMILY",
    "AUTHORITY_NOTE",
    "AUTHORITY_PROFESSIONAL",
    "Batch",
    "CareItem",
    "CareState",
    "Completion",
    "Confirmation",
    "Conflict",
    "Correction",
    "DEFAULT_VOCABULARY",
    "FIELD_LABELS",
    "FieldResolution",
    "FieldValue",
    "Issue",
    "Ledger",
    "Note",
    "Observation",
    "ParsedInstruction",
    "Provenance",
    "STATUS_COMPLETED",
    "STATUS_CONFIRMED",
    "STATUS_LABELS",
    "STATUS_PENDING",
    "SYNTHETIC_DATA_NOTICE",
    "Sentence",
    "SourceRecord",
    "TaskSnapshot",
    "Timing",
    "Vocabulary",
    "WorkflowError",
    "build_ledger",
    "find_actions",
    "find_medications",
    "find_patient",
    "find_role",
    "load_batch",
    "load_sources",
    "normalise_timing",
    "parse_correction",
    "parse_instruction_sentence",
    "records_from_mappings",
    "snapshot_changes",
    "split_clauses",
    "split_sentences",
    "utc_now",
]

__version__ = "1.0.0"
