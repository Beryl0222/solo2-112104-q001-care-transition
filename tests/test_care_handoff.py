import json
import tempfile
import unittest
from pathlib import Path

from care_handoff import (
    Batch,
    CareState,
    WorkflowError,
    build_ledger,
    find_actions,
    find_medications,
    find_patient,
    normalise_timing,
    records_from_mappings,
    split_clauses,
    split_sentences,
)


def ledger_for(*records):
    return build_ledger(Batch(tuple(records_from_mappings(records)), ()))


class ExtractionTests(unittest.TestCase):
    def test_aliases_merge_but_similar_names_stay_distinct(self):
        self.assertEqual(find_actions("量血压")[0][0], "血压测量")
        self.assertEqual(find_actions("测血压")[0][0], "血压测量")
        self.assertEqual(find_actions("血糖监测")[0][0], "血糖测量")
        # 血糖 is not silently treated as 血压.
        actions = [item[0] for item in find_actions("测血压和血糖")]
        self.assertIn("血压测量", actions)
        self.assertIn("血糖测量", actions)

    def test_medications_are_not_merged_by_prefix(self):
        found = [item[0] for item in find_medications("氨氯地平和二甲双胍")]
        self.assertEqual(found, ["氨氯地平", "二甲双胍"])

    def test_patient_name_is_not_merged_with_a_longer_name(self):
        known = ["林宁", "林宁安"]
        self.assertEqual(find_patient("林宁安今日复诊", known)[0], "林宁安")
        self.assertEqual(find_patient("林宁今日复诊", known)[0], "林宁")
        self.assertIsNone(find_patient("林宁安今日复诊", ["林宁"]))

    def test_patient_is_not_inferred_from_symptoms(self):
        self.assertIsNone(find_patient("家属说患者昨晚头晕", ["林宁"]))

    def test_timing_dimensions_are_independent(self):
        timing = normalise_timing("出院后每日两次，饭后服用")[0]
        names = {name for name, _, _ in timing.dimensions}
        self.assertEqual(names, {"frequency", "relation", "anchor"})

    def test_clause_splitting_keeps_offsets(self):
        text = "林宁出院后早晚测量血压，三日后复诊。"
        sentence = split_sentences(text)[0]
        clauses = split_clauses(sentence)
        self.assertEqual(text[clauses[0].start : clauses[0].end], clauses[0].text)
        self.assertEqual(text[clauses[1].start : clauses[1].end], clauses[1].text)


