#!/usr/bin/env python3
"""Workflow payload shapes and the token-stream lint semantics.

Target: ``plugins/model-guard/scripts/enforce_explicit_model.py``. The hook's
*behaviour* is the contract; this module pins the Workflow-side contract:

    WorkflowShapeTests          Workflow payload shapes + strict env flag.
    LintSemanticsTests          top-level model: key detection + value class.
    DuplicateModelKeyTests      a duplicate top-level model key fails closed
                                (JS keeps the LAST duplicate).
    OptionalCallTests           agent?.(...) / workflow?.(...) are real spawns.

The runner, payload builders and decision asserts live in ``helpers``. Do NOT
weaken an assertion to make a red test pass -- the hook is authoritative; a
persistent disagreement is a suspected hook bug to report, not to paper over.

stdlib only; python3 >= 3.14. Run: python3 -m unittest discover <this dir> -v
"""

import os
import tempfile
import unittest

from helpers import HookTestCase, workflow_payload, workflow_script


# =========================================================================
# Workflow payload shapes + strict env flag
# =========================================================================
class WorkflowShapeTests(HookTestCase):
    """Which Workflow payloads can be statically inspected. An inline `script`
    or a readable `scriptPath` is linted. A saved workflow invoked by `name` /
    `resumeFromRunId` cannot be inspected -> ask (deny under the strict env
    flag). A payload with none of those fields, or an unreadable scriptPath,
    denies (fail-closed)."""

    def test_name_only_asks(self):
        """A name-only saved workflow cannot be inspected -> ask."""
        self.assert_ask(
            workflow_payload({"name": "deep-research"}), reason_substring="statically"
        )

    def test_resume_only_asks(self):
        """A resumeFromRunId-only workflow cannot be inspected -> ask."""
        self.assert_ask(
            workflow_payload({"resumeFromRunId": "run_123"}),
            reason_substring="statically",
        )

    def test_strict_flag_denies_saved_workflow(self):
        """The strict env flag flips saved-workflow ask to deny for truthy values."""
        # _env_flag accepts any of these truthy spellings -> strict deny; any
        # falsey / unset spelling leaves the default ask.
        for truthy in ("1", "true", "TRUE", "yes", "on", " On "):
            with self.subTest(flag=repr(truthy)):
                self.assert_deny(
                    workflow_payload({"name": "deep-research"}),
                    env={"MODEL_GUARD_STRICT_SAVED_WORKFLOWS": truthy},
                )
        for falsey in ("0", "false", "no", "off", "", "  ", "banana"):
            with self.subTest(flag=repr(falsey)):
                self.assert_ask(
                    workflow_payload({"resumeFromRunId": "r1"}),
                    env={"MODEL_GUARD_STRICT_SAVED_WORKFLOWS": falsey},
                )

    def test_no_verifiable_field_denies(self):
        """A payload with no script/scriptPath/name/resume denies."""
        self.assert_deny(workflow_payload({"args": {"foo": 1}}))

    def test_empty_tool_input_denies(self):
        """An empty tool_input has nothing to verify -> deny."""
        # {} has no script / scriptPath / name / resume -> nothing to verify.
        self.assert_deny(workflow_payload({}))

    def test_scriptpath_linted_and_denied_when_bad(self):
        """A readable scriptPath is linted and its model-less call denies."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "wf.js"), "w", encoding="utf-8") as f:
                f.write("agent('x', { temperature: 1 });")
            self.assert_deny(
                workflow_payload({"scriptPath": "wf.js"}, cwd=tmp),
                reason_substring="model",
            )

    def test_scriptpath_clean_abstains(self):
        """A readable scriptPath whose calls are all modeled abstains."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "wf.js"), "w", encoding="utf-8") as f:
                f.write("agent('x', { model: 'opus' });")
            self.assert_abstain(workflow_payload({"scriptPath": "wf.js"}, cwd=tmp))

    def test_scriptpath_unreadable_denies(self):
        """An unreadable scriptPath denies (fail-closed)."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assert_deny(workflow_payload({"scriptPath": "missing.js"}, cwd=tmp))

    def test_scriptpath_non_utf8_denies_without_crash(self):
        """A scriptPath whose bytes are not valid UTF-8 fails closed -> deny. The
        read raises UnicodeDecodeError (a ValueError); it must be caught, not
        crash the hook to empty stdout / a non-zero exit (which would fail OPEN).
        run_hook asserts exit 0, so a crash here surfaces as a hard failure."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "wf.js"), "wb") as f:
                f.write(b"agent('x', { temperature: 1 });\xff")
            self.assert_deny(workflow_payload({"scriptPath": "wf.js"}, cwd=tmp))

    def test_inline_script_clean_abstains(self):
        """A clean inline script abstains."""
        self.wf_abstain("agent('do a', { model: 'opus' });")

    def test_empty_script_abstains(self):
        """An empty script has no spawns -> abstain."""
        self.wf_abstain("")


