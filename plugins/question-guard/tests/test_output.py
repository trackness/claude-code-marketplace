#!/usr/bin/env python3
"""IO + entry-point tests for question-guard.

These drive the real entry script (``scripts/remind_questions.py``) as a
subprocess with a JSON payload on stdin -- the exact contract Claude Code uses --
and assert the two delivery modes (quiet ``additionalContext`` JSON vs. visible
plain stdout), the env-var truthiness parsing, the input scan cap, and the
fail-open silences. The hook must ALWAYS exit 0 and never write stderr.

stdlib only; python3 >= 3.14 (the runner).
"""

import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.abspath(os.path.join(HERE, "..", "scripts", "remind_questions.py"))


def run_entry(stdin_text, env=None):
    """Run the entry script once; return the CompletedProcess (exit asserted 0)."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, ENTRY],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )
    assert proc.returncode == 0, (
        f"hook must always exit 0, got {proc.returncode!r} (stderr={proc.stderr!r})"
    )
    return proc


def payload(prompt):
    """A UserPromptSubmit stdin payload carrying ``prompt``."""
    return json.dumps(
        {"hook_event_name": "UserPromptSubmit", "prompt": prompt}
    )


class QuietModeTests(unittest.TestCase):
    """Default (quiet) delivery emits the additionalContext JSON envelope."""

    def test_additional_context_shape(self):
        proc = run_entry(payload("Is the build green?"))
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        self.assertIn("Questions are questions.", out["additionalContext"])
        self.assertEqual(proc.stderr, "")

    def test_quiet_when_visible_is_falsey(self):
        for value in ("0", "false", "nope", ""):
            with self.subTest(value=value):
                proc = run_entry(
                    payload("Is it done?"), env={"QUESTION_GUARD_VISIBLE": value}
                )
                self.assertIn("hookSpecificOutput", proc.stdout)


class VisibleModeTests(unittest.TestCase):
    """QUESTION_GUARD_VISIBLE truthy -> plain-text stdout, no JSON envelope."""

    def test_visible_plain_stdout(self):
        proc = run_entry(
            payload("Is it done?"), env={"QUESTION_GUARD_VISIBLE": "1"}
        )
        self.assertTrue(proc.stdout.startswith("The user's latest message"))
        self.assertNotIn("hookSpecificOutput", proc.stdout)

    def test_visible_truthy_values_parsed(self):
        for value in ("1", "true", "yes", "on", "  TRUE  ", "Yes"):
            with self.subTest(value=value):
                proc = run_entry(
                    payload("Is it done?"), env={"QUESTION_GUARD_VISIBLE": value}
                )
                self.assertNotIn("hookSpecificOutput", proc.stdout)
                self.assertTrue(proc.stdout.startswith("The user's latest message"))


class FailOpenTests(unittest.TestCase):
    """Every non-question / malformed input path is silent with exit 0."""

    def test_silent_on_no_questions(self):
        proc = run_entry(payload("add a test."))
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(proc.stderr, "")

    def test_silent_on_malformed_json(self):
        proc = run_entry("this is not json")
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(proc.stderr, "")

    def test_silent_on_missing_prompt_key(self):
        proc = run_entry(json.dumps({"hook_event_name": "UserPromptSubmit"}))
        self.assertEqual(proc.stdout.strip(), "")
        self.assertEqual(proc.stderr, "")

    def test_scan_cap_ignores_question_beyond_20k(self):
        prompt = ("x" * _remind_scan_cap()) + " is this counted?"
        proc = run_entry(payload(prompt))
        self.assertEqual(proc.stdout.strip(), "")


def _remind_scan_cap():
    """Read INPUT_SCAN_CAP straight from the core module, no literal duplicated."""
    scripts = os.path.abspath(os.path.join(HERE, "..", "scripts"))
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import _remind  # noqa: PLC0415

    return _remind.INPUT_SCAN_CAP


if __name__ == "__main__":
    unittest.main()
