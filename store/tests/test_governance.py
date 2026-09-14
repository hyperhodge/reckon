#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Classification and presentation mode -- §2.1, §8.5. An earlier session (PLAN.md row 6).

Usage: python3 -m unittest discover -s store/tests

Own fixtures in a temp dir. Nothing here reads ~/.claude, the archive, the real
store or the network, and nothing here can reach a binary.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "store"))

import store            # noqa: E402
import governance       # noqa: E402


class Temp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        for name in ("projects", "tasks", "runs", "store", "ledger", "decisions"):
            (self.root / name).mkdir()
        self.refs = self.root / "store" / "refs.json"

    def tearDown(self):
        self.dir.cleanup()

    def register(self):
        return store.RefRegister.load(self.refs)

    def project(self, ref, **fields):
        record = {"ref": ref, "name": f"name-of-{ref}", "one_liner": "a one liner",
                  "stage": "captured"}
        record.update(fields)
        store.write_record(self.root / "projects" / f"{ref}.md", record, "")
        return record


class TestProviderNotDeployer(Temp):
    """§10 query 2, added to classification_queries() in an earlier session rather than
    built as a second classification reader somewhere else."""

    def test_provider_and_both_are_reported_separately(self):
        self.project("P-0001", my_role="provider")
        self.project("P-0002", my_role="both")
        self.project("P-0003", my_role="deployer")
        self.project("P-0004", my_role="n/a")
        loaded = store.load_store(self.root)
        result = governance.classification_queries(loaded)["provider_not_deployer"]
        self.assertEqual(result["provider"], ["P-0001"])
        self.assertEqual(result["both"], ["P-0002"])
        self.assertEqual(result["deployer"], ["P-0003"])
        self.assertEqual(result["not_applicable"], ["P-0004"])

    def test_a_project_with_no_role_is_unclassified_not_a_deployer(self):
        """Absent is not a value. Reading a blank as 'deployer' would be the
        register answering a governance question nobody answered."""
        self.project("P-0005")
        loaded = store.load_store(self.root)
        result = governance.classification_queries(loaded)["provider_not_deployer"]
        self.assertEqual(result["unclassified"], ["P-0005"])
        self.assertEqual(result["deployer"], [])

    def test_a_value_outside_the_vocabulary_is_not_silently_bucketed(self):
        self.project("P-0006", my_role="vendor")
        loaded = store.load_store(self.root)
        queries = governance.classification_queries(loaded)
        self.assertIn("P-0006", queries["provider_not_deployer"]["unclassified"])
        self.assertIn("my_role", queries["invalid_values"]["P-0006"])


# ----------------------------------------------------------- classification