# =========================================================================
# lint semantics: model: key detection + value classification
# =========================================================================
class LintSemanticsTests(HookTestCase):
    """Given a correctly-lexed stream, the lint accepts a spawn iff its second
    argument is an object literal with a DEPTH-1 `model` key in KEY position
    whose value is not a banned/blank literal. This class pins key detection
    (top-level only, quoted keys, ternary value-position rejection), value
    classification (literal valid / blank / banned / dynamic), the no-options
    denials, nested-workflow denial, pipeline pass-through, interpolation
    recursion, and multi-call reporting."""

    # ---- key must be top-level and in key position -----------------------
    def test_bareword_and_quoted_keys_accepted(self):
        """A bareword or quoted `model` key in key position is accepted."""
        for key in ("model", "'model'", '"model"'):
            with self.subTest(key=key):
                self.wf_abstain(f"agent('x', {{ {key}: 'sonnet' }});")

    def test_nested_model_key_not_counted(self):
        """A `model` nested inside a sub-object is not the top-level opts key."""
        # A `model` nested inside a sub-object (a schema) is not the top-level
        # opts key -> deny.
        self.wf_deny(
            "agent('x', { schema: { type: 'object', "
            "properties: { model: { type: 'string' } } } });",
            reason_substring="model",
        )

    def test_ternary_value_position_model_not_a_key(self):
        """A `model` token in a ternary value position is not a key -> deny."""
        # A `model` token sitting in a ternary VALUE position (before the
        # ternary's own colon, or in a branch) is not a key. Bareword, double-
        # and single-quoted, and multiline forms all deny.
        for opts in (
            "{ x: cond ? model : other }",
            '{ onErr: flag ? "model" : "skip" }',
            "{ x: c ? 'model' : y }",
            "{ k: cond\n ? model\n : fallback }",
        ):
            with self.subTest(opts=opts):
                self.wf_deny(f"agent('do', {opts});", reason_substring="model")

    def test_real_key_counts_despite_ternary_value_model(self):
        """A genuine top-level model key satisfies despite a ternary value `model`
        token elsewhere in the same object."""
        self.wf_abstain("agent('do', { x: cond ? model : other, model: 'opus' });")

    def test_key_after_call_value_comma_counts(self):
        """A model key after a comma that follows a call value still counts."""
        self.wf_abstain("agent('do', { a: fn(1, 2), model: 'sonnet' });")

    # ---- value classification --------------------------------------------
    def test_valid_literal_values_abstain(self):
        """Each valid literal model value abstains."""
        for mv in ("haiku", "sonnet", "opus"):
            with self.subTest(model=mv):
                self.wf_abstain(f"agent('x', {{ model: '{mv}' }});")

    def test_dynamic_value_abstains(self):
        """A non-literal (identifier) model value is treated as satisfying."""
        # A non-literal (identifier) model value cannot be statically resolved;
        # it is treated as satisfying presence, not denied (documented).
        self.wf_abstain("agent('x', { model: chosenModel });")

    def test_escaped_model_value_denies(self):
        r"""A JS string / template escape in the model value folds to a banned
        model at runtime (e.g. 'fabl\x65' === 'fable'); the raw source never
        equals it, so the value must fail closed rather than classify 'ok'."""
        for opts in (
            r"{ model: 'fabl\x65' }",
            r"{ model: '\x66able' }",
            r"{ model: '\x66\x61\x62\x6c\x65' }",
            r"{ model: 'fabl\u{65}' }",
            r"{ model: 'i\x6eherit' }",
            r"{ model: `fabl\x65` }",
        ):
            with self.subTest(opts=opts):
                self.wf_deny(
                    f"agent('t', {opts});", reason_substring="plain string literal"
                )

    def test_concatenated_model_value_denies(self):
        """A '+'-concatenation of string literals folds to a banned model
        ('fa' + 'ble' === 'fable') yet is no single literal -> fail closed."""
        for opts in ("{ model: 'fa' + 'ble' }", "{ model: 'in' + 'herit' }"):
            with self.subTest(opts=opts):
                self.wf_deny(
                    f"agent('t', {opts});", reason_substring="plain string literal"
                )

    def test_static_interpolation_model_value_denies(self):
        """A template that interpolates string literals folds to a banned model
        (`fa${''}ble` === 'fable') yet is not a plain literal -> fail closed."""
        self.wf_deny(
            "agent('t', { model: `fa${''}ble` });",
            reason_substring="plain string literal",
        )

    def test_string_bearing_dynamic_value_denies(self):
        """The tightened boundary: any string-token-bearing model expression that
        is not one plain literal (here a ternary / a `||` default) fails closed,
        since a literal branch could be the banned model."""
        for opts in (
            "{ model: cond ? 'opus' : 'fable' }",
            "{ model: chosen || 'fable' }",
        ):
            with self.subTest(opts=opts):
                self.wf_deny(
                    f"agent('t', {opts});", reason_substring="plain string literal"
                )

    def test_blank_literal_values_deny(self):
        """An empty or whitespace-only literal model value denies as blank."""
        # Only truly empty / whitespace PLAIN literals classify as blank; an
        # escaped literal (e.g. '\t') never reaches here -- it fails closed on
        # its backslash first (see test_escaped_model_value_denies).
        for opts in (
            "{ model: '' }",
            '{ model: "" }',
            "{ model: '  ' }",
            "{ model: `` }",
            "{ model: `   ` }",
        ):
            with self.subTest(opts=opts):
                self.wf_deny(f"agent('x', {opts});", reason_substring="blank")

    def test_banned_literal_values_deny(self):
        """A banned literal model value denies across quote forms and case."""
        # Banned across quote forms and case -- matches the Agent-side ban.
        for opts in (
            "{ model: 'fable' }",
            "{ model: 'inherit' }",
            "{ model: 'FABLE' }",
            '{ model: "fable" }',
            "{ model: `fable` }",
            "{ \"model\": 'inherit' }",
        ):
            with self.subTest(opts=opts):
                self.wf_deny(f"agent('x', {opts});", reason_substring="banned")

    # ---- no options object -----------------------------------------------
    def test_missing_model_key_denies(self):
        """An options object without a model key denies."""
        self.wf_deny("agent('do a', { temperature: 1 });", reason_substring="model")

    def test_no_second_arg_denies(self):
        """A call with only a prompt and no options object denies."""
        self.wf_deny("agent('just a prompt');", reason_substring="model")

    def test_empty_call_denies(self):
        """An argument-less agent() call denies."""
        self.wf_deny("agent();", reason_substring="model")

    def test_non_object_opts_deny(self):
        """A spread or bare identifier is not an inspectable options object."""
        self.wf_deny("agent('x', { ...base });", reason_substring="model")
        self.wf_deny("agent('x', opts);", reason_substring="model")

    # ---- nested workflow() -----------------------------------------------
    def test_nested_workflow_call_denies(self):
        """A nested workflow() call cannot be verified and denies."""
        self.assert_deny(
            workflow_script("agent('a', { model: 'opus' }); workflow('other');"),
            reason_substring="workflow",
        )

    # ---- pipeline() is a plain call, not a spawn -------------------------
    def test_pipeline_wrapper_not_flagged(self):
        """pipeline() needs no model; its agent() children are each linted."""
        self.wf_abstain(
            "pipeline(\n  agent('a', { model: 'haiku' }),\n"
            "  agent('b', { model: 'sonnet' }),\n);"
        )
        self.wf_deny(
            "pipeline(\n  agent('a', { model: 'haiku' }),\n"
            "  agent('b', { temperature: 1 }),\n);",
            reason_substring="model",
        )

    # ---- calls inside template interpolations ----------------------------
    def test_call_inside_interpolation_is_linted(self):
        """A model-less agent() inside a ${...} interpolation is still linted."""
        # A model-less agent() inside a ${...} is still linted; a `model:` living
        # in the interpolation's own code text is not the outer opts key.
        self.wf_deny(
            'agent(`x ${a("model: y")}`, { other: 1 });', reason_substring="model"
        )

    # ---- multiple calls: the offender is named ---------------------------
    def test_multi_call_names_missing_offender(self):
        """With several calls, the missing-model offender is named in the reason."""
        self.wf_deny(
            "agent('good', { model: 'opus' });\n"
            "agent('the-bad-one', { temperature: 1 });\n",
            reason_substring="the-bad-one",
        )

    def test_multi_call_names_banned_offender(self):
        """With several calls, the banned-model offender is named in the reason."""
        self.wf_deny(
            "agent('good', { model: 'opus' });\n"
            "agent('the-bad-one', { model: 'fable' });\n",
            reason_substring="the-bad-one",
        )


