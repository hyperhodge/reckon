#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The names check -- spec-core.md Item 6, §6.3.

Usage: python3 -m unittest discover -s store/tests

**Every name here is made up.** The real list lives outside the repository and
nothing in this file reads it: each test writes its own list to a temp dir,
locks it, and points the check at it.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "store"))

import names_check      # noqa: E402

MADE_UP = """# made-up names for tests
Marrow & Finch | MFX
Quillbrook Analytics | Quillbrook
#? Obsidian Larch
Zent
"""


class Temp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.base = Path(self.dir.name)
        self.root = self.base / "register"
        (self.root / "decisions").mkdir(parents=True)
        self.private = self.base / "never-publish"
        self.private.mkdir(mode=0o700)
        self.list = self.private / "names.txt"
        self.write_list(MADE_UP)
        self.package = self.base / "package"
        self.package.mkdir()

    def tearDown(self):
        self.dir.cleanup()

    def write_list(self, text, mode=0o600):
        self.list.write_text(text, encoding="utf-8")
        os.chmod(self.list, mode)

    def file(self, name, text):
        path = self.package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_main(self, *argv):
        out = io.StringIO()
        code = names_check.main([*argv, "--list", str(self.list), "--root", str(self.root)],
                                out=out)
        return code, out.getvalue()

    def hits(self, text, words=frozenset(), name=False):
        entries = names_check.load_list(self.list, self.root)
        return names_check.Matcher(entries, words).scan_line(text, name=name)


class Expansion(Temp):
    def test_every_form_is_generated_for_a_multi_word_entry_with_an_ampersand(self):
        forms = names_check.expand("Marrow & Finch")
        self.assertEqual(set(forms.values()), set(names_check.KINDS))
        for form in ("marrow & finch", "marrow and finch", "marrow finch",
                     "marrowfinch", "marrowandfinch", "marrow&finch",
                     "marrow-finch", "marrow-and-finch", "marrow_finch",
                     "marrow & finch's", "www.marrowfinch.com",
                     "marrowandfinch.co.uk", "@marrowfinch.com"):
            self.assertIn(form, forms)

    def test_candidate_lines_are_not_active(self):
        self.assertEqual(self.hits("the obsidian larch report"), [])


class Matching(Temp):
    def test_lower_case_hyphenated_and_web_address_forms_are_caught(self):
        for text in ("notes about marrow and finch today",
                     "folder MARROW-FINCH/brief",
                     "see https://www.marrowandfinch.co.uk/about",
                     "mail jo@marrowfinch.com",
                     "quillbrook_analytics_v2"):
            with self.subTest(text=text):
                self.assertTrue(self.hits(text), text)

    def test_one_letter_misspelling_of_a_long_entry_is_caught(self):
        hits = self.hits("the Quilbrook engagement")
        self.assertEqual([hit[2] for hit in hits], [names_check.NEAR_MISS])
        self.assertTrue(self.hits("quillbrok"))
        self.assertTrue(self.hits("quill-brook"))

    def test_one_letter_misspelling_of_a_four_letter_entry_is_not(self):
        self.assertTrue(self.hits("Zent"))
        self.assertEqual(self.hits("Zant"), [])
        self.assertEqual(self.hits("Zents"), [])

    def test_a_name_inside_a_longer_word_is_not_a_match(self):
        self.assertEqual(self.hits("presentment"), [])
        self.assertEqual(self.hits("MFXING and remfx"), [])


class ShortNamesAndOrdinaryWords(Temp):
    """The owner, 13 September: a two-letter name must not match inside a longer
    word, and must not flood the report. Every name here is made up."""

    def setUp(self):
        super().setUp()
        self.write_list("MFX\nGlimmerton\nSpendora\nHarbour | Quillbrook\n")
        self.words = frozenset({"harbour", "glimmertop", "spend", "orb", "the", "a"})

    def test_a_short_name_matches_as_written_as_a_whole_word(self):
        self.assertTrue(self.hits("worked with MFX on pricing"))
        self.assertTrue(self.hits("MFX's rollout"))
        self.assertEqual(self.hits("MFXTRA"), [])

    def test_a_short_name_in_lower_case_or_joined_by_an_underscore_is_not(self):
        for text in ("mfx = model['x']", "rate_mfx_total", "the Mfx value"):
            with self.subTest(text=text):
                self.assertEqual(self.hits(text, self.words), [])

    def test_a_short_name_in_lower_case_in_a_path_or_a_name_is(self):
        self.assertTrue(self.hits("see clients/mfx/notes.md", self.words))
        self.assertEqual(self.hits("mfx-width: 2px; Math.mfx(a)", self.words), [])
        self.assertTrue(self.hits("mfx", self.words, name=True))
        self.assertTrue(self.hits("https://www.mfx.com/", self.words))

    def test_an_ordinary_word_on_the_list_matches_only_as_written(self):
        self.assertTrue(self.hits("the Harbour account", self.words))
        self.assertEqual(self.hits("sheltered in the harbour", self.words), [])

    def test_a_near_miss_that_is_an_ordinary_word_is_not_caught(self):
        self.assertEqual(self.hits("glimmertop", self.words), [])
        self.assertEqual(self.hits("glimmertops", self.words), [])
        self.assertTrue(self.hits("Glimmertin", self.words))

    def test_a_near_miss_never_glues_words_a_space_separates(self):
        self.assertEqual(self.hits("spend orb", self.words), [])
        self.assertTrue(self.hits("spend ora", self.words))      # same letters
        self.assertTrue(self.hits("spendorx", self.words))       # one word, one edit

    def test_the_control_passes_with_strict_names_and_a_dictionary(self):
        entries = names_check.load_list(self.list, self.root)
        control = names_check.positive_control(
            entries, names_check.Matcher(entries, self.words))
        self.assertEqual(control["caught"], control["planted"])