class TestClassification(Temp):

    def test_is_ai_system_is_three_valued_and_never_a_bool(self):
        """§2.1: yes | no | contested. Two thirds of it is not the whole field,
        and `contested` is the honest answer for the interesting cases."""
        self.assertEqual(store.CLASSIFICATION_VOCABULARY["is_ai_system"],
                         ["yes", "no", "contested"])
        for value in ("yes", "no", "contested"):
            result = store.classify({"ref": "P-0009", "is_ai_system": value})
            self.assertEqual(result["fields"]["is_ai_system"]["value"], value)
            self.assertNotIsInstance(result["fields"]["is_ai_system"]["value"], bool)
            self.assertEqual(result["fields"]["is_ai_system"]["state"], "set")

    def test_the_parser_does_not_collapse_yes_and_no(self):
        fields, _ = store.parse_record("---\nref: P-0009\nis_ai_system: no\n---\n")
        self.assertEqual(fields["is_ai_system"], "no")
        self.assertNotIsInstance(fields["is_ai_system"], bool)

    def test_a_value_outside_the_vocabulary_is_invalid(self):
        result = store.classify({"ref": "P-0009", "act_tier": "medium"})
        self.assertEqual(result["fields"]["act_tier"]["state"], "invalid")
        self.assertIn("act_tier", result["invalid"])
        self.assertFalse(result["complete"])

    def test_a_captured_project_owes_no_classification_yet(self):
        """§3.1's principle from the classification side: fields become
        mandatory as a project advances, never at creation."""
        result = store.classify({"ref": "P-0009", "stage": "captured"})
        self.assertEqual(result["missing_and_owed"], [])
        self.assertTrue(result["unclassified"])
        self.assertFalse(result["fields"]["act_tier"]["owed_now"])

    def test_the_same_project_at_scoped_owes_three_of_the_four(self):
        result = store.classify({"ref": "P-0009", "stage": "scoped"})
        self.assertEqual(sorted(result["missing_and_owed"]),
                         ["act_tier", "is_ai_system", "my_role"])
        self.assertFalse(result["fields"]["third_party_output"]["owed_now"])

    def test_third_party_output_is_owed_from_validated(self):
        result = store.classify({"ref": "P-0009", "stage": "validated"})
        self.assertIn("third_party_output", result["missing_and_owed"])

    def test_which_stage_owes_what_is_read_from_the_one_table(self):
        """Not restated in the classification code. A second list is a second
        place to be wrong."""
        for field in store.CLASSIFICATION_FIELDS:
            stage = store.classification_owed_from(field)
            self.assertIn(field, store.STAGE_REQUIREMENTS[stage]["fields"])

    def test_check_calls_an_invalid_value_a_problem_not_an_advisory(self):
        """Advisory about debts is not advisory about the vocabulary -- the same
        rule an unknown stage has always had."""
        self.project("P-0009", stage="scoped", is_ai_system="maybe",
                     my_role="deployer", act_tier="minimal", directory="~/x")
        reg = self.register()
        reg.adopt("P-0009", "x", "project")
        result = store.check(store.load_store(self.root), reg)
        self.assertTrue(any("is_ai_system is 'maybe'" in line
                            for line in result["problems"]))
        self.assertTrue(any("yes | no | contested" in line
                            for line in result["problems"]))

    def test_check_calls_a_missing_classification_a_debt(self):
        self.project("P-0009", stage="scoped")
        reg = self.register()
        reg.adopt("P-0009", "x", "project")
        result = store.check(store.load_store(self.root), reg)
        self.assertEqual(result["problems"], [])
        self.assertTrue(any("owes 'act_tier'" in line for line in result["advisories"]))


class TestClassificationQueries(Temp):

    def setUp(self):
        super().setUp()
        self.project("P-0001", stage="production", is_ai_system="yes",
                     my_role="deployer", act_tier="high", third_party_output="client",
                     directory="~/a", output_location="~/a", value_estimate="3h",
                     context="business", type="code", value_basis="hours_saved",
                     next_review="2026-12-01")
        self.project("P-0002", stage="scoped", is_ai_system="contested",
                     my_role="provider", act_tier="transparency", directory="~/b")
        self.project("P-0003", stage="captured")
        self.project("P-0004", stage="validated", is_ai_system="no", my_role="n/a",
                     act_tier="minimal", directory="~/d")
        self.queries = governance.classification_queries(store.load_store(self.root))

    def test_missing_at_a_stage_that_owes_it(self):
        self.assertEqual(self.queries["missing_at_a_stage_that_owes_it"],
                         {"P-0004": ["third_party_output"]})

    def test_never_classified_is_a_different_answer_from_missing(self):
        """A captured shower thought owes nothing yet. Collapsing the two would
        be the flat thirty-field form §3.1 rejects."""
        self.assertEqual(self.queries["never_classified"], ["P-0003"])
        self.assertNotIn("P-0003", self.queries["missing_at_a_stage_that_owes_it"])

    def test_contested_gets_a_list_of_its_own(self):
        self.assertEqual(self.queries["contested"], ["P-0002"])
        self.assertEqual(self.queries["ai_systems"], ["P-0001"])

    def test_by_act_tier(self):
        self.assertEqual(self.queries["by_act_tier"]["high"], ["P-0001"])
        self.assertEqual(self.queries["by_act_tier"]["unclassified"], ["P-0003"])

    def test_output_reaching_a_third_party(self):
        self.assertEqual(self.queries["output_reaches_a_third_party"], ["P-0001"])

    def test_a_query_returns_refs_and_never_names(self):
        """A query result that carries names cannot be shown in presentation
        mode, which would make the two features contradict each other."""
        text = json.dumps(self.queries)
        for ref in ("P-0001", "P-0002", "P-0003", "P-0004"):
            self.assertNotIn(f"name-of-{ref}", text)