# =========================================================================
# duplicate top-level model key (JS keeps the LAST duplicate)
# =========================================================================
class DuplicateModelKeyTests(HookTestCase):
    """A duplicate top-level `model` key must not be classified from the FIRST
    entry: JavaScript object semantics keep the LAST duplicate, so a benign first
    `model` beside a banned / blank second would run the second while a first-
    wins read cleared the first. Any duplicate top-level model key denies (fail-
    closed), independent of which value the lint can resolve; a single legitimate
    model key is unaffected and still abstains."""

    def test_duplicate_valid_then_banned_denies(self):
        """`{model:'opus', model:'fable'}` runs 'fable' at runtime -> deny."""
        self.wf_deny(
            "agent('x', { model: 'opus', model: 'fable' });", reason_substring="model"
        )

    def test_duplicate_valid_then_blank_denies(self):
        """`{model:'opus', model:''}` smuggles a blank / inherit model -> deny."""
        self.wf_deny(
            "agent('x', { model: 'opus', model: '' });", reason_substring="model"
        )

    def test_duplicate_denies_even_when_last_is_dynamic(self):
        """A duplicate whose LAST value is dynamic still denies. Last-wins would
        clear it (a dynamic value reads as satisfying), so this pins the sound
        choice -- deny every duplicate -- over merely classifying the last."""
        self.wf_deny(
            "agent('x', { model: 'opus', model: chosen });", reason_substring="model"
        )

    def test_quoted_and_bareword_duplicate_denies(self):
        """Duplicate detection sees quoted and bareword `model` keys alike."""
        self.wf_deny(
            "agent('x', { 'model': 'opus', model: 'fable' });", reason_substring="model"
        )

    def test_single_model_key_still_abstains(self):
        """A legitimate single top-level model key is unaffected -> abstain."""
        self.wf_abstain("agent('x', { model: 'opus' });")


