#!/usr/bin/env python3
"""Agent/Task model policy and the agent-.md frontmatter pin fallback.

Target: ``plugins/model-guard/scripts/enforce_explicit_model.py``. The hook's
*behaviour* is the contract; this module pins the Agent-side contract:

    AgentPolicyTests            Agent/Task tool_input.model policy.
    FrontmatterPinTests         agent-.md frontmatter `model:` fallback (walk-up
                                + HOME, traversal refusal, banned-pin deny).
    FrontmatterBodyScanTests    the pin is honoured ONLY inside a well-formed
                                leading `--- ... ---` block, never body prose.

The runner, payload builders and decision asserts live in ``helpers``. Do NOT
weaken an assertion to make a red test pass -- the hook is authoritative; a
persistent disagreement is a suspected hook bug to report, not to paper over.

stdlib only; python3 >= 3.14. Run: python3 -m unittest discover <this dir> -v
"""

import os
import tempfile
import unittest

from helpers import HookTestCase, agent_payload, task_payload


# =========================================================================
# Agent / Task model policy
# =========================================================================
class AgentPolicyTests(HookTestCase):
    """tool_input.model policy for Agent and its Task alias: an explicit,
    non-banned string model abstains; everything else (missing, blank, banned,
    non-string) denies. Banning is case-insensitive."""

    VALID = ("haiku", "sonnet", "opus")
    BANNED = ("fable", "inherit")

    def test_missing_model_denies(self):
        """A spawn with no model key is denied for the missing-model reason."""
        self.assert_deny(
            agent_payload({"subagent_type": "general-purpose", "prompt": "reply done"}),
            reason_substring="model",
        )

    def test_valid_models_abstain(self):
        """Each allowed model name (haiku/sonnet/opus) abstains."""
        for model in self.VALID:
            with self.subTest(model=model):
                self.assert_abstain(agent_payload({"model": model, "prompt": "x"}))

    def test_blank_model_denies(self):
        """An empty or whitespace-only model is denied."""
        for blank in ("", "   ", "\t", "\n"):
            with self.subTest(blank=repr(blank)):
                self.assert_deny(
                    agent_payload({"model": blank, "prompt": "x"}),
                    reason_substring="model",
                )

    def test_banned_models_deny(self):
        """A banned model (fable/inherit) is denied for the banned reason."""
        for model in self.BANNED:
            with self.subTest(model=model):
                self.assert_deny(
                    agent_payload({"model": model, "prompt": "x"}),
                    reason_substring="banned",
                )

    def test_banned_is_case_insensitive(self):
        """Banning ignores case and surrounding whitespace."""
        for model in ("FABLE", "Fable", "INHERIT", "InHeRiT", " fable "):
            with self.subTest(model=model):
                self.assert_deny(
                    agent_payload({"model": model, "prompt": "x"}),
                    reason_substring="banned",
                )

    def test_non_string_model_denies(self):
        """A non-str model fails the isinstance gate and denies as missing."""
        for model in (123, 1.5, True, None, {"name": "opus"}, ["opus"]):
            with self.subTest(model=model):
                self.assert_deny(
                    agent_payload({"model": model, "prompt": "x"}),
                    reason_substring="model",
                )

    def test_task_alias_matches_agent(self):
        """Task behaves identically to Agent: deny on missing, abstain on valid."""
        self.assert_deny(task_payload({"prompt": "x"}), reason_substring="model")
        self.assert_abstain(task_payload({"model": "opus"}))


