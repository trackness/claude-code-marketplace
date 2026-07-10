#!/usr/bin/env python3
"""Fail-closed structural anomalies, fail-safe abstains, and the fuzz invariant.

Target: ``plugins/model-guard/scripts/enforce_explicit_model.py``. The hook's
*behaviour* is the contract; this module pins the two directional guarantees and
the seeded invariant that binds them:

    FailClosedTests             structural anomalies -> deny (fatal-safe).
    FailSafeAbstainTests        other tools / bad stdin -> abstain (never wedge).
    FuzzInvariantTests          seeded generator: the fatal-direction invariant.

The runner, payload builders and decision asserts live in ``helpers``. Do NOT
weaken an assertion to make a red test pass -- the hook is authoritative; a
persistent disagreement is a suspected hook bug to report, not to paper over.

stdlib only; python3 >= 3.14. Run: python3 -m unittest discover <this dir> -v
"""

import random
import unittest
from typing import ClassVar

from helpers import HookTestCase, run_hook, workflow_script

# Probabilities the seeded fuzz generator uses to include each optional field
# or wrap calls in a pipeline; named so the generator carries no bare literals.
_TEMPERATURE_PROB = 0.5
_TIMEOUT_PROB = 0.5
_TOOLS_PROB = 0.4
_PIPELINE_PROB = 0.5


# =========================================================================
# fail-closed structural anomalies
# =========================================================================
class FailClosedTests(HookTestCase):
    """Any structural anomaly the lint cannot resolve soundly is a LintError ->
    deny. This is the fatal-direction guarantee: unterminated literals /
    comments, unbalanced brackets, a stray identifier-escape backslash, and the
    documented quote-bearing division-vs-regex ambiguity all fail closed rather
    than risk silently allowing a smuggled model-less spawn."""

    def test_unterminated_string_denies(self):
        """An unterminated string literal fails closed -> deny."""
        self.wf_deny("agent('unterminated, { model: 'opus' })")

    def test_unterminated_template_denies(self):
        """An unterminated template literal fails closed -> deny."""
        self.wf_deny("agent(`unterminated, { model: 'opus' })")

    def test_unterminated_block_comment_denies(self):
        """An unterminated block comment fails closed -> deny."""
        self.wf_deny("agent('x', { model: 'opus' }); /* never closed")

    def test_unterminated_regex_denies(self):
        """A regex that never closes on its line fails closed -> deny."""
        # A regex opens in expression position and never closes on its line.
        self.wf_deny("const re = /unterminated\n; agent('x', {});")

    def test_unbalanced_parens_denies(self):
        """Unbalanced parentheses around a call fail closed -> deny."""
        self.wf_deny("agent('x', { model: 'opus' ")

    def test_unbalanced_braces_denies(self):
        """Unbalanced braces around a call fail closed -> deny."""
        self.wf_deny("agent('x', { model: 'opus' ) ;")

    def test_stray_backslash_denies(self):
        """A bare backslash (identifier-escape smuggling) fails closed -> deny."""
        # A bare '\' outside any string/regex/template is an identifier-escape
        # smuggling attempt -> fail closed.
        self.wf_deny("\\u0061gent('t', {})")

    def test_division_ambiguity_quote_bearing_denies(self):
        """A quote-bearing division-vs-regex slash is irresolvable -> deny."""
        # A division-context slash that ALSO reads as a single-line regex whose
        # span carries an unbalanced quote/backtick is irresolvable -> deny. Each
        # of these hides a model-less agent() under one of the two lexings.
        for script in (
            "a.split(/'/); agent('do', {t:1}); b.split(/'/);",
            'r = /"/; agent("x", {t:1}); q = /"/;',
            "r = /'/; agent('x', {t:1}); q = /'/;",
            "x = /a'b/; agent('m', {t:1}); y = /c'd/;",
            "foo /a'/ ; agent('do', {t:1}) ; bar = z /b'/;",
            "y = z /`/; agent('m', {t:1}); w = q /`/;",
        ):
            with self.subTest(script=script):
                self.assert_deny(workflow_script(script))

    def test_division_ambiguity_does_not_over_deny_modeled(self):
        """A balanced quote-bearing regex elsewhere must not fire the guard."""
        # The guard must not fire on a balanced quote-bearing regex elsewhere:
        # the one real spawn is modeled -> allow.
        self.wf_abstain("a.split(/'/); agent('do', { model: 'opus' });")

    def test_deeply_nested_interpolation_denies(self):
        """A RecursionError from pathologically nested ${...} fails closed -> deny.

        Guards the fail-open hole where the recursive template lexer
        (_read_template -> _lex -> _read_template ...) blows Python's recursion
        limit on a spawn buried under hundreds of nested template
        interpolations. Left uncaught that RecursionError would crash the hook
        to empty stdout -- an abstain the permission flow reads as ALLOW,
        smuggling the banned model-'fable' spawn straight through. The guard
        converts the crash into a fail-closed deny.
        """
        # Each ${...} level costs several lexer frames, so this depth overshoots
        # the default recursion limit well before the buried spawn is reached.
        depth = 600
        banned = "agent('x', { model: 'fable' })"
        script = "`${" * depth + banned + "}`" * depth
        self.wf_deny(script, reason_substring="too deeply")