# =========================================================================
# optional-call spawns: agent?.(...) / workflow?.(...) are real spawns
# =========================================================================
class OptionalCallTests(HookTestCase):
    """The optional-call form `agent?.(...)` lexes as the name then '?' '.' '(',
    not name-then-'(', so the call detector must recognise it or a syntactically
    valid, runtime-executable spawn slips past every model / banned / nested-
    workflow check. These pin that it is linted exactly like `agent(...)`, that
    optional MEMBER access `obj?.agent(...)` stays excluded, and that a bare
    `agent ? a : b` ternary is not mistaken for a call."""

    def test_optional_call_spawns_are_linted(self):
        """`agent?.(...)` is linted like `agent(...)`: model-less and banned deny,
        a valid model abstains; `workflow?.(...)` is a nested workflow -> deny."""
        self.wf_deny("agent?.('x', { temperature: 1 });", reason_substring="model")
        self.wf_deny("agent?.('x', { model: 'fable' });", reason_substring="banned")
        self.wf_abstain("agent?.('x', { model: 'opus' });")
        self.assert_deny(
            workflow_script("workflow?.('other');"), reason_substring="workflow"
        )

    def test_optional_member_call_not_matched(self):
        """obj?.agent(...) is optional member access, not the DSL agent -> abstain."""
        self.wf_abstain("obj?.agent('x');")

    def test_agent_ternary_is_not_a_call(self):
        """A bare `agent ? a : b` ternary is not a spawn (no '.(' after '?')."""
        self.wf_abstain("const z = agent ? a : b;")