class Refusals(Temp):
    def test_no_list_file_is_a_refusal_never_a_pass(self):
        self.list.unlink()
        self.file("a.txt", "nothing here")
        code, output = self.run_main(str(self.package))
        self.assertEqual(code, names_check.EXIT_REFUSED)
        self.assertIn("REFUSED", output)
        self.assertNotIn("PASS", output)

    def test_a_list_other_accounts_can_read_is_refused(self):
        self.write_list(MADE_UP, mode=0o644)
        code, output = self.run_main(str(self.package))
        self.assertEqual(code, names_check.EXIT_REFUSED)
        self.assertIn("other accounts", output)

    def test_a_list_inside_the_repository_is_refused(self):
        inside = self.root / "names.txt"
        inside.write_text(MADE_UP)
        os.chmod(inside, 0o600)
        with self.assertRaises(names_check.Refused):
            names_check.load_list(inside, self.root)

    def test_the_control_refuses_to_run_when_one_form_is_broken(self):
        real = names_check.matcher_forms

        def without_hyphens(entries, words=frozenset()):
            return {form: sources for form, sources in real(entries, words).items()
                    if not any(kind == "hyphenated" for _, _, kind in sources)}

        self.file("clean.txt", "nothing to see")
        names_check.matcher_forms = without_hyphens
        try:
            code, output = self.run_main(str(self.package))
        finally:
            names_check.matcher_forms = real
        self.assertEqual(code, names_check.EXIT_REFUSED)
        self.assertIn("positive control missed", output)
        self.assertIn("hyphenated", output)
        self.assertNotIn("RESULT       PASS", output)


class Report(Temp):
    def test_a_clean_package_passes_and_shows_the_control(self):
        self.file("README.md", "A register of work.\n")
        code, output = self.run_main(str(self.package))
        self.assertEqual(code, names_check.EXIT_PASS)
        self.assertIn("control      PASSED", output)
        self.assertIn("RESULT       PASS", output)

    def test_the_list_never_appears_in_the_report_beyond_the_matched_entry(self):
        self.file("notes.txt", "line one\nwe met Quillbrook on Tuesday\n")
        code, output = self.run_main(str(self.package), "--report",
                                     str(self.base / "report.txt"))
        self.assertEqual(code, names_check.EXIT_MATCH)
        lowered = output.lower()
        self.assertIn("'quillbrook'", lowered)
        self.assertIn("notes.txt:2", output)
        for other in ("marrow", "finch", "mfx", "analytics", "obsidian", "larch", "zent"):
            self.assertNotIn(other, lowered)
        self.assertEqual((self.base / "report.txt").stat().st_mode & 0o777, 0o600)

    def test_file_and_folder_names_and_json_strings_are_scanned(self):
        # The escaped names are invisible to a line scan: only the decoded
        # string values carry them.
        self.file("marrow-finch/readme.txt", "clean")
        self.file("data.json", json.dumps({"note": "Quillbrook"}).replace("Q", "\\u0051"))
        self.file("rows.jsonl", '{"a": "\\u005aent report"}\n')
        code, output = self.run_main(str(self.package))
        self.assertEqual(code, names_check.EXIT_MATCH)
        self.assertIn("marrow-finch (file or folder name)", output)
        self.assertIn("data.json (a string value)", output)
        self.assertIn("rows.jsonl:1 (a string value)", output)

    def test_the_capital_letter_layer_lists_candidates_without_failing(self):
        self.file("a.txt", "Worked with Pemberly on the rollout.\n")
        code, output = self.run_main(str(self.package))
        self.assertEqual(code, names_check.EXIT_PASS)
        self.assertIn("Pemberly", output)


class Clearance(Temp):
    def fingerprint_of(self, output):
        return output.split("fingerprint ")[1].split()[0]

    def test_a_false_alarm_cleared_by_name_no_longer_stops_the_package(self):
        self.file("a.txt", "Zent\n")
        code, output = self.run_main(str(self.package))
        self.assertEqual(code, names_check.EXIT_MATCH)
        fp = self.fingerprint_of(output)
        code, _ = self.run_main("--clear", fp, "--by", "Tester",
                                "--reason", "a German word in a quoted sentence")
        self.assertEqual(code, names_check.EXIT_PASS)
        record = json.loads((next((self.root / "decisions").glob("*.jsonl"))
                             .read_text().splitlines()[-1]))
        self.assertEqual(record["kind"], names_check.CLEARANCE_KIND)
        self.assertNotIn("zent", json.dumps(record).lower())
        code, output = self.run_main(str(self.package))
        self.assertEqual(code, names_check.EXIT_PASS)
        self.assertIn("cleared      1", output)

    def test_a_reason_that_names_the_entry_is_refused(self):
        code, output = self.run_main("--clear", "0123456789abcdef", "--by", "Tester",
                                     "--reason", "it is only Quillbrook")
        self.assertEqual(code, names_check.EXIT_REFUSED)
        self.assertEqual(list((self.root / "decisions").glob("*.jsonl")), [])

    def test_clearing_needs_a_name(self):
        code, _ = self.run_main("--clear", "0123456789abcdef", "--reason", "fine")
        self.assertEqual(code, names_check.EXIT_REFUSED)


if __name__ == "__main__":
    unittest.main()