class LedgerTests(unittest.TestCase):
    def test_each_action_keeps_its_own_timing(self):
        ledger = ledger_for(
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "patient": "林宁",
                "text": "林宁出院后早晚测量血压，由社区护士负责，三日后复诊。",
            }
        )
        blood_pressure = ledger.snapshots["林宁::血压测量"]
        follow_up = ledger.snapshots["林宁::复诊"]
        self.assertIn("早晚", blood_pressure.field("timing").display)
        self.assertNotIn("3日", blood_pressure.field("timing").display)
        self.assertIn("3日后", follow_up.field("timing").display)

    def test_conflicting_formal_records_are_reported_not_resolved(self):
        ledger = ledger_for(
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "patient": "林宁",
                "text": "林宁出院后每日两次测量血压，由社区护士负责。",
            },
            {
                "id": "d2",
                "kind": "outpatient",
                "version": 1,
                "patient": "林宁",
                "text": "门诊记录：每日一次测量血压，由社区护士负责。",
            },
        )
        timing = ledger.snapshots["林宁::血压测量"].field("timing")
        self.assertTrue(timing.is_conflict)
        self.assertEqual({v.provenance.source_id for v in timing.distinct}, {"d1", "d2"})
        self.assertTrue(any(i.code == "field_conflict" for i in ledger.issues))

    def test_newer_sentence_does_not_win_by_itself(self):
        ledger = ledger_for(
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "patient": "林宁",
                "text": "林宁每日两次测量血压，由社区护士负责。",
            },
            {
                "id": "d2",
                "kind": "discharge",
                "version": 9,
                "patient": "林宁",
                "text": "林宁每日一次测量血压，由社区护士负责。",
            },
        )
        timing = ledger.snapshots["林宁::血压测量"].field("timing")
        self.assertTrue(timing.is_conflict, "version 9 must not silently override version 1")

    def test_correction_changes_only_the_named_condition(self):
        ledger = ledger_for(
            {
                "id": "a1",
                "kind": "discharge",
                "version": 1,
                "patient": "林宁",
                "text": "林宁出院后每日两次，饭后测量血压，由社区护士负责。",
            },
            {
                "id": "a2",
                "kind": "discharge_correction",
                "version": 2,
                "patient": "林宁",
                "text": "更正：测量频次改为每日一次；测量安排其余未作调整。",
            },
        )
        timing = ledger.snapshots["林宁::血压测量"].field("timing")
        self.assertFalse(timing.is_conflict)
        self.assertIn("每日1次", timing.display)
        self.assertIn("饭后", timing.display, "unchanged dimensions must survive")
        self.assertIn("出院后", timing.display)
        self.assertEqual(
            [v.value for v in timing.superseded], ["frequency:daily:2+relation:after_meals+anchor:after_discharge"]
        )
        # The unchanged scope is recorded as evidence, not as a new value.
        self.assertTrue(timing.preserved_by)

    def test_correction_does_not_resolve_an_unrelated_conflict(self):
        ledger = ledger_for(
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "patient": "林宁",
                "text": "林宁每日两次测量血压，由社区护士负责。",
            },
            {
                "id": "d2",
                "kind": "outpatient",
                "version": 1,
                "patient": "林宁",
                "text": "林宁每日一次测量血压，由社区护士负责。",
            },
            {
                "id": "c1",
                "kind": "discharge_correction",
                "version": 2,
                "patient": "林宁",
                "text": "更正：测量频次改为每日三次。",
            },
        )
        timing = ledger.snapshots["林宁::血压测量"].field("timing")
        self.assertTrue(timing.is_conflict)
        self.assertTrue(
            any(i.code == "correction_ambiguous_due_to_conflict" for i in ledger.issues)
        )

    def test_family_note_never_overrides_a_formal_order(self):
        ledger = ledger_for(
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "patient": "林宁",
                "text": "林宁每日两次测量血压，由社区护士负责。",
            },
            {
                "id": "f1",
                "kind": "family_note",
                "version": 1,
                "patient": "林宁",
                "text": "家属希望改为每日三次测量血压。",
            },
        )
        timing = ledger.snapshots["林宁::血压测量"].field("timing")
        self.assertFalse(timing.is_conflict)
        self.assertIn("每日2次", timing.display)
        observation = ledger.observations[0]
        self.assertTrue(observation.differs_from_formal)
        self.assertEqual(observation.related_item_id, "林宁::血压测量")

    def test_missing_and_conflict_are_distinguished(self):
        ledger = ledger_for(
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "patient": "林宁",
                "text": "林宁每日两次测量血压。",
            },
            {
                "id": "d2",
                "kind": "outpatient",
                "version": 1,
                "patient": "林宁",
                "text": "林宁每日一次测量血压，由社区护士负责。",
            },
        )
        snapshot = ledger.snapshots["林宁::血压测量"]
        self.assertTrue(snapshot.field("timing").is_conflict)
        self.assertTrue(snapshot.field("responsible").is_missing is False)
        self.assertIn("timing", [c.field for c in snapshot.conflicts])

    def test_every_value_carries_a_quote_from_the_source(self):
        record = {
            "id": "d1",
            "kind": "discharge",
            "version": 1,
            "patient": "林宁",
            "text": "林宁每日两次测量血压，由社区护士负责。",
        }
        ledger = ledger_for(record)
        snapshot = ledger.snapshots["林宁::血压测量"]
        for field in ("timing", "responsible"):
            for value in snapshot.field(field).values:
                provenance = value.provenance
                self.assertEqual(
                    record["text"][provenance.start : provenance.end],
                    provenance.quote,
                )
                self.assertIn(provenance.quote, record["text"])

    def test_accompaniment_is_not_treated_as_responsible(self):
        ledger = ledger_for(
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "patient": "林宁",
                "text": "林宁三日后复诊，由家属陪同。",
            }
        )
        snapshot = ledger.snapshots["林宁::复诊"]
        self.assertTrue(snapshot.field("responsible").is_missing)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "patient": "林宁",
                "text": "林宁出院后每日两次测量血压，由社区护士负责，三日后复诊。",
            }
        ]
        self.state = CareState()
        self.state.ingest(ledger_for(*self.records))

    def test_confirmation_requires_named_person_and_acknowledgement(self):
        item_id = "林宁::血压测量"
        with self.assertRaises(WorkflowError):
            self.state.confirm(item_id, by="")
        # Follow-up has no responsible person; confirming needs an explicit ack.
        with self.assertRaises(WorkflowError):
            self.state.confirm("林宁::复诊", by="王护士")
        item = self.state.confirm(
            "林宁::复诊", by="王护士", acknowledge=["missing:responsible"]
        )
        self.assertEqual(item.status, "confirmed")
        self.assertEqual(item.latest_confirmation.acknowledged_issues, ("missing:responsible",))

    def test_completion_cannot_skip_confirmation(self):
        with self.assertRaises(WorkflowError):
            self.state.complete("林宁::血压测量", by="王护士")

    def test_new_material_reopens_unfinished_items(self):
        item_id = "林宁::血压测量"
        self.state.confirm(item_id, by="王护士")
        changed = [
            dict(self.records[0]),
            {
                "id": "c1",
                "kind": "discharge_correction",
                "version": 2,
                "patient": "林宁",
                "text": "更正：测量频次改为每日一次。",
            },
        ]
        self.state.ingest(ledger_for(*changed))
        item = self.state.items[item_id]
        self.assertEqual(item.status, "pending")
        self.assertTrue(item.review_reasons)
        self.assertIn("每日1次", item.snapshot.field("timing").display)
        self.assertIn("每日2次", item.confirmed_basis.field("timing").display)

    def test_completed_item_keeps_the_basis_used_at_the_time(self):
        item_id = "林宁::血压测量"
        self.state.confirm(item_id, by="王护士")
        self.state.complete(item_id, by="王护士", note="已上门完成")
        changed = [
            dict(self.records[0]),
            {
                "id": "c1",
                "kind": "discharge_correction",
                "version": 2,
                "patient": "林宁",
                "text": "更正：测量频次改为每日一次。",
            },
        ]
        self.state.ingest(ledger_for(*changed))
        item = self.state.items[item_id]
        self.assertEqual(item.status, "completed")
        self.assertEqual(item.completion.basis.fingerprint, item.confirmed_basis.fingerprint)
        self.assertIn("每日2次", item.display_snapshot.field("timing").display)
        self.assertIn("每日1次", item.snapshot.field("timing").display)
        self.assertTrue(item.post_completion_changes)

    def test_completion_refused_when_material_changed_after_confirmation(self):
        item_id = "林宁::血压测量"
        self.state.confirm(item_id, by="王护士")
        changed = [
            dict(self.records[0]),
            {
                "id": "c1",
                "kind": "discharge_correction",
                "version": 2,
                "patient": "林宁",
                "text": "更正：测量频次改为每日一次。",
            },
        ]
        self.state.ingest(ledger_for(*changed))
        with self.assertRaises(WorkflowError):
            self.state.complete(item_id, by="王护士")

    def test_duplicate_material_does_not_force_reconfirmation(self):
        item_id = "林宁::血压测量"
        self.state.confirm(item_id, by="王护士")
        duplicate = dict(self.records[0])
        duplicate["id"] = "d1-copy"
        self.state.ingest(ledger_for(duplicate))
        item = self.state.items[item_id]
        self.assertEqual(item.status, "confirmed")
        self.assertFalse(item.review_reasons)

    def test_a_new_patient_batch_does_not_erase_another_patient(self):
        self.state.ingest(
            ledger_for(
                {
                    "id": "z1",
                    "kind": "discharge",
                    "version": 1,
                    "patient": "周敏",
                    "text": "周敏每日一次测量血糖，由社区护士负责。",
                }
            )
        )
        self.assertIn("林宁::血压测量", self.state.items)
        self.assertIn("周敏::血糖测量", self.state.items)

    def test_state_roundtrip_preserves_frozen_basis(self):
        item_id = "林宁::血压测量"
        self.state.confirm(item_id, by="王护士")
        self.state.complete(item_id, by="王护士")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.state.save(path)
            loaded = CareState.load(path)
        item = loaded.items[item_id]
        self.assertEqual(item.status, "completed")
        self.assertEqual(item.completion.by, "王护士")
        self.assertEqual(
            item.completion.basis.fingerprint, item.confirmed_basis.fingerprint
        )