# ------------------------------------------------------- presentation mode

class TestPresentationMode(Temp):

    def setUp(self):
        super().setUp()
        self.project("P-0007", stage="production", name="nanowiki",
                     one_liner="Offline-first personal wiki with gist sync",
                     directory="~/Documents/Dev Projects/nanowiki",
                     output_location="~/Documents/Dev Projects/nanowiki-content",
                     github_repo="hyperhodge/nanowiki", is_ai_system="no",
                     my_role="n/a", act_tier="out_of_scope", third_party_output="none",
                     context="personal", type="code", value_basis="hours_saved",
                     value_estimate="3h/month", next_review="2026-12-01")
        store.write_record(self.root / "tasks" / "P-0007-T05.md",
                           {"ref": "P-0007-T05", "project_ref": "P-0007",
                            "state": "shipped",
                            "brief": "Separate nanowiki's code from its content",
                            "spec_path": "TASK-002-nanowiki-split.md"}, "")
        store.append_runs(self.root / "runs" / "2026-09.jsonl", [{
            "ref": "R-0001", "claude_session_id": "aaaa-bbbb", "cost_usd": 1.0,
            "started_at": "2026-09-01T10:00:00Z", "task_ref": "P-0007-T05",
            "transcript_path": "/Users/ada/Library/.../nanowiki-session.jsonl",
            "outputs": [], "commits": []}])
        reg = self.register()
        reg.adopt("P-0007", "nanowiki", "project")
        reg.adopt("P-0007-T05", "TASK-002-nanowiki-split.md", "task")
        reg.adopt("R-0001", "aaaa-bbbb", "run")
        reg.save()
        self.loaded = store.load_store(self.root)
        self.payload = store.export(self.loaded, self.register())
        self.presented = governance.present(self.payload)
        self.text = json.dumps(self.presented, sort_keys=True)

    def test_no_value_from_any_disclosive_field_survives(self):
        """The test the brief asks for by name. It asserts against the whole
        serialised payload rather than against the redactor's own list of what
        it touched, so a field the redactor forgot fails here."""
        audit = governance.audit_presentation(self.payload, self.presented)
        self.assertEqual(audit["leaked"], [])
        self.assertGreater(audit["checked"], 5)
        self.assertTrue(audit["clean"])

    def test_the_unredacted_export_really_does_contain_them(self):
        """Otherwise the test above passes for the wrong reason."""
        raw = json.dumps(self.payload, sort_keys=True)
        for secret in ("nanowiki", "hyperhodge/nanowiki", "Offline-first personal wiki"):
            self.assertIn(secret, raw)

    def test_the_project_name_becomes_an_opaque_label(self):
        self.assertEqual(self.presented["projects"]["P-0007"]["name"], "Project P-0007")
        self.assertNotIn("nanowiki", self.text)

    def test_a_free_text_brief_is_redacted_too(self):
        """The leak the audit found on its first real run: the name was hidden
        and the task brief said it anyway."""
        self.assertNotIn("Separate nanowiki", self.text)
        self.assertIn("brief", store.DISCLOSIVE_FIELDS)

    def test_the_allocation_register_is_redacted(self):
        """refs.json binds a project ref to its slug, so it holds every name a
        second time. A presentation mode that leaks through its own
        bookkeeping is not one."""
        bindings = self.presented["refs"]["bindings"]
        self.assertNotIn("nanowiki", bindings["P-0007"]["natural_key"])
        self.assertNotIn("nanowiki", bindings["P-0007-T05"]["natural_key"])

    def test_a_run_keeps_its_session_id(self):
        """A uuid names nothing about anyone, and redacting the ids would cost
        the presented export its audit trail for no gain. Stated in
        DISCLOSIVE_FIELDS and asserted here so the choice is deliberate."""
        self.assertEqual(self.presented["runs"][0]["claude_session_id"], "aaaa-bbbb")
        self.assertEqual(self.presented["refs"]["bindings"]["R-0001"]["natural_key"],
                         "aaaa-bbbb")

    def test_refs_carry_no_meaning_and_are_never_redacted(self):
        """§8.5's presentation mode is only possible because of this. P-0007
        says nothing about nanowiki, by design."""
        self.assertIn("P-0007", self.text)
        self.assertEqual(sorted(self.presented["projects"]), ["P-0007"])
        self.assertEqual(sorted(self.presented["tasks"]), ["P-0007-T05"])
        self.assertEqual(self.presented["runs"][0]["ref"], "R-0001")

    def test_the_governance_a_presentation_exists_to_show_survives(self):
        """A redaction that removed everything would be safe and useless."""
        fields = self.presented["projects"]["P-0007"]
        self.assertEqual(fields["stage"], "production")
        self.assertEqual(fields["act_tier"], "out_of_scope")
        self.assertEqual(fields["is_ai_system"], "no")
        self.assertEqual(fields["context"], "personal")
        self.assertEqual(self.presented["runs"][0]["cost_usd"], 1.0)

    def test_the_same_label_twice_is_the_same_label(self):
        """Stable, so two mentions of one project read as one project."""
        again = governance.present(store.export(self.loaded, self.register()))
        self.assertEqual(again["projects"]["P-0007"]["directory"],
                         self.presented["projects"]["P-0007"]["directory"])

    def test_it_says_it_is_a_presentation(self):
        self.assertTrue(self.presented["presentation_mode"])
        self.assertIn("§8.5", self.presented["basis"])
        self.assertEqual(self.presented["redacted_fields"],
                         sorted(store.DISCLOSIVE_FIELDS))

    def test_the_unredacted_export_is_not_mutated(self):
        self.assertEqual(self.payload["projects"]["P-0007"]["name"], "nanowiki")

    def test_an_override_reason_is_dropped_whole(self):
        """Free text written by a human under pressure. That a gate was
        overridden, and when, survives; what they said does not, and neither
        does their name (prd-core.md §6 rule 5)."""
        _, transition, _ = store.transition_project(
            {"ref": "P-0007", "stage": "captured"}, "production",
            override={"by": "ada", "reason": "the Acme demo is Monday"},
            now="2026-09-06T10:00:00Z")
        store.append_decisions([transition], self.root / "decisions")
        presented = governance.present(
            store.export(store.load_store(self.root), self.register()))
        record = presented["decisions"][0]
        self.assertNotIn("Acme", json.dumps(presented))
        self.assertEqual(record["override"]["by"], "[Approver]")
        self.assertTrue(record["overridden"])

    def test_every_field_naming_a_person_becomes_a_placeholder(self):
        """Decided by the owner, 13 September: his name never appears in shared data."""
        payload = {"projects": {}, "runs": [], "superseded_runs": [],
                   "tasks": {"P-0001-T01": {"ref": "P-0001-T01", "approved_by": "Wendeline",
                                            "handed_off_by": "Wendeline"}},
                   "decisions": [{"kind": "SessionStarted", "at": "2026-09-13T00:00:00Z",
                                  "override": {"by": "Wendeline", "reason": "x"}}]}
        presented = governance.present(payload)
        self.assertNotIn("Wendeline", json.dumps(presented))
        self.assertEqual(presented["tasks"]["P-0001-T01"]["approved_by"], "[Approver]")
        self.assertTrue(governance.audit_presentation(payload, presented)["clean"])

    def test_a_ref_or_session_in_a_by_field_is_not_a_person(self):
        payload = {"projects": {}, "runs": [], "superseded_runs": [], "decisions": [],
                   "tasks": {"P-0001-T01": {"ref": "P-0001-T01",
                                            "superseded_in_part_by": "P-0001-T14",
                                            "consumed_by": "session 25"}},
                   "refs": {"bindings": {"P-0001-T14": {"kind": "task", "natural_key": "TASK-099-a-brief.md",
                                                        "rename_basis": "split on Wendeline's say"}}}}
        presented = governance.present(payload)
        task = presented["tasks"]["P-0001-T01"]
        self.assertEqual((task["superseded_in_part_by"], task["consumed_by"]),
                         ("P-0001-T14", "session 25"))
        self.assertNotIn("Wendeline", json.dumps(presented))
        self.assertTrue(governance.audit_presentation(payload, presented)["clean"])

    def test_the_audit_catches_a_name_that_survives_elsewhere(self):
        payload = {"projects": {}, "runs": [], "superseded_runs": [], "decisions": [],
                   "tasks": {"P-0001-T01": {"ref": "P-0001-T01", "approved_by": "Wendeline",
                                            "task_type": "Wendeline's"}}}
        presented = governance.present(payload)
        self.assertEqual(governance.audit_presentation(payload, presented)["leaked"],
                         ["Wendeline"])


