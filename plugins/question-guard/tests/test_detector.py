#!/usr/bin/env python3
"""Detector + composition tests for question-guard's core module.

These exercise ``_remind`` in-process (no subprocess): sentence detection under
the four question tests, sanitisation of pasted/quoted material, directive
detection for variant selection, and reminder composition (quoting, truncation,
count reporting, the length clamp).

stdlib only; python3 >= 3.14.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import _remind  # noqa: E402


class DetectQuestionsTests(unittest.TestCase):
    """The four OR-combined question tests, sanitisation, and de-duplication."""

    def test_question_mark_terminated(self):
        self.assertEqual(_remind.detect_questions("Is the build green?"),
                         ["Is the build green?"])

    def test_wh_lead_without_question_mark(self):
        self.assertEqual(len(_remind.detect_questions("why is the cache cold")), 1)

    def test_aux_pronoun_with_question_mark(self):
        self.assertEqual(len(_remind.detect_questions("can you clean this up?")), 1)

    def test_aux_pronoun_without_question_mark(self):
        self.assertEqual(len(_remind.detect_questions("do we have tests")), 1)

    def test_imperative_aux_lead_is_not_a_question(self):
        self.assertEqual(_remind.detect_questions("do the refactor"), [])

    def test_plain_imperative_is_not_a_question(self):
        self.assertEqual(_remind.detect_questions("fix the bug then run tests"), [])

    def test_tag_question_without_question_mark(self):
        self.assertEqual(len(_remind.detect_questions("we ship today, right")), 1)

    def test_wh_single_word_is_not_a_question(self):
        self.assertEqual(_remind.detect_questions("Why."), [])

    def test_question_mark_in_fenced_code_does_not_fire(self):
        self.assertEqual(_remind.detect_questions("```\nis this real?\n```"), [])

    def test_question_in_inline_code_ternary_does_not_fire(self):
        self.assertEqual(_remind.detect_questions("use `cond ? a : b` here."), [])

    def test_question_mark_in_url_does_not_fire(self):
        self.assertEqual(
            _remind.detect_questions("see https://example.com/a?b=1 now."), []
        )

    def test_blockquote_line_is_stripped(self):
        self.assertEqual(_remind.detect_questions("> is this quoted?"), [])

    def test_overlapping_tests_yield_one_entry(self):
        # '?'-terminated AND WH-led -> still exactly one entry for the sentence.
        self.assertEqual(_remind.detect_questions("why is it slow?"),
                         ["why is it slow?"])

    def test_two_distinct_questions_counted_separately(self):
        got = _remind.detect_questions("Is it done? Should we merge this?")
        self.assertEqual(got, ["Is it done?", "Should we merge this?"])

    def test_empty_prompt_yields_nothing(self):
        self.assertEqual(_remind.detect_questions(""), [])


class DetectDirectivesTests(unittest.TestCase):
    """Directive detection -- used only to select the reminder variant."""

    def test_imperative_lead_is_a_directive(self):
        self.assertTrue(_remind.detect_directives("add a test"))

    def test_please_prefix_is_a_directive(self):
        self.assertTrue(_remind.detect_directives("please add a test"))

    def test_lets_prefix_is_a_directive(self):
        self.assertTrue(_remind.detect_directives("let's ship it"))

    def test_pure_question_is_not_a_directive(self):
        self.assertFalse(_remind.detect_directives("is it done?"))


class ComposeReminderTests(unittest.TestCase):
    """Reminder composition: variant, quoting, truncation, count, clamp."""

    def test_pure_variant_when_no_directives(self):
        out = _remind.compose_reminder(["Is it done?"], False)
        self.assertIn("Questions are questions.", out)
        self.assertIn("contains 1 question(s)", out)
        self.assertIn("1. Is it done?", out)

    def test_mixed_variant_when_directives(self):
        out = _remind.compose_reminder(["Should we merge this?"], True)
        self.assertIn("mixes questions with directives", out)
        self.assertIn("act only on what is explicitly directed.", out)

    def test_only_first_five_quoted_but_count_is_total(self):
        qs = [f"Question number {i}?" for i in range(1, 8)]  # 7 questions
        out = _remind.compose_reminder(qs, False)
        self.assertIn("contains 7 question(s)", out)
        self.assertIn("5. ", out)
        self.assertNotIn("6. ", out)

    def test_quote_hard_sliced_to_200_chars_no_ellipsis(self):
        long_q = "why " + ("x" * 300) + "?"
        out = _remind.compose_reminder([long_q], False)
        self.assertNotIn("…", out)  # no ellipsis char
        self.assertIn("1. " + long_q[:_remind.MAX_QUOTE_CHARS] + "\n", out)
        self.assertNotIn(long_q[:_remind.MAX_QUOTE_CHARS] + "x", out)

    def test_reminder_stays_within_hard_cap(self):
        qs = ["why " + ("x" * 300) + "?" for _ in range(20)]
        out = _remind.compose_reminder(qs, False)
        self.assertLessEqual(len(out), _remind.MAX_REMINDER_CHARS)
        self.assertIn("Questions are questions.", out)  # template text survives


if __name__ == "__main__":
    unittest.main()