# =========================================================================
# fail-safe abstains (never wedge a session)
# =========================================================================
class FailSafeAbstainTests(HookTestCase):
    """The hook governs only Agent/Task/Workflow and must never wedge a session
    on malformed input: any other tool, unparsable stdin, non-object JSON, and
    a missing/null tool_input all leave normal permission flow untouched -- with
    the one exception that an Agent whose tool_input is absent still denies (an
    empty tool_input has no model)."""

    def test_other_tools_abstain(self):
        """A tool the hook does not govern leaves permission flow untouched."""
        for payload in (
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}},
            {"tool_name": "Edit", "tool_input": {}},
        ):
            with self.subTest(tool=payload["tool_name"]):
                self.assert_abstain(payload)

    def test_garbage_stdin_exits_zero_silently(self):
        """Unparsable stdin exits 0 with no output."""
        res = run_hook("this is not json {{{")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "")

    def test_empty_stdin_exits_zero(self):
        """Empty stdin exits 0 with no output."""
        res = run_hook("")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "")

    def test_non_object_json_abstains(self):
        """Valid JSON that is not an object abstains silently."""
        for payload in ("[1, 2, 3]", "42", '"a string"', "null", "true"):
            with self.subTest(payload=payload):
                res = run_hook(payload)
                self.assertEqual(res.returncode, 0)
                self.assertEqual(res.stdout.strip(), "")

    def test_missing_tool_input_on_agent_denies(self):
        """An Agent with no tool_input defaults to {} -> omitted model -> deny."""
        self.assert_deny({"tool_name": "Agent"}, reason_substring="model")

    def test_null_tool_input_on_agent_denies(self):
        """A null (non-dict) tool_input is coerced to {} -> omitted model -> deny."""
        self.assert_deny(
            {"tool_name": "Agent", "tool_input": None}, reason_substring="model"
        )

    def test_null_tool_input_on_other_tool_abstains(self):
        """A null tool_input on an ungoverned tool abstains."""
        self.assert_abstain({"tool_name": "Bash", "tool_input": None})


# =========================================================================
# seeded fuzz: the fatal-direction invariant
# =========================================================================
class FuzzInvariantTests(HookTestCase):
    """A deterministic, seeded generator asserting the one-way contract on a few
    hundred non-ambiguous variants:

        * every fully model-LESS spawn set DENIES (fatal direction preserved);
        * every fully-modeled, allowed-literal spawn set ABSTAINS (no over-deny).

    Generated scripts avoid slashes and unbalanced quotes, so they are never in
    the fail-closed ambiguity zone -- the invariant is exact, not probabilistic.
    A fixed seed makes any failure reproducible.
    """

    SEED = 0xC0FFEE
    N = 300
    _WORDS: ClassVar[list[str]] = [
        "do",
        "run",
        "task",
        "step",
        "phase",
        "handle",
        "process",
        "check",
        "audit",
        "review",
        "plan",
        "build",
    ]

    def _prompt(self, rng):
        n = rng.randint(1, 4)
        return " ".join(rng.choice(self._WORDS) for _ in range(n))

    def _call(self, rng, modeled):
        q = rng.choice(["'", '"'])
        pstr = q + self._prompt(rng) + q  # prompt carries no quotes
        extras = []
        if rng.random() < _TEMPERATURE_PROB:
            extras.append(f"temperature: {rng.randint(0, 2)}")
        if rng.random() < _TIMEOUT_PROB:
            extras.append(f"timeout: {rng.randint(1, 99)}")
        if rng.random() < _TOOLS_PROB:
            extras.append(f"tools: [{rng.randint(0, 9)}, {rng.randint(0, 9)}]")
        if modeled:
            model = rng.choice(["haiku", "sonnet", "opus"])
            mq = rng.choice(["'", '"', "`"])
            keyfmt = rng.choice(["model", "'model'", '"model"'])
            parts = [*extras, f"{keyfmt}: {mq}{model}{mq}"]
            rng.shuffle(parts)
            joined = ", ".join(parts)
            return f"agent({pstr}, {{ {joined} }})"
        shape = rng.choice(["opts", "opts", "noarg", "empty"])
        if shape == "opts":
            if not extras:
                extras = ["temperature: 1"]
            joined = ", ".join(extras)
            return f"agent({pstr}, {{ {joined} }})"
        if shape == "noarg":
            return f"agent({pstr})"
        return "agent()"

    def _script(self, rng, modeled):
        n = rng.randint(1, 3)
        calls = [self._call(rng, modeled) for _ in range(n)]
        if n > 1 and rng.random() < _PIPELINE_PROB:
            return "pipeline(\n  " + ",\n  ".join(calls) + "\n);"
        return "".join(c + ";\n" for c in calls)

    def test_modelless_variants_always_deny(self):
        """Every generated fully model-less spawn set denies (fatal direction)."""
        rng = random.Random(self.SEED)
        for i in range(self.N):
            script = self._script(rng, modeled=False)
            with self.subTest(i=i, script=script):
                res = run_hook(workflow_script(script))
                self.assertEqual(
                    res.decision,
                    "deny",
                    f"model-less script must deny:\n{script}\n(reason={res.reason!r})",
                )

    def test_modeled_variants_always_abstain(self):
        """Every generated fully-modeled spawn set abstains (no over-deny)."""
        rng = random.Random(self.SEED ^ 0x9999)
        for i in range(self.N):
            script = self._script(rng, modeled=True)
            with self.subTest(i=i, script=script):
                res = run_hook(workflow_script(script))
                self.assertEqual(
                    res.decision,
                    "abstain",
                    f"fully-modeled script must abstain:\n{script}\n"
                    f"(reason={res.reason!r})",
                )


if __name__ == "__main__":
    unittest.main()