class TestTheGovernanceCLI(Temp):

    def run_cli(self, *argv):
        import io
        out = io.StringIO()
        code = governance.main(["--root", str(self.root), *argv], out=out)
        return code, out.getvalue()

    def setUp(self):
        super().setUp()
        self.project("P-0009", stage="scoped", name="secret-client-thing",
                     is_ai_system="contested", my_role="provider", directory="~/x")
        reg = self.register()
        reg.adopt("P-0009", "secret-client-thing", "project")
        reg.save()

    def test_the_default_view_names_what_is_owed(self):
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("is_ai_system=contested", text)
        self.assertIn("act_tier", text)

    def test_show_marks_missing_and_not_yet_owed_differently(self):
        code, text = self.run_cli("--show", "P-0009")
        self.assertEqual(code, 0)
        self.assertIn("owed from scoped", text)
        self.assertIn("not owed until validated", text)

    def test_audit_passes_on_a_real_store(self):
        code, text = self.run_cli("--audit")
        self.assertEqual(code, 0)
        self.assertIn("clean", text)

    def test_present_writes_a_redacted_file(self):
        """INVERTED IN SESSION 8, deliberately, the way an earlier session inverted two
        of an earlier session's. The assertion did not rot: writing an export now needs
        a recorded consent (§8.5, PLAN.md row 8), so the two-argument call this
        test used to make is refused. What it asserts about the CONTENT is
        unchanged and still the point."""
        target = self.root / "presented.json"
        code, text = self.run_cli("--present", "--out", str(target))
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", text)
        self.assertFalse(target.exists())

        code, _ = self.run_cli("--present", "--out", str(target),
                               "--consent-by", "ada",
                               "--consent-reason", "the test that used to be here")
        self.assertEqual(code, 0)
        self.assertNotIn("secret-client-thing", target.read_text())
        self.assertIn("P-0009", target.read_text())