class CliTests(unittest.TestCase):
    def test_board_groups_by_patient_and_shows_sources(self):
        from care_handoff.cli import main

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            source = Path(directory) / "care.json"
            source.write_text(
                json.dumps(
                    [
                        {
                            "id": "d1",
                            "kind": "discharge",
                            "version": 1,
                            "patient": "林宁",
                            "text": "林宁每日两次测量血压，由社区护士负责。",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(main(["ingest", str(source), "--state", str(state)]), 0)
            exit_code = main(
                ["board", "林宁", "--state", str(state), "--json"]
            )
            self.assertEqual(exit_code, 0)


class SourceDataTests(unittest.TestCase):
    def test_sample_batch_builds_two_items_with_correction(self):
        from care_handoff.sources import load_batch

        data = Path(__file__).parents[1] / "data" / "care.json"
        ledger = build_ledger(load_batch(data))
        self.assertEqual(
            set(ledger.snapshots), {"林宁::血压测量", "林宁::复诊"}
        )
        follow_up = ledger.snapshots["林宁::复诊"]
        self.assertIn("2日后", follow_up.field("timing").display)
        self.assertEqual(
            [v.value for v in follow_up.field("timing").superseded],
            ["anchor:after_days:3"],
        )
        blood_pressure = ledger.snapshots["林宁::血压测量"]
        self.assertTrue(blood_pressure.field("timing").preserved_by)


if __name__ == "__main__":
    unittest.main()


class SeparateBatchTests(unittest.TestCase):
    """A correction usually arrives in a later file than the record it edits."""

    def test_correction_in_a_later_batch_finds_its_target(self):
        state = CareState()
        state.ingest(
            ledger_for(
                {
                    "id": "a1",
                    "kind": "discharge",
                    "version": 1,
                    "patient": "林宁",
                    "text": "林宁出院后每日两次测量血压，由社区护士负责，三日后复诊。",
                }
            )
        )
        state.confirm("林宁::血压测量", by="王护士")
        state.ingest(
            ledger_for(
                {
                    "id": "a3",
                    "kind": "discharge_correction",
                    "version": 2,
                    "patient": "林宁",
                    "text": "更正：测量频次改为每日一次；测量安排其余未作调整。",
                }
            )
        )
        item = state.items["林宁::血压测量"]
        self.assertIn("每日1次", item.snapshot.field("timing").display)
        self.assertIn("出院后", item.snapshot.field("timing").display)
        self.assertEqual(item.status, "pending")
        self.assertIn("每日2次", item.confirmed_basis.field("timing").display)

    def test_earlier_sources_survive_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = CareState()
            state.ingest(
                ledger_for(
                    {
                        "id": "a1",
                        "kind": "discharge",
                        "version": 1,
                        "patient": "林宁",
                        "text": "林宁每日两次测量血压，由社区护士负责。",
                    }
                )
            )
            state.save(path)
            loaded = CareState.load(path)
        loaded.ingest(
            ledger_for(
                {
                    "id": "a3",
                    "kind": "discharge_correction",
                    "version": 2,
                    "patient": "林宁",
                    "text": "更正：测量频次改为每日一次。",
                }
            )
        )
        self.assertIn(
            "每日1次", loaded.items["林宁::血压测量"].snapshot.field("timing").display
        )


class MedicationScopingTests(unittest.TestCase):
    def test_two_medications_create_two_items_with_their_own_schedules(self):
        ledger = ledger_for(
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "patient": "赵强",
                "text": "赵强每日两次服用氨氯地平，由社区护士负责；"
                "每日一次服用二甲双胍，由家属负责。",
            }
        )
        self.assertEqual(
            set(ledger.snapshots),
            {"赵强::服药::氨氯地平", "赵强::服药::二甲双胍"},
        )
        amlodipine = ledger.snapshots["赵强::服药::氨氯地平"]
        metformin = ledger.snapshots["赵强::服药::二甲双胍"]
        self.assertIn("每日2次", amlodipine.field("timing").display)
        self.assertIn("社区护士", amlodipine.field("responsible").display)
        self.assertIn("每日1次", metformin.field("timing").display)
        self.assertIn("家属", metformin.field("responsible").display)

    def test_duplicate_medication_mentions_do_not_merge(self):
        ledger = ledger_for(
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "patient": "赵强",
                "text": "赵强每日服用阿司匹林一次，由社区护士负责。",
            },
            {
                "id": "d2",
                "kind": "outpatient",
                "version": 1,
                "patient": "赵强",
                "text": "每日服用华法林一次，由社区护士负责。",
            },
        )
        self.assertEqual(
            set(ledger.snapshots),
            {"赵强::服药::阿司匹林", "赵强::服药::华法林"},
        )

    def test_correction_naming_one_medication_leaves_the_other_alone(self):
        ledger = ledger_for(
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "patient": "赵强",
                "text": "赵强每日两次服用氨氯地平，每日一次服用二甲双胍，由社区护士负责。",
            },
            {
                "id": "c1",
                "kind": "discharge_correction",
                "version": 2,
                "patient": "赵强",
                "text": "更正：二甲双胍改为每日两次。",
            },
        )
        amlodipine = ledger.snapshots["赵强::服药::氨氯地平"]
        metformin = ledger.snapshots["赵强::服药::二甲双胍"]
        self.assertIn("每日2次", amlodipine.field("timing").display)
        self.assertEqual(amlodipine.field("timing").superseded, ())
        self.assertIn("每日2次", metformin.field("timing").display)
        self.assertTrue(metformin.field("timing").superseded)


class DistinctionTests(unittest.TestCase):
    def test_similar_action_names_are_not_merged(self):
        ledger = ledger_for(
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "patient": "林宁",
                "text": "林宁每日两次测量血压，每日一次测量血糖，由社区护士负责。",
            }
        )
        self.assertEqual(
            set(ledger.snapshots), {"林宁::血压测量", "林宁::血糖测量"}
        )

    def test_missing_patient_is_reported_not_guessed(self):
        ledger = ledger_for(
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "text": "每日两次测量血压，由社区护士负责。",
            }
        )
        self.assertEqual(ledger.snapshots, {})
        self.assertTrue(any(i.code == "missing_patient" for i in ledger.issues))


class SharedRoleTests(unittest.TestCase):
    def test_trailing_role_across_two_actions_is_flagged(self):
        ledger = ledger_for(
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "patient": "赵强",
                "text": "赵强每日两次服用氨氯地平，每日一次服用二甲双胍，由社区护士负责。",
            }
        )
        self.assertTrue(
            any(i.code == "shared_responsible" for i in ledger.issues),
            "a role that plausibly covers two actions must be flagged",
        )

    def test_role_attaches_only_to_preceding_action(self):
        ledger = ledger_for(
            {
                "id": "d1",
                "kind": "discharge",
                "version": 1,
                "patient": "林宁",
                "text": "测量血压由社区护士负责，三日后复诊。",
            }
        )
        self.assertIn(
            "社区护士", ledger.snapshots["林宁::血压测量"].field("responsible").display
        )
        self.assertTrue(
            ledger.snapshots["林宁::复诊"].field("responsible").is_missing
        )
