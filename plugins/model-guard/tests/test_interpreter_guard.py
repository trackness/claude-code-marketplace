#!/usr/bin/env python3
"""Interpreter-version guard for the model-guard entry point.

The hook is launched as a bare ``python3`` (hooks.json). On a host where that
resolves to an interpreter older than 3.14, the 3.14-only enforcement logic
cannot even parse -- so ``enforce_explicit_model`` (the entry point) must emit
its OWN fail-CLOSED deny rather than crash to a non-zero exit, which Claude Code
treats as a non-blocking error that lets the unverified spawn proceed
(fail-OPEN). This suite drives the real entry script under a genuine < 3.14
interpreter and pins that fail-closed contract.

stdlib only; python3 >= 3.14 (the runner). Skips when no < 3.14 interpreter is
available to probe.
"""

import json
import shutil
import subprocess
import unittest

from helpers import SCRIPT, HookTestCase, agent_payload, workflow_script


def _find_old_python():
    """Path to a python3 interpreter older than 3.14, or None if none is on PATH.
    The child does the version comparison itself and prints 1 when it is too old,
    so the requirement lives in one place and no version literal leaks here."""
    seen = set()
    candidates = (
        "python3.13",
        "python3.12",
        "python3.11",
        "python3.10",
        "python3.9",
        "python3",
        "python",
    )
    for name in candidates:
        exe = shutil.which(name)
        if not exe or exe in seen:
            continue
        seen.add(exe)
        probe = subprocess.run(
            [exe, "-c", "import sys; print(int(sys.version_info[:2] < (3, 14)))"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.stdout.strip() == "1":
            return exe
    return None


class InterpreterGuardTests(HookTestCase):
    """The entry point must fail CLOSED (deny), not crash, on an interpreter that
    cannot run the 3.14-only logic."""

    def test_old_interpreter_fails_closed(self):
        """Run the real entry script under a < 3.14 interpreter: it must exit 0
        and emit a deny naming the version requirement, never a traceback."""
        exe = _find_old_python()
        if exe is None:
            self.skipTest("no python3 < 3.14 interpreter available to probe")
        for payload in (
            agent_payload({"prompt": "x"}),
            workflow_script("agent('x', { model: 'fable' });"),
        ):
            with self.subTest(tool=payload["tool_name"]):
                proc = subprocess.run(
                    [exe, SCRIPT],
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    proc.returncode, 0, f"entry must exit 0; stderr={proc.stderr!r}"
                )
                out = json.loads(proc.stdout)["hookSpecificOutput"]
                self.assertEqual(out["permissionDecision"], "deny")
                self.assertIn("3.14", out["permissionDecisionReason"])


if __name__ == "__main__":
    unittest.main()
