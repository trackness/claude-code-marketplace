#!/usr/bin/env python3
"""Shared test harness for the model-guard enforcement hook.

The hook (``scripts/enforce_explicit_model.py``) is a PreToolUse hook: it reads
a JSON payload on stdin and either emits a ``hookSpecificOutput`` JSON object on
stdout (permissionDecision ``deny`` / ``ask`` / ``allow``) or emits nothing and
exits 0 (an *abstain* -- normal permission flow proceeds). It ALWAYS exits 0.

This module is the single point of contact with that contract:

* ``run_hook(payload, env=None, cwd=None)`` drives the hook as a real
  subprocess and normalises its output into a :class:`HookResult`.
* ``agent_payload`` / ``task_payload`` / ``workflow_payload`` build the three
  PreToolUse payload shapes the hook handles.
* :class:`HookTestCase` adds ``assert_deny`` / ``assert_ask`` / ``assert_abstain``
  so every test states the decision it expects and the reason substring that
  proves the hook denied for the *right* reason.

stdlib only; python3 >= 3.14.
"""

import json
import os
import subprocess
import sys
import unittest
from typing import NamedTuple

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.abspath(
    os.path.join(HERE, "..", "scripts", "enforce_explicit_model.py")
)


class HookResult(NamedTuple):
    """One hook invocation, normalised for assertions.

    ``decision`` is the permissionDecision the hook emitted -- ``deny`` / ``ask``
    / ``allow`` -- or ``abstain`` when it emitted nothing. ``reason`` is the
    permissionDecisionReason (``''`` when abstaining). ``returncode`` / ``stdout``
    / ``stderr`` are the raw process result, kept so the fail-safe tests can
    assert on the exit code and empty output directly.
    """

    decision: str
    reason: str
    returncode: int
    stdout: str
    stderr: str


def run_hook(payload, env=None, cwd=None):
    """Run the hook once and return a :class:`HookResult`.

    ``payload`` is a dict (JSON-encoded and sent on stdin) or a raw ``str`` sent
    verbatim -- the latter feeds the garbage/empty-stdin fail-safe tests. ``env``
    overlays the parent environment (used for the strict-flag and HOME-fallback
    tests). ``cwd`` sets the subprocess working directory (the hook's
    ``os.getcwd()`` fallback); per-payload ``cwd`` is set separately by the
    payload builders and drives the frontmatter walk-up.
    """
    stdin_text = payload if isinstance(payload, str) else json.dumps(payload)
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, SCRIPT],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=full_env,
        cwd=cwd,
        check=False,  # returncode is asserted explicitly just below
    )
    assert proc.returncode == 0, (
        f"hook must always exit 0, got {proc.returncode!r} (stderr={proc.stderr!r})"
    )
    emitted = proc.stdout.strip()
    if not emitted:
        return HookResult("abstain", "", proc.returncode, proc.stdout, proc.stderr)
    hook_output = json.loads(emitted)["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse", hook_output
    return HookResult(
        hook_output["permissionDecision"],
        hook_output.get("permissionDecisionReason", ""),
        proc.returncode,
        proc.stdout,
        proc.stderr,
    )


# ---- payload builders ---------------------------------------------------
def agent_payload(tool_input, cwd=None, tool_name="Agent"):
    """A PreToolUse payload for an Agent (or Task) spawn."""
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": cwd or os.getcwd(),
    }


def task_payload(tool_input, cwd=None):
    """The Task alias of Agent -- the hook treats them identically."""
    return agent_payload(tool_input, cwd=cwd, tool_name="Task")


def workflow_payload(tool_input, cwd=None):
    """A PreToolUse payload for a Workflow spawn."""
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Workflow",
        "tool_input": tool_input,
        "cwd": cwd or os.getcwd(),
    }


def workflow_script(script, cwd=None):
    """Shorthand: a Workflow payload carrying an inline ``script``."""
    return workflow_payload({"script": script}, cwd=cwd)


# ---- assertion base -----------------------------------------------------
class HookTestCase(unittest.TestCase):
    """Base TestCase with decision-asserting helpers.

    Each helper runs the hook once and asserts the decision; ``assert_deny`` /
    ``assert_ask`` also take an optional ``reason_substring`` so a deny is
    pinned to the *contract reason* (``model`` / ``banned`` / ``blank`` /
    ``workflow`` / ``statically``), not merely to "it said no". The failing
    message surfaces the actual reason (and stderr) to make red tests legible.
    """

    def assert_deny(self, payload, reason_substring=None, env=None, cwd=None, msg=None):
        """Run the hook and assert it denied (optionally for a reason substring)."""
        res = run_hook(payload, env=env, cwd=cwd)
        self.assertEqual(
            res.decision,
            "deny",
            msg or f"expected deny; reason={res.reason!r} stderr={res.stderr!r}",
        )
        if reason_substring is not None:
            self.assertIn(reason_substring, res.reason, msg)
        return res

    def assert_ask(self, payload, reason_substring=None, env=None, cwd=None, msg=None):
        """Run the hook and assert it asked (optionally for a reason substring)."""
        res = run_hook(payload, env=env, cwd=cwd)
        self.assertEqual(
            res.decision,
            "ask",
            msg or f"expected ask; reason={res.reason!r} stderr={res.stderr!r}",
        )
        if reason_substring is not None:
            self.assertIn(reason_substring, res.reason, msg)
        return res

    def assert_abstain(self, payload, env=None, cwd=None, msg=None):
        """Run the hook and assert it abstained (emitted nothing, exit 0)."""
        res = run_hook(payload, env=env, cwd=cwd)
        self.assertEqual(
            res.decision,
            "abstain",
            msg
            or f"expected abstain; decision={res.decision!r} reason={res.reason!r} "
            f"stderr={res.stderr!r}",
        )
        return res

    # -- Workflow-script shorthands (most lexer/lint tests are script-driven) --
    def wf_deny(self, script, reason_substring=None, msg=None):
        """Assert an inline Workflow `script` is denied."""
        return self.assert_deny(
            workflow_script(script), reason_substring, msg=msg or script
        )

    def wf_abstain(self, script, msg=None):
        """Assert an inline Workflow `script` is abstained on (allowed through)."""
        return self.assert_abstain(workflow_script(script), msg=msg or script)