# --------------------------------------------------------------------- row 8
# Export consent (§8.5). An earlier session. The serialiser shipped in an earlier session and the
# redactor in an earlier session; what row 8 owes is the DEMAND being recorded.
# --------------------------------------------------------------------------

class TestExportConsent(Temp):

    def payload(self):
        self.project("P-0001", name="secret-client-thing",
                     directory="/Users/someone/Clients/Acme")
        return store.export(store.load_store(self.root), self.register())

    def test_a_manifest_counts_the_disclosive_values_by_field(self):
        manifest = store.export_manifest(self.payload())
        self.assertGreaterEqual(manifest["disclosive_values"]["name"], 1)
        self.assertGreaterEqual(manifest["disclosive_values"]["directory"], 1)
        self.assertFalse(manifest["presentation_mode"])
        self.assertTrue(manifest["warnings"])

    def test_a_manifest_counts_the_natural_keys_in_the_allocation_register(self):
        """The leak an earlier session's audit found. A manifest that counted the record
        and not the register would understate what the export carries."""
        payload = self.payload()
        payload["refs"] = {"bindings": {
            "P-0001": {"kind": "project", "natural_key": "acme-thing"},
            "R-0001": {"kind": "run", "natural_key": "5b2f-a-uuid"}}}
        inventory = store.disclosive_inventory(payload)
        self.assertEqual(inventory["natural_key"], 1)      # the run's is not one

    def test_a_presented_manifest_still_counts_what_was_there_before_redaction(self):
        """A presented payload counts zero by construction, and 'zero disclosive
        values' would read as 'there was nothing to hide'."""
        payload = self.payload()
        presented = governance.present(payload)
        manifest = store.export_manifest(
            presented, presentation_mode=True, unredacted=payload,
            audit=governance.audit_presentation(payload, presented))
        self.assertGreater(manifest["disclosive_values_total"], 0)
        self.assertTrue(manifest["redacted_fields"])
        self.assertEqual(manifest["warnings"], [])

    def test_a_failed_audit_becomes_a_warning_on_the_manifest(self):
        manifest = store.export_manifest(
            self.payload(), presentation_mode=True,
            audit={"clean": False, "leaked": ["secret-client-thing"], "checked": 1})
        self.assertTrue(any("FAILED ITS OWN AUDIT" in w for w in manifest["warnings"]))

    def test_consent_needs_a_name_and_a_reason(self):
        manifest = store.export_manifest(self.payload())
        with self.assertRaises(store.ConsentNotRecorded):
            store.consent_record(manifest, by=None, reason="because")
        with self.assertRaises(store.ConsentNotRecorded):
            store.consent_record(manifest, by="ada", reason="   ")

    def test_consent_is_appended_to_the_append_only_decisions_log(self):
        manifest = store.export_manifest(self.payload(), destination="/tmp/x.json")
        record = store.record_export_consent(
            manifest, "ada", "sending to a client", root=self.root)
        self.assertEqual(record["kind"], "ExportConsent")
        loaded = store.load_store(self.root)
        self.assertEqual(len(store.export_consents(loaded)), 1)
        self.assertEqual(store.export_consents(loaded)[0]["by"], "ada")

    def test_a_consent_records_how_much_shipped_in_the_clear(self):
        manifest = store.export_manifest(self.payload())
        record = store.consent_record(manifest, "ada", "why")
        self.assertGreater(record["disclosive_values_in_the_clear"], 0)
        presented = governance.present(self.payload())
        clean = store.export_manifest(presented, presentation_mode=True,
                                      unredacted=self.payload())
        self.assertEqual(
            store.consent_record(clean, "ada", "why")["disclosive_values_in_the_clear"],
            0)

    def test_nothing_is_written_when_consent_is_refused(self):
        manifest = store.export_manifest(self.payload())
        with self.assertRaises(store.ConsentNotRecorded):
            store.record_export_consent(manifest, "ada", None, root=self.root)
        self.assertEqual(list((self.root / "decisions").glob("*.jsonl")), [])

    def test_an_export_consent_is_redacted_in_presentation_mode(self):
        """The log lives inside the export it records, so a destination path and
        a free-text reason would leak through the audit trail -- the same shape
        as the refs.json leak an earlier session found."""
        manifest = store.export_manifest(
            self.payload(), destination="/Users/someone/Clients/Acme/export.json")
        store.record_export_consent(manifest, "ada",
                                    "for the Acme review meeting", root=self.root)
        payload = store.export(store.load_store(self.root), self.register())
        presented = governance.present(payload)
        audit = governance.audit_presentation(payload, presented)
        self.assertTrue(audit["clean"], audit["leaked"])
        consent = [r for r in presented["decisions"] if r["kind"] == "ExportConsent"][0]
        self.assertEqual(consent["by"], "[Approver]")     # that someone consented survives
        self.assertNotIn("Acme", json.dumps(consent))     # what they said does not


class TestTheExportCLIGate(Temp):

    def run_cli(self, *argv):
        import io
        out = io.StringIO()
        code = store.main(["--root", str(self.root), *argv], out=out)
        return code, out.getvalue()

    def setUp(self):
        super().setUp()
        self.project("P-0001", name="secret-client-thing")

    def test_stdout_is_not_gated_because_nothing_is_written(self):
        code, text = self.run_cli("--export")
        self.assertEqual(code, 0)
        self.assertIn("export manifest", text)
        self.assertIn("WARNING", text)                    # in the clear, and it says so

    def test_writing_a_file_without_a_name_is_refused(self):
        target = self.root / "export.json"
        code, text = self.run_cli("--export", "--out", str(target))
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", text)
        self.assertFalse(target.exists())

    def test_writing_a_file_with_consent_records_it(self):
        target = self.root / "export.json"
        code, text = self.run_cli("--export", "--out", str(target),
                                  "--consent-by", "ada",
                                  "--consent-reason", "a client asked")
        self.assertEqual(code, 0)
        self.assertTrue(target.exists())
        code, log = self.run_cli("--consent-log")
        self.assertIn("ada", log)
        self.assertIn("a client asked", log)
        self.assertIn("IN THE CLEAR", log)