# =========================================================================
# simple-options whitelist: fail closed on constructs that can hide the model
# =========================================================================
class SimpleOptionsShapeTests(HookTestCase):
    """The options object is accepted ONLY when every top-level entry is a
    statically-simple `simple-key : value` (bareword or escape-free quoted key).
    Any construct whose effective model the lint cannot prove -- a computed key,
    an escaped-name key, a spread, a getter/setter/method/shorthand entry, or a
    prototype-mutating __proto__ key -- fails closed to a deny, even when a benign
    `model:'opus'` sits beside it (each construct below runs / masks a banned
    'fable' at runtime yet slipped past the pre-fix key-spelling enumeration).

    Teeth: every deny case here ALLOWS under HEAD:_enforce.py (proven at build);
    a legitimate simple object and a dynamic model value still abstain."""

    def test_computed_key_denies(self):
        """A computed `['model']` key can override the model -> deny."""
        self.wf_deny(
            "agent('x', { model: 'opus', ['model']: 'fable' });",
            reason_substring="non-simple",
        )

    def test_escaped_name_key_denies(self):
        r"""An escaped quoted key `'mod\x65l'` is 'model' at runtime -> deny."""
        self.wf_deny(
            r"agent('x', { model: 'sonnet', 'mod\x65l': 'fable' });",
            reason_substring="escaped",
        )

    def test_spread_entry_denies(self):
        """A spread `...{ model: 'fable' }` overrides the model -> deny."""
        self.wf_deny(
            "agent('x', { model: 'opus', ...{ model: 'fable' } });",
            reason_substring="non-simple",
        )

    def test_proto_key_denies(self):
        """A `__proto__` key mutates the prototype and cannot be verified -> deny."""
        self.wf_deny(
            "agent('x', { model: 'opus', __proto__: { model: 'fable' } });",
            reason_substring="__proto__",
        )

    def test_getter_entry_denies(self):
        """A `get model(){...}` getter redefines model at runtime -> deny."""
        self.wf_deny(
            "agent('x', { model: 'opus', get model() { return 'fable'; } });",
            reason_substring="non-simple",
        )

    def test_method_entry_denies(self):
        """A method entry `run(){...}` is not a `simple-key : value` -> deny."""
        self.wf_deny(
            "agent('x', { model: 'opus', run() { return 1; } });",
            reason_substring="non-simple",
        )

    def test_shorthand_entry_denies(self):
        """A shorthand entry `extra` is not a `simple-key : value` -> deny."""
        self.wf_deny(
            "agent('x', { model: 'opus', extra });", reason_substring="non-simple"
        )

    def test_simple_valid_object_still_abstains(self):
        """A wholly-simple object with a valid model still passes."""
        self.wf_abstain("agent('x', { model: 'opus', label: 'y' });")

    def test_dynamic_model_value_still_abstains(self):
        """A simple entry with a dynamic (identifier) model value still passes."""
        self.wf_abstain("agent('x', { model: chosenModel });")


if __name__ == "__main__":
    unittest.main()