# =========================================================================
# frontmatter pin fallback
# =========================================================================
class FrontmatterPinTests(HookTestCase):
    """When the call omits a model, an agent's ``.md`` frontmatter `model:` pin
    is an acceptable explicit per-task choice. The lookup walks UP from cwd
    through every ancestor's .claude/agents/<type>.md, then falls back to
    ~/.claude/agents/<type>.md. A banned pin still denies; a type containing a
    path separator is rejected (no traversal); an explicit call-site model is
    checked before any pin lookup."""

    def _write_pin(self, base_dir, type_name, model):
        agents_dir = os.path.join(base_dir, ".claude", "agents")
        os.makedirs(agents_dir, exist_ok=True)
        with open(
            os.path.join(agents_dir, type_name + ".md"), "w", encoding="utf-8"
        ) as f:
            f.write(
                f"---\nname: {type_name}\nmodel: {model}\ndescription: t\n---\nBody.\n"
            )

    def test_pin_allows_omitted_model(self):
        """A non-banned frontmatter pin lets an omitted call-site model through."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_pin(tmp, "reviewer", "sonnet")
            self.assert_abstain(agent_payload({"subagent_type": "reviewer"}, cwd=tmp))

    def test_pin_found_walking_up_from_subdir(self):
        """The pin lookup walks up from a nested cwd to find the ancestor pin."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_pin(tmp, "reviewer", "opus")
            deep = os.path.join(tmp, "a", "b", "c")
            os.makedirs(deep, exist_ok=True)
            self.assert_abstain(agent_payload({"subagent_type": "reviewer"}, cwd=deep))

    def test_pin_found_via_home_fallback(self):
        """A pin reachable only via the HOME fallback (no ancestor of cwd holds
        it, only ~/.claude/agents) is still resolved."""
        with (
            tempfile.TemporaryDirectory() as home,
            tempfile.TemporaryDirectory() as work,
        ):
            self._write_pin(home, "hp_home_only_pin_zzz", "haiku")
            self.assert_abstain(
                agent_payload({"subagent_type": "hp_home_only_pin_zzz"}, cwd=work),
                env={"HOME": home},
            )

    def test_banned_pin_denies(self):
        """A frontmatter pin naming a banned model still denies."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_pin(tmp, "reviewer", "fable")
            self.assert_deny(
                agent_payload({"subagent_type": "reviewer"}, cwd=tmp),
                reason_substring="banned",
            )

    def test_unknown_type_denies(self):
        """A subagent_type with no matching pin file denies as missing model."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assert_deny(
                agent_payload({"subagent_type": "does-not-exist"}, cwd=tmp),
                reason_substring="model",
            )

    def test_traversal_type_rejected(self):
        """A subagent_type carrying a path separator (or `.` / `..`) is refused
        before any file read, so no pin can be smuggled from outside the agents
        directory."""
        with tempfile.TemporaryDirectory() as tmp:
            # Plant a real pin one level up to prove it is NOT reachable.
            self._write_pin(os.path.dirname(tmp), "reviewer", "opus")
            for bad in (
                "../reviewer",
                "..\\reviewer",
                "a/b",
                ".",
                "..",
                "sub/../reviewer",
            ):
                with self.subTest(type=bad):
                    self.assert_deny(
                        agent_payload({"subagent_type": bad}, cwd=tmp),
                        reason_substring="model",
                    )

    def test_non_utf8_frontmatter_denies_without_crash(self):
        """An agent .md whose frontmatter carries non-UTF-8 bytes must not crash
        the hook: reading it raises UnicodeDecodeError (a ValueError), which is
        caught so the unresolved pin falls through to the omitted-model deny
        rather than aborting the process (which would fail OPEN). HOME is pointed
        at an empty dir and the type name is unique so no other pin can resolve."""
        type_name = "mg_nonutf8_pin_zzz"
        with (
            tempfile.TemporaryDirectory() as home,
            tempfile.TemporaryDirectory() as tmp,
        ):
            agents_dir = os.path.join(tmp, ".claude", "agents")
            os.makedirs(agents_dir)
            with open(os.path.join(agents_dir, type_name + ".md"), "wb") as f:
                f.write(b"---\nname: x\nmodel: \xffopus\n---\nBody.\n")
            self.assert_deny(
                agent_payload({"subagent_type": type_name}, cwd=tmp),
                reason_substring="model",
                env={"HOME": home},
            )

    def test_explicit_call_model_checked_before_pin(self):
        """A banned call-site model denies even when a valid pin exists: a pin
        never rescues an explicitly banned call-site model."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_pin(tmp, "reviewer", "opus")
            self.assert_deny(
                agent_payload({"subagent_type": "reviewer", "model": "fable"}, cwd=tmp),
                reason_substring="banned",
            )


# =========================================================================
# frontmatter pin: only a well-formed leading block, never body prose
# =========================================================================
class FrontmatterBodyScanTests(HookTestCase):
    """A `model:` pin is honoured ONLY inside a well-formed leading `--- ... ---`
    frontmatter block. A `model:` line in an agent .md's BODY prose, or under a
    `---` that never closes, must grant no pin -- so an omitted-model spawn still
    denies; a duplicate `model:` in the block is ambiguous and grants no pin; a
    properly delimited block still pins."""

    def _write_agent_md(self, base_dir, type_name, text):
        agents_dir = os.path.join(base_dir, ".claude", "agents")
        os.makedirs(agents_dir, exist_ok=True)
        path = os.path.join(agents_dir, type_name + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_body_model_line_grants_no_pin(self):
        """`model: opus` in body prose (no frontmatter at all) grants no pin."""
        with (
            tempfile.TemporaryDirectory() as home,
            tempfile.TemporaryDirectory() as tmp,
        ):
            # The `model:` sits at a body line start -- exactly the shape the old
            # whole-file scan mistook for a pin -- but there is no frontmatter.
            self._write_agent_md(
                tmp,
                "mg_body_prose_zzz",
                "# Reviewer agent\n\nExample config:\n\nmodel: opus\n\nUse it well.\n",
            )
            self.assert_deny(
                agent_payload({"subagent_type": "mg_body_prose_zzz"}, cwd=tmp),
                reason_substring="model",
                env={"HOME": home},
            )

    def test_unclosed_frontmatter_grants_no_pin(self):
        """A leading `---` that never closes is not a well-formed block -> deny."""
        with (
            tempfile.TemporaryDirectory() as home,
            tempfile.TemporaryDirectory() as tmp,
        ):
            self._write_agent_md(
                tmp,
                "mg_unclosed_zzz",
                "---\nname: x\nmodel: opus\nno closing delimiter here\n",
            )
            self.assert_deny(
                agent_payload({"subagent_type": "mg_unclosed_zzz"}, cwd=tmp),
                reason_substring="model",
                env={"HOME": home},
            )

    def test_duplicate_frontmatter_model_grants_no_pin(self):
        """Two `model:` keys in the block are ambiguous (YAML keeps the last, a
        first-wins read would take the first) -> no pin, fail-closed."""
        with (
            tempfile.TemporaryDirectory() as home,
            tempfile.TemporaryDirectory() as tmp,
        ):
            self._write_agent_md(
                tmp,
                "mg_dup_fm_zzz",
                "---\nname: x\nmodel: opus\nmodel: fable\n---\nBody.\n",
            )
            self.assert_deny(
                agent_payload({"subagent_type": "mg_dup_fm_zzz"}, cwd=tmp),
                reason_substring="model",
                env={"HOME": home},
            )

    def test_wellformed_frontmatter_still_pins(self):
        """A properly delimited `--- ... model: sonnet ... ---` still pins; a
        `model:` line in the body after the block does not override it -> abstain."""
        with (
            tempfile.TemporaryDirectory() as home,
            tempfile.TemporaryDirectory() as tmp,
        ):
            self._write_agent_md(
                tmp,
                "mg_wellformed_zzz",
                "---\nname: x\nmodel: sonnet\n---\nBody mentions model: opus prose.\n",
            )
            self.assert_abstain(
                agent_payload({"subagent_type": "mg_wellformed_zzz"}, cwd=tmp),
                env={"HOME": home},
            )


if __name__ == "__main__":
    unittest.main()
