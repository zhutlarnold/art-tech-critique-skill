"""Behavior checks for offline register decisions and safe persistence."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "art-tech-critique" / "scripts" / "registry.py"
SPEC = importlib.util.spec_from_file_location("research_registry", SCRIPT)
registry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registry)


def case(case_id="wooden", **overrides):
    value = {"id": case_id, "title": "Wooden Mirror", "kind": "artwork",
             "artist": "Daniel Rozin", "year": 1999,
             "aliases": ["木头镜子", "木镜"], "stage": "delivered",
             "publication": {"status": "unknown", "evidence": []},
             "question": "材料如何让观众看见数字图像？",
             "takeaway": "木块将数字图像变成物质表面"}
    value.update(overrides)
    return value


def fixture():
    value = registry.empty_registry()
    value["cases"] = [case()]
    value["sources"] = [{"id": "artist-page", "url": "https://example.org/Art?item=1#details",
                         "source_type": "artist_official", "evidence_family_id": "artist-description"}]
    value["claims"] = [{"id": "fact-one", "case_id": "wooden", "type": "fact",
                        "text": "The work uses wooden elements.", "sources": ["artist-page"],
                        "premises": [], "publishable": True}]
    value["images"] = [{"case_id": "wooden", "source_url": "https://example.org/Photo.jpg",
                        "sha256": "a" * 64, "role": "overview"}]
    return value


def categories(result):
    return [item["category"] for item in result["risks"]]


class IdentityAndSources(unittest.TestCase):
    def test_known_cross_language_alias_is_existing_identity(self):
        result = registry.assess_candidate(fixture(), case("candidate", title="木头镜子", aliases=[]))
        matches = [item for item in result["risks"] if item["category"] == "identity_match"]
        self.assertEqual(matches[0]["case_ids"], ["wooden"])

    def test_different_and_unknown_years_need_version_review(self):
        for year in (2014, None):
            with self.subTest(year=year):
                result = registry.assess_candidate(fixture(), case("candidate", year=year))
                self.assertIn("version_review", categories(result))
                self.assertNotIn("identity_match", categories(result))

    def test_same_title_different_artist_does_not_merge(self):
        result = registry.assess_candidate(fixture(), case("candidate", artist="Another Artist"))
        self.assertNotIn("identity_match", categories(result))
        self.assertNotIn("version_review", categories(result))

    def test_abbreviated_artist_name_is_not_guessed(self):
        result = registry.assess_candidate(fixture(), case("candidate", artist="D. Rozin"))
        self.assertNotIn("identity_match", categories(result))
        self.assertNotIn("version_review", categories(result))

    def test_unknown_artist_is_uncertain_and_does_not_block_distinct_add(self):
        candidate = case("unknown-work", artist=None)
        result = registry.assess_candidate(fixture(), candidate)
        self.assertIn("identity_uncertain", categories(result))
        self.assertNotIn("identity_match", categories(result))
        merged, _ = registry.merge_registry(fixture(), {"cases": [candidate]}, "add")
        self.assertEqual(len(merged["cases"]), 2)

    def test_generic_untitled_never_auto_merges(self):
        value = fixture()
        value["cases"] = [case(title="Untitled", aliases=["无题"])]
        result = registry.assess_candidate(value, case("different-object", title="无题", aliases=[]))
        self.assertIn("identity_uncertain", categories(result))
        self.assertNotIn("identity_match", categories(result))
        self.assertNotIn("version_review", categories(result))

    def test_unicode_typography_and_whitespace_normalize_without_translation(self):
        self.assertEqual(registry.normalize_text("  ＤＡＮＩＥＬ  Rozin  "), "daniel rozin")
        self.assertNotEqual(registry.normalize_text("罗津"), registry.normalize_text("Rozin"))

    def test_url_only_removes_tracking_preserving_other_information(self):
        url = "https://EXAMPLE.org:8443/Art/Photo.jpg?item=2&u=%2F&blank=&utm_source=x&fbclid=y#Zoom"
        self.assertEqual(registry.normalize_url(url),
                         "https://example.org:8443/Art/Photo.jpg?item=2&u=%2F&blank=#Zoom")
        for other in ("https://example.org:8443/art/Photo.jpg?item=2&u=%2F&blank=#Zoom",
                      "https://example.org/Art/Photo.jpg?item=2&u=%2F&blank=#Zoom",
                      "https://example.org:8443/Art/Photo.jpg?item=3&u=%2F&blank=#Zoom",
                      "https://example.org:8443/Art/Photo.jpg?item=2&u=%2F&blank=#Other"):
            self.assertNotEqual(registry.normalize_url(url), registry.normalize_url(other))

    def test_source_and_known_family_reuse_are_flags(self):
        source = {"id": "repost", "url": "https://example.org/Art?item=1&utm_campaign=z#details",
                  "source_type": "report", "evidence_family_id": "artist-description"}
        result = registry.assess_candidate(fixture(), case("another", artist="Other"), [source])
        self.assertIn("source_url_reuse", categories(result))
        self.assertIn("evidence_family_overlap", categories(result))

    def test_shared_domain_or_institution_is_not_automatically_same_family(self):
        source = {"id": "independent-report", "url": "https://example.org/Other",
                  "source_type": "report", "institution": "Same Institution"}
        result = registry.assess_candidate(fixture(), case("another", artist="Other"), [source])
        self.assertNotIn("source_url_reuse", categories(result))
        self.assertNotIn("evidence_family_overlap", categories(result))

    def test_image_hash_and_url_reuse_are_detected(self):
        images = [{"source_url": "https://other.org/different-file.jpg", "sha256": "A" * 64},
                  {"source_url": "https://example.org/Photo.jpg?utm_source=x", "sha256": "b" * 64}]
        result = registry.assess_candidate(fixture(), case("another", artist="Other"), images=images)
        matches = [item["match"] for item in result["risks"] if item["category"] == "image_reuse"]
        self.assertEqual(matches, ["sha256", "source_url"])

    def test_keyword_overlap_is_warning_never_semantic_approval(self):
        result = registry.assess_candidate(fixture(), case("another", artist="Other"))
        self.assertIn("argument_overlap_warning", categories(result))
        self.assertIs(result["requires_human_review"], True)
        new = registry.assess_candidate(fixture(), case("another", title="Other", aliases=[],
                                                      artist="Other", question="robots", takeaway="sensors"))
        self.assertIs(new["requires_human_review"], True)


class EvidenceValidation(unittest.TestCase):
    def test_known_valid_register_passes(self):
        self.assertEqual(registry.validate_registry(fixture())["errors"], [])

    def test_enum_wrong_types_return_validation_errors(self):
        value = fixture()
        value["cases"][0].update(kind={}, stage=[], publication={"status": {}, "evidence": []})
        value["claims"][0]["type"] = {}
        self.assertGreaterEqual(len(registry.validate_registry(value)["errors"]), 4)

    def test_cyclic_premises_are_rejected(self):
        value = fixture()
        value["claims"] = [
            {"id": "a", "case_id": "wooden", "type": "interpretation", "text": "A",
             "sources": [], "premises": ["b"], "publishable": True},
            {"id": "b", "case_id": "wooden", "type": "interpretation", "text": "B",
             "sources": [], "premises": ["a"], "publishable": True}]
        self.assertTrue(any("cycle" in error for error in registry.validate_registry(value)["errors"]))

    def test_published_requires_actual_evidence_and_confirmed_status(self):
        for publication in ({"status": "published_confirmed", "evidence": []},
                            {"status": "unknown", "evidence": ["Draft was delivered"]}):
            with self.subTest(publication=publication):
                value = fixture()
                value["cases"][0].update(stage="published", publication=publication)
                self.assertTrue(registry.validate_registry(value)["errors"])

    def test_nonempty_publication_evidence_is_accepted(self):
        value = fixture()
        value["cases"][0].update(stage="published", publication={
            "status": "published_confirmed", "evidence": ["User confirmed posting on 2026-10-05; link is retained in the source log."]})
        self.assertFalse(registry.validate_registry(value)["errors"])

    def test_unverified_claim_cannot_be_publishable(self):
        value = fixture()
        value["claims"][0].update(type="unverified", sources=[], publishable=True)
        self.assertTrue(any("unverified claim" in error for error in registry.validate_registry(value)["errors"]))

    def test_fact_and_intent_need_real_source_references(self):
        for claim_type, sources in (("fact", []), ("artist_intent", ["missing"])):
            with self.subTest(claim_type=claim_type):
                value = fixture()
                value["claims"][0].update(type=claim_type, sources=sources)
                self.assertTrue(registry.validate_registry(value)["errors"])

    def test_interpretation_needs_grounded_premises(self):
        value = fixture()
        value["claims"].append({"id": "reading", "case_id": "wooden", "type": "interpretation",
                                "text": "Our material reading.", "sources": [],
                                "premises": ["fact-one"], "publishable": True})
        self.assertEqual(registry.validate_registry(value)["errors"], [])
        value["claims"][1]["premises"] = []
        self.assertTrue(registry.validate_registry(value)["errors"])

    def test_transitive_unverified_premise_blocks_publication(self):
        value = fixture()
        value["claims"] += [
            {"id": "guess", "case_id": "wooden", "type": "unverified", "text": "Guess",
             "sources": [], "premises": [], "publishable": False},
            {"id": "bridge", "case_id": "wooden", "type": "interpretation", "text": "Bridge",
             "sources": [], "premises": ["guess", "fact-one"], "publishable": False},
            {"id": "reading", "case_id": "wooden", "type": "interpretation", "text": "Conclusion",
             "sources": [], "premises": ["bridge"], "publishable": True}]
        errors = registry.validate_registry(value)["errors"]
        self.assertTrue(any("unverified premise" in error for error in errors))

    def test_unknown_claim_case_and_image_references_rejected(self):
        value = fixture()
        value["claims"][0]["case_id"] = "missing"
        value["images"][0]["case_id"] = "missing"
        errors = registry.validate_registry(value)["errors"]
        self.assertEqual(sum("case_id" in error for error in errors), 2)


class MergeAndPersistence(unittest.TestCase):
    def setUp(self):
        self.run_root = Path(__file__).resolve().parent / ".test-runs"
        self.run_root.mkdir(exist_ok=True)
        self.run_directory = self.run_root / ("run-" + uuid.uuid4().hex)
        self.run_directory.mkdir()
        self.addCleanup(self.cleanup_temp)
        self.path = self.run_directory / "registry.json"
        self.entry_path = self.run_directory / "entry.json"
        self.path.write_text(json.dumps(fixture()), encoding="utf-8")

    def cleanup_temp(self):
        # Verify the absolute deletion target stays inside the tests workspace.
        self.run_directory.resolve().relative_to(self.run_root.resolve())
        shutil.rmtree(self.run_directory)

    def write_entry(self, value):
        self.entry_path.write_text(json.dumps(value), encoding="utf-8")

    def test_add_identity_and_version_are_blocked_without_forcing(self):
        for year in (1999, 2014, None):
            with self.subTest(year=year):
                self.write_entry({"cases": [case("new-id", year=year)]})
                before = self.path.read_bytes()
                with self.assertRaises(registry.RegistryError):
                    registry.merge_file(self.path, self.entry_path, "add")
                self.assertEqual(self.path.read_bytes(), before)

    def test_existing_id_can_be_explicitly_updated_to_record_versions(self):
        value = fixture()
        value["review_context"] = {"author": "club", "keep": True}
        value["cases"][0]["adoption"] = {"keep": "original", "status": "selected"}
        value["cases"][0]["publication"]["trace"] = "retain this"
        merged, _ = registry.merge_registry(value, {"cases": [{"id": "wooden", "versions": [1999, 2014],
                                                              "adoption": {"status": "reviewed"}}]}, "update")
        self.assertEqual(merged["review_context"], value["review_context"])
        self.assertEqual(merged["cases"][0]["adoption"], {"keep": "original", "status": "reviewed"})
        self.assertEqual(merged["cases"][0]["publication"]["trace"], "retain this")
        self.assertEqual(merged["cases"][0]["versions"], [1999, 2014])
        self.assertNotIn("versions", value["cases"][0])

    def test_duplicate_source_url_requires_reuse_of_existing_id(self):
        source = {"id": "duplicate", "url": "https://example.org/Art?item=1&utm_source=x#details",
                  "source_type": "report"}
        with self.assertRaisesRegex(registry.RegistryError, "reuse"):
            registry.merge_registry(fixture(), {"sources": [source]}, "add")

    def test_source_url_update_cannot_collide_with_another_source(self):
        value = fixture()
        value["sources"].append({"id": "other", "url": "https://example.org/Other", "source_type": "report"})
        with self.assertRaisesRegex(registry.RegistryError, "reuse"):
            registry.merge_registry(value, {"sources": [{"id": "other", "url": value["sources"][0]["url"]}]}, "update")

    def test_invalid_merge_does_not_modify_original_or_create_backup(self):
        self.write_entry({"claims": [{"id": "fact-one", "publishable": True, "type": "unverified"}]})
        before = self.path.read_bytes()
        with self.assertRaises(registry.RegistryError):
            registry.merge_file(self.path, self.entry_path, "update")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.backup-*.json")), [])
        self.assertFalse(self.path.with_name("registry.json.lock").exists())

    def test_successful_merge_keeps_exact_unique_backup_and_is_idempotent(self):
        self.write_entry({"cases": [{"id": "wooden", "reviewed": True}]})
        before = self.path.read_bytes()
        result = registry.merge_file(self.path, self.entry_path, "update")
        self.assertTrue(result["changed"])
        self.assertEqual(Path(result["backup"]).read_bytes(), before)
        self.assertTrue(json.loads(self.path.read_text(encoding="utf-8"))["cases"][0]["reviewed"])
        again = registry.merge_file(self.path, self.entry_path, "update")
        self.assertEqual(again["backup"], None)
        self.assertFalse(again["changed"])
        self.assertEqual(len(list(self.path.parent.glob("*.backup-*.json"))), 1)

    def test_failed_atomic_rename_keeps_original_valid_file(self):
        self.write_entry({"cases": [{"id": "wooden", "reviewed": True}]})
        before = self.path.read_bytes()
        with patch.object(registry.os, "replace", side_effect=OSError("simulated rename failure")):
            with self.assertRaises(OSError):
                registry.merge_file(self.path, self.entry_path, "update")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_external_file_change_aborts_before_replacement(self):
        before = self.path.read_bytes()
        expected = hashlib.sha256(before).hexdigest()
        changed = fixture()
        changed["external_edit"] = True
        self.path.write_text(json.dumps(changed), encoding="utf-8")
        modified = self.path.read_bytes()
        with self.assertRaisesRegex(registry.RegistryError, "changed"):
            registry.atomic_replace(self.path, fixture(), expected)
        self.assertEqual(self.path.read_bytes(), modified)

    def test_live_writer_lock_is_not_removed_or_bypassed(self):
        lock = self.path.with_name("registry.json.lock")
        lock.write_text("another writer", encoding="utf-8")
        self.write_entry({"cases": [{"id": "wooden", "reviewed": True}]})
        with self.assertRaisesRegex(registry.RegistryError, "lock"):
            registry.merge_file(self.path, self.entry_path, "update")
        self.assertEqual(lock.read_text(), "another writer")

    def test_init_never_overwrites_existing_register(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(registry.RegistryError, "overwrite"):
            registry.init_file(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        output = self.path.parent / "new-register.json"
        registry.init_file(output, seed=self.path)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), fixture())

    def test_cli_assess_and_check_are_read_only_in_moved_folder(self):
        moved = self.path.parent / "renamed skill scripts"
        moved.mkdir()
        script = moved / "registry.py"
        script.write_bytes(SCRIPT.read_bytes())
        candidate_path = self.path.parent / "candidate.json"
        candidate_path.write_text(json.dumps(case("candidate", title="木头镜子", aliases=[])), encoding="utf-8")
        before = self.path.read_bytes()
        environment = dict(os.environ, PYTHONUTF8="1")
        for arguments in (["check", "--registry", str(self.path)],
                          ["assess", "--registry", str(self.path), "--candidate", str(candidate_path)]):
            completed = subprocess.run([sys.executable, str(script), *arguments],
                                       capture_output=True, text=True, encoding="utf-8", env=environment)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertTrue(json.loads(completed.stdout)["ok"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.path.with_name("registry.json.lock").exists())

    def test_cli_candidate_needs_complete_base_fields(self):
        candidate_path = self.path.parent / "candidate.json"
        candidate_path.write_text('{"title":"A title"}', encoding="utf-8")
        completed = subprocess.run([sys.executable, str(SCRIPT), "assess", "--registry", str(self.path),
                                    "--candidate", str(candidate_path)],
                                   capture_output=True, text=True, encoding="utf-8",
                                   env=dict(os.environ, PYTHONUTF8="1"))
        self.assertEqual(completed.returncode, 2)
        self.assertIn("candidate.id", completed.stdout)

    def test_assess_optional_claims_receive_evidence_validation(self):
        value = {"case": case("candidate", artist="Another Artist"),
                 "claims": [{"id": "new-unverified", "case_id": "candidate", "type": "unverified",
                             "text": "A guess", "sources": [], "premises": [], "publishable": True}]}
        with self.assertRaisesRegex(registry.RegistryError, "unverified"):
            registry._assess_input(fixture(), value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
