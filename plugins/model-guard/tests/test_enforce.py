#!/usr/bin/env python3
"""Specification suite for the model-guard PreToolUse hook.

Target: ``plugins/model-guard/scripts/enforce_explicit_model.py`` -- a token-
stream ECMAScript lexer + lint that denies any Agent/Task/Workflow spawn which
does not name an explicit, allowed model. The hook's *behaviour* is the
contract; this suite pins that contract by area:

    AgentPolicyTests            Agent/Task tool_input.model policy.
    FrontmatterPinTests         agent-.md frontmatter `model:` fallback.
    WorkflowShapeTests          Workflow payload shapes + strict env flag.
    LexerConformanceTests       ES lexing: strings, escapes, templates,
                                comments, regex-vs-division, flags, postfix.
    LintSemanticsTests          top-level model: key detection + value class.
    SlashClassificationRegressionTests
                                the member-access / slash-context regression
                                family (docstrings name each hole guarded).
    FailClosedTests             structural anomalies -> deny (fatal-safe).
    FailSafeAbstainTests        other tools / bad stdin -> abstain (never wedge).
    FuzzInvariantTests          seeded generator: the fatal-direction invariant.

The runner, payload builders and decision asserts live in ``helpers``. Do NOT
weaken an assertion to make a red test pass -- the hook is authoritative; a
persistent disagreement is a suspected hook bug to report, not to paper over.

stdlib only; python3 >= 3.9. Run: python3 -m unittest discover <this dir> -v
"""
import os
import random
import tempfile
import unittest

from helpers import (
    HookTestCase, run_hook,
    agent_payload, task_payload, workflow_payload, workflow_script,
)


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
        self.assert_deny(
            agent_payload({"subagent_type": "general-purpose",
                           "prompt": "reply done"}),
            reason_substring="model")

    def test_valid_models_abstain(self):
        for model in self.VALID:
            with self.subTest(model=model):
                self.assert_abstain(agent_payload({"model": model,
                                                   "prompt": "x"}))

    def test_blank_model_denies(self):
        for blank in ("", "   ", "\t", "\n"):
            with self.subTest(blank=repr(blank)):
                self.assert_deny(agent_payload({"model": blank, "prompt": "x"}),
                                 reason_substring="model")

    def test_banned_models_deny(self):
        for model in self.BANNED:
            with self.subTest(model=model):
                self.assert_deny(agent_payload({"model": model, "prompt": "x"}),
                                 reason_substring="banned")

    def test_banned_is_case_insensitive(self):
        for model in ("FABLE", "Fable", "INHERIT", "InHeRiT", " fable "):
            with self.subTest(model=model):
                self.assert_deny(agent_payload({"model": model, "prompt": "x"}),
                                 reason_substring="banned")

    def test_non_string_model_denies(self):
        # A non-str model is not an explicit textual choice: it fails the
        # isinstance(str) gate and falls through to the omitted-model deny.
        for model in (123, 1.5, True, None, {"name": "opus"}, ["opus"]):
            with self.subTest(model=model):
                self.assert_deny(agent_payload({"model": model, "prompt": "x"}),
                                 reason_substring="model")

    def test_task_alias_matches_agent(self):
        # Task is a pure alias of Agent: same deny on missing, same abstain on a
        # valid model.
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
        with open(os.path.join(agents_dir, type_name + ".md"), "w") as f:
            f.write("---\nname: %s\nmodel: %s\ndescription: t\n---\nBody.\n"
                    % (type_name, model))

    def test_pin_allows_omitted_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_pin(tmp, "reviewer", "sonnet")
            self.assert_abstain(agent_payload({"subagent_type": "reviewer"},
                                              cwd=tmp))

    def test_pin_found_walking_up_from_subdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_pin(tmp, "reviewer", "opus")
            deep = os.path.join(tmp, "a", "b", "c")
            os.makedirs(deep, exist_ok=True)
            self.assert_abstain(agent_payload({"subagent_type": "reviewer"},
                                              cwd=deep))

    def test_pin_found_via_home_fallback(self):
        # No ancestor of cwd holds the pin; only ~/.claude/agents does. The hook
        # resolves it via the HOME fallback appended after the walk-up.
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as work:
            self._write_pin(home, "hp_home_only_pin_zzz", "haiku")
            self.assert_abstain(
                agent_payload({"subagent_type": "hp_home_only_pin_zzz"},
                              cwd=work),
                env={"HOME": home})

    def test_banned_pin_denies(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_pin(tmp, "reviewer", "fable")
            self.assert_deny(agent_payload({"subagent_type": "reviewer"},
                                           cwd=tmp),
                             reason_substring="banned")

    def test_unknown_type_denies(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assert_deny(
                agent_payload({"subagent_type": "does-not-exist"}, cwd=tmp),
                reason_substring="model")

    def test_traversal_type_rejected(self):
        # A subagent_type carrying a path separator (or . / ..) is refused before
        # any file read, so a pin cannot be smuggled from outside the agents dir.
        with tempfile.TemporaryDirectory() as tmp:
            # Plant a real pin one level up to prove it is NOT reachable.
            self._write_pin(os.path.dirname(tmp), "reviewer", "opus")
            for bad in ("../reviewer", "..\\reviewer", "a/b", ".", "..",
                        "sub/../reviewer"):
                with self.subTest(type=bad):
                    self.assert_deny(
                        agent_payload({"subagent_type": bad}, cwd=tmp),
                        reason_substring="model")

    def test_explicit_call_model_checked_before_pin(self):
        # A valid pin does NOT rescue an explicitly banned call-site model.
        with tempfile.TemporaryDirectory() as tmp:
            self._write_pin(tmp, "reviewer", "opus")
            self.assert_deny(
                agent_payload({"subagent_type": "reviewer", "model": "fable"},
                              cwd=tmp),
                reason_substring="banned")


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
        self.assert_ask(workflow_payload({"name": "deep-research"}),
                        reason_substring="statically")

    def test_resume_only_asks(self):
        self.assert_ask(workflow_payload({"resumeFromRunId": "run_123"}),
                        reason_substring="statically")

    def test_strict_flag_denies_saved_workflow(self):
        # _env_flag accepts any of these truthy spellings -> strict deny; any
        # falsey / unset spelling leaves the default ask.
        for truthy in ("1", "true", "TRUE", "yes", "on", " On "):
            with self.subTest(flag=repr(truthy)):
                self.assert_deny(
                    workflow_payload({"name": "deep-research"}),
                    env={"MODEL_GUARD_STRICT_SAVED_WORKFLOWS": truthy})
        for falsey in ("0", "false", "no", "off", "", "  ", "banana"):
            with self.subTest(flag=repr(falsey)):
                self.assert_ask(
                    workflow_payload({"resumeFromRunId": "r1"}),
                    env={"MODEL_GUARD_STRICT_SAVED_WORKFLOWS": falsey})

    def test_no_verifiable_field_denies(self):
        self.assert_deny(workflow_payload({"args": {"foo": 1}}))

    def test_empty_tool_input_denies(self):
        # {} has no script / scriptPath / name / resume -> nothing to verify.
        self.assert_deny(workflow_payload({}))

    def test_scriptpath_linted_and_denied_when_bad(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "wf.js"), "w") as f:
                f.write("agent('x', { temperature: 1 });")
            self.assert_deny(workflow_payload({"scriptPath": "wf.js"}, cwd=tmp),
                             reason_substring="model")

    def test_scriptpath_clean_abstains(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "wf.js"), "w") as f:
                f.write("agent('x', { model: 'opus' });")
            self.assert_abstain(
                workflow_payload({"scriptPath": "wf.js"}, cwd=tmp))

    def test_scriptpath_unreadable_denies(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assert_deny(
                workflow_payload({"scriptPath": "missing.js"}, cwd=tmp))

    def test_inline_script_clean_abstains(self):
        self.wf_abstain("agent('do a', { model: 'opus' });")

    def test_empty_script_abstains(self):
        self.wf_abstain("")


# =========================================================================
# ECMAScript lexer conformance
# =========================================================================
class LexerConformanceTests(HookTestCase):
    """The lexer must tokenise ES source the way acorn/esprima do, because the
    lint runs on the token stream. These cases exercise each lexical construct
    where a mis-lex would either hide a model-less spawn (fatal) or swallow a
    correctly-modeled one (over-deny)."""

    # ---- strings / templates are opaque single tokens --------------------
    def test_model_mention_in_string_prompt_is_not_a_key(self):
        # A `model:` substring living inside the prompt STRING is token content,
        # never a top-level opts key. All three quote forms must still deny.
        self.wf_deny("agent('please set model: opus here', { other: 1 });",
                     reason_substring="model")
        self.wf_deny('agent("model: opus in text", { other: 1 });',
                     reason_substring="model")
        self.wf_deny("agent(`model: opus templated`, { other: 1 });",
                     reason_substring="model")

    def test_string_escapes_do_not_terminate_early(self):
        # An escaped quote inside a string does not close it; the opts object and
        # its real model key are still reached.
        self.wf_abstain(r"agent('a\'b\'c', { model: 'opus' });")
        self.wf_abstain(r'agent("x\"y", { model: "sonnet" });')

    def test_template_with_nested_interpolation_parsed(self):
        # A template prompt whose ${...} nests quotes, arrays, braces and a call
        # lexes cleanly; the top-level model on the opts object -> allow.
        script = ("agent(`process "
                  "${JSON.stringify({a: \"x\", b: [1, 2], c: fn(3, 4)})} now`, "
                  "{ model: 'sonnet' });")
        self.wf_abstain(script)

    # ---- comments --------------------------------------------------------
    def test_block_comment_model_is_not_a_key(self):
        self.wf_deny("agent('x', { /* model: 'opus' */ other: 1 });",
                     reason_substring="model")

    def test_line_comment_model_is_not_a_key(self):
        self.wf_deny("agent('x', {\n  // model: 'opus'\n  other: 1\n});",
                     reason_substring="model")

    def test_comment_does_not_break_a_real_model(self):
        self.wf_abstain("agent('x', {\n  // pick carefully\n  model: 'haiku'\n});")

    # ---- regex in EXPRESSION position (value) ----------------------------
    def test_regex_value_is_not_a_model_key(self):
        # /model:/ is a regex value, not a `model` key -> deny.
        self.wf_deny("agent('x', { validate: /model:/ });",
                     reason_substring="model")

    def test_agent_shape_inside_genuine_regex_is_opaque(self):
        # An agent()-shaped substring inside a real regex literal is regex
        # content -- one opaque token -- so the only executing spawn (modeled)
        # allows. Covers value-position regex, object-value regex, flagged regex.
        for regex_pos in ("f(1, /agent(x)/);",
                          "const o = { v: /agent(x)/ };",
                          "const re = /agent(y)/g;",
                          "const re = /x/gimsuy;"):
            with self.subTest(regex_pos=regex_pos):
                self.wf_abstain("%s agent('p', { model: 'opus' });" % regex_pos)

    def test_regex_char_class_with_quote_is_opaque(self):
        # A quote inside a [ ] character class stays inside the regex token and
        # must not trip string/ambiguity handling; the real model key allows.
        self.wf_abstain("agent('x', { validate: /[a-z'\"]/, model: 'opus' });")

    # ---- '/' in DIVISION position (does not over-deny a modeled call) ----
    def test_plain_division_before_modeled_call_abstains(self):
        self.wf_abstain("const r = a / b; agent('x', { model: 'opus' });")

    def test_balanced_quote_arithmetic_abstains(self):
        # Two divisions sandwiching a balanced string stay local (the string
        # cannot swallow the call) -> not flagged.
        self.wf_abstain(
            "const r = a / b + \"note\" + c / d; "
            "agent('x', { model: 'opus' });")

    def test_division_by_modeled_call_result_abstains(self):
        # `x / agent(...)`: the slash is division, the call still lints (modeled).
        self.wf_abstain("const r = total / agent('y', { model: 'sonnet' });")

    def test_postfix_increment_then_slash_is_division(self):
        # `i++ / agent(...)`: '++' ends an expression so '/' is division, leaving
        # the following spawn a visible token. Model-less -> deny; modeled -> ok.
        self.wf_deny("let i = 0; i++ / agent('x', { t: 1 });",
                     reason_substring="model")
        self.wf_abstain("let i = 0; i++ / agent('x', { model: 'opus' });")

    def test_regex_after_keyword_terminates_cleanly(self):
        # After a beforeExpr keyword a '/' opens a regex; the regex must
        # terminate at its own closing '/' and not swallow the following spawn.
        self.wf_deny("x = typeof /it's/; agent('y', { t: 1 });",
                     reason_substring="model")
        self.wf_deny("return /it's/; agent('x', { t: 1 });",
                     reason_substring="model")

    def test_keyword_regex_with_quote_does_not_over_deny(self):
        # A legit quote-bearing regex after a keyword is skipped as a regex,
        # leaving a properly-modeled call allowed.
        self.wf_abstain(
            "return /it's a test/.test(x); agent('y', { model: 'opus' });")

    def test_regex_escaped_slashes_not_a_line_comment(self):
        # '//' produced by escaped slashes inside a regex is regex content, not a
        # line comment that would swallow the rest of the line (and its spawn).
        self.wf_deny("x = /a\\/\\//; agent('m', { t: 1 });",
                     reason_substring="model")

    # ---- member-access exclusion (obj.agent is not the DSL agent) --------
    def test_member_call_not_matched(self):
        self.wf_abstain("obj.agent('x');")

    def test_similar_identifier_not_matched(self):
        self.wf_abstain("superagent('x');")


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
        for key in ("model", "'model'", '"model"'):
            with self.subTest(key=key):
                self.wf_abstain("agent('x', { %s: 'sonnet' });" % key)

    def test_nested_model_key_not_counted(self):
        # A `model` nested inside a sub-object (a schema) is not the top-level
        # opts key -> deny.
        self.wf_deny(
            "agent('x', { schema: { type: 'object', "
            "properties: { model: { type: 'string' } } } });",
            reason_substring="model")

    def test_ternary_value_position_model_not_a_key(self):
        # A `model` token sitting in a ternary VALUE position (before the
        # ternary's own colon, or in a branch) is not a key. Bareword, double-
        # and single-quoted, and multiline forms all deny.
        for opts in ("{ x: cond ? model : other }",
                     "{ onErr: flag ? \"model\" : \"skip\" }",
                     "{ x: c ? 'model' : y }",
                     "{ k: cond\n ? model\n : fallback }"):
            with self.subTest(opts=opts):
                self.wf_deny("agent('do', %s);" % opts, reason_substring="model")

    def test_real_key_counts_despite_ternary_value_model(self):
        # A genuine top-level model key still satisfies even alongside a ternary
        # value-position `model` token elsewhere in the same object.
        self.wf_abstain(
            "agent('do', { x: cond ? model : other, model: 'opus' });")

    def test_key_after_call_value_comma_counts(self):
        # model key in key position after a comma that follows a call value.
        self.wf_abstain("agent('do', { a: fn(1, 2), model: 'sonnet' });")

    # ---- value classification --------------------------------------------
    def test_valid_literal_values_abstain(self):
        for mv in ("haiku", "sonnet", "opus"):
            with self.subTest(model=mv):
                self.wf_abstain("agent('x', { model: '%s' });" % mv)

    def test_dynamic_value_abstains(self):
        # A non-literal (identifier) model value cannot be statically resolved;
        # it is treated as satisfying presence, not denied (documented).
        self.wf_abstain("agent('x', { model: chosenModel });")

    def test_blank_literal_values_deny(self):
        # Raw inner text (no unescaping): only truly empty/whitespace literals
        # are blank -- "\t" here is the two literal chars backslash-t (non-blank).
        for opts in ("{ model: '' }", '{ model: "" }', "{ model: '  ' }",
                     "{ model: `` }", "{ model: `   ` }"):
            with self.subTest(opts=opts):
                self.wf_deny("agent('x', %s);" % opts, reason_substring="blank")

    def test_banned_literal_values_deny(self):
        # Banned across quote forms and case -- matches the Agent-side ban.
        for opts in ("{ model: 'fable' }", "{ model: 'inherit' }",
                     "{ model: 'FABLE' }", '{ model: "fable" }',
                     "{ model: `fable` }", "{ \"model\": 'inherit' }"):
            with self.subTest(opts=opts):
                self.wf_deny("agent('x', %s);" % opts, reason_substring="banned")

    # ---- no options object -----------------------------------------------
    def test_missing_model_key_denies(self):
        self.wf_deny("agent('do a', { temperature: 1 });",
                     reason_substring="model")

    def test_no_second_arg_denies(self):
        self.wf_deny("agent('just a prompt');", reason_substring="model")

    def test_empty_call_denies(self):
        self.wf_deny("agent();", reason_substring="model")

    def test_non_object_opts_deny(self):
        # A spread or a bare identifier is not an inspectable object literal.
        self.wf_deny("agent('x', { ...base });", reason_substring="model")
        self.wf_deny("agent('x', opts);", reason_substring="model")

    # ---- nested workflow() -----------------------------------------------
    def test_nested_workflow_call_denies(self):
        self.assert_deny(
            workflow_script("agent('a', { model: 'opus' }); workflow('other');"),
            reason_substring="workflow")

    # ---- pipeline() is a plain call, not a spawn -------------------------
    def test_pipeline_wrapper_not_flagged(self):
        # pipeline() itself needs no model; its agent() children are each linted.
        self.wf_abstain(
            "pipeline(\n  agent('a', { model: 'haiku' }),\n"
            "  agent('b', { model: 'sonnet' }),\n);")
        self.wf_deny(
            "pipeline(\n  agent('a', { model: 'haiku' }),\n"
            "  agent('b', { temperature: 1 }),\n);",
            reason_substring="model")

    # ---- calls inside template interpolations ----------------------------
    def test_call_inside_interpolation_is_linted(self):
        # A model-less agent() inside a ${...} is still linted; a `model:` living
        # in the interpolation's own code text is not the outer opts key.
        self.wf_deny("agent(`x ${a(\"model: y\")}`, { other: 1 });",
                     reason_substring="model")

    # ---- multiple calls: the offender is named ---------------------------
    def test_multi_call_names_missing_offender(self):
        self.wf_deny(
            "agent('good', { model: 'opus' });\n"
            "agent('the-bad-one', { temperature: 1 });\n",
            reason_substring="the-bad-one")

    def test_multi_call_names_banned_offender(self):
        self.wf_deny(
            "agent('good', { model: 'opus' });\n"
            "agent('the-bad-one', { model: 'fable' });\n",
            reason_substring="the-bad-one")


# =========================================================================
# slash-classification regression family (member access + contextual kw)
# =========================================================================
class SlashClassificationRegressionTests(HookTestCase):
    """Regressions for the token-lexer slash-classification holes: a keyword used
    as a member/property, a bare contextual keyword, a member method whose name
    is a control keyword, a block-closing brace, and a unicode-escaped call name
    must never let a model-less agent() spawn be swallowed as a regex literal and
    silently allowed. (Docstrings below name each specific hole, carried forward
    verbatim from the regression set they were added with.)"""

    # --- Finding 1: a regex-prefix keyword used as a PROPERTY (obj.do,
    # gen.return, x.in, ...) is a value, so the following '/' is division and the
    # agent() between the slashes is real code, not regex content -> must deny.
    def test_member_keyword_property_slash_is_division_not_regex(self):
        for member in ("obj.do", "gen.return", "arr.of", "x.in", "gen.throw",
                       "o.yield", "o.await", "o.case", "o.delete"):
            script = "%s /agent('m',{t:1})/ ;" % member
            with self.subTest(member=member):
                self.assert_deny(workflow_script(script),
                                 msg="%s must expose the spawn" % member)

    def test_member_keyword_division_does_not_over_deny_modeled(self):
        # the same member-property division around a *modeled* call must allow.
        self.wf_abstain(
            "let obj={do:6}; let z = obj.do / agent('m',{ model: 'opus' }) / 2;")

    # --- Finding 2/5: a member method whose name is a control keyword
    # (p.catch(), o.for(), p?.catch(), o.switch()) closes a CALL, so its ')' is a
    # value and the following '/' is division -> the later agent() is exposed.
    def test_member_control_method_slash_is_division(self):
        for pre in ("x.catch(fn)", "o.for(x)", "p?.catch(fn)", "o.switch(x)"):
            script = "%s / a; agent('a', {t:1}) / b;" % pre
            with self.subTest(pre=pre):
                self.assert_deny(workflow_script(script),
                                 msg="%s must expose the spawn" % pre)

    def test_member_control_method_division_does_not_over_deny(self):
        # ordinary `.catch()` division around a modeled call is common real code.
        self.wf_abstain(
            "const r = p.catch(fn) / total; agent('a', { model: 'opus' });")

    # --- Finding 3: the contextual keywords of / yield / await can be
    # identifiers (sloppy binding) or properties that END an expression, so a
    # following '/' is division and swallows nothing -> the spawn must deny.
    def test_contextual_keyword_identifier_slash_is_division(self):
        for kw in ("of", "await", "yield"):
            script = "var %s = 1;\n%s / agent('t', {}) / 2;" % (kw, kw)
            with self.subTest(kw=kw):
                self.assert_deny(workflow_script(script),
                                 msg="%s identifier must expose spawn" % kw)

    def test_contextual_keyword_property_slash_is_division(self):
        self.wf_deny("let obj={of:1}; obj.of / agent('t', {}) / 2;")

    # --- Finding 4: a unicode-escaped call identifier (agent -> agent)
    # cannot be lexed as the name "agent"/"workflow"; the stray backslash is a
    # structural anomaly and must fail closed rather than silently allow.
    def test_unicode_escaped_agent_identifier_deny(self):
        self.wf_deny("\\u0061gent('t', {})")

    def test_unicode_escaped_workflow_identifier_deny(self):
        self.wf_deny("\\u0077orkflow('t', {})")

    def test_unicode_escaped_identifier_in_interpolation_deny(self):
        self.wf_deny("`${ \\u0061gent('t', {}) }`")

    # --- Finding 6: a '/' after a block-closing '}' is division (the fatal-safe
    # reading); a model-less agent() following it stays a visible token.
    def test_block_close_slash_is_division(self):
        self.wf_deny("function f(){} / a; agent('a', {t:1}) / b;")

    # --- Preservation: an agent()-shaped substring INSIDE a genuine regex
    # literal (unambiguous regex position) is regex content, never executes, and
    # must not deny a script whose only real spawn is properly modeled.
    def test_agent_shape_inside_genuine_regex_allows_modeled_spawn(self):
        for regex_pos in ("f(1, /agent(x)/);",
                          "const o = { v: /agent(x)/ };",
                          "const re = /agent(y)/g;"):
            script = "%s agent('p', { model: 'opus' });" % regex_pos
            with self.subTest(regex_pos=regex_pos):
                self.assert_abstain(workflow_script(script),
                                    msg="%s must allow" % regex_pos)


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
        self.wf_deny("agent('unterminated, { model: 'opus' })")

    def test_unterminated_template_denies(self):
        self.wf_deny("agent(`unterminated, { model: 'opus' })")

    def test_unterminated_block_comment_denies(self):
        self.wf_deny("agent('x', { model: 'opus' }); /* never closed")

    def test_unterminated_regex_denies(self):
        # A regex opens in expression position and never closes on its line.
        self.wf_deny("const re = /unterminated\n; agent('x', {});")

    def test_unbalanced_parens_denies(self):
        self.wf_deny("agent('x', { model: 'opus' ")

    def test_unbalanced_braces_denies(self):
        self.wf_deny("agent('x', { model: 'opus' ) ;")

    def test_stray_backslash_denies(self):
        # A bare '\' outside any string/regex/template is an identifier-escape
        # smuggling attempt -> fail closed.
        self.wf_deny("\\u0061gent('t', {})")

    def test_division_ambiguity_quote_bearing_denies(self):
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
        # The guard must not fire on a balanced quote-bearing regex elsewhere:
        # the one real spawn is modeled -> allow.
        self.wf_abstain("a.split(/'/); agent('do', { model: 'opus' });")


# =========================================================================
# fail-safe abstains (never wedge a session)
# =========================================================================
class FailSafeAbstainTests(HookTestCase):
    """The hook governs only Agent/Task/Workflow and must never wedge a session
    on malformed input: any other tool, unparseable stdin, non-object JSON, and
    a missing/null tool_input all leave normal permission flow untouched -- with
    the one exception that an Agent whose tool_input is absent still denies (an
    empty tool_input has no model)."""

    def test_other_tools_abstain(self):
        for payload in ({"tool_name": "Bash", "tool_input": {"command": "ls"}},
                        {"tool_name": "Read",
                         "tool_input": {"file_path": "/etc/hosts"}},
                        {"tool_name": "Edit", "tool_input": {}}):
            with self.subTest(tool=payload["tool_name"]):
                self.assert_abstain(payload)

    def test_garbage_stdin_exits_zero_silently(self):
        res = run_hook("this is not json {{{")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "")

    def test_empty_stdin_exits_zero(self):
        res = run_hook("")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "")

    def test_non_object_json_abstains(self):
        for payload in ("[1, 2, 3]", "42", '"a string"', "null", "true"):
            with self.subTest(payload=payload):
                res = run_hook(payload)
                self.assertEqual(res.returncode, 0)
                self.assertEqual(res.stdout.strip(), "")

    def test_missing_tool_input_on_agent_denies(self):
        # tool_input absent -> defaults to {} -> model omitted -> deny.
        self.assert_deny({"tool_name": "Agent"}, reason_substring="model")

    def test_null_tool_input_on_agent_denies(self):
        # A null (non-dict) tool_input is coerced to {} -> omitted model -> deny.
        self.assert_deny({"tool_name": "Agent", "tool_input": None},
                         reason_substring="model")

    def test_null_tool_input_on_other_tool_abstains(self):
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
    _WORDS = ["do", "run", "task", "step", "phase", "handle", "process",
              "check", "audit", "review", "plan", "build"]

    def _prompt(self, rng):
        n = rng.randint(1, 4)
        return " ".join(rng.choice(self._WORDS) for _ in range(n))

    def _call(self, rng, modeled):
        q = rng.choice(["'", '"'])
        pstr = q + self._prompt(rng) + q          # prompt carries no quotes
        extras = []
        if rng.random() < 0.5:
            extras.append("temperature: %d" % rng.randint(0, 2))
        if rng.random() < 0.5:
            extras.append("timeout: %d" % rng.randint(1, 99))
        if rng.random() < 0.4:
            extras.append("tools: [%d, %d]" % (rng.randint(0, 9),
                                               rng.randint(0, 9)))
        if modeled:
            model = rng.choice(["haiku", "sonnet", "opus"])
            mq = rng.choice(["'", '"', "`"])
            keyfmt = rng.choice(["model", "'model'", '"model"'])
            parts = extras + ["%s: %s%s%s" % (keyfmt, mq, model, mq)]
            rng.shuffle(parts)
            return "agent(%s, { %s })" % (pstr, ", ".join(parts))
        shape = rng.choice(["opts", "opts", "noarg", "empty"])
        if shape == "opts":
            if not extras:
                extras = ["temperature: 1"]
            return "agent(%s, { %s })" % (pstr, ", ".join(extras))
        if shape == "noarg":
            return "agent(%s)" % pstr
        return "agent()"

    def _script(self, rng, modeled):
        n = rng.randint(1, 3)
        calls = [self._call(rng, modeled) for _ in range(n)]
        if n > 1 and rng.random() < 0.5:
            return "pipeline(\n  " + ",\n  ".join(calls) + "\n);"
        return "".join(c + ";\n" for c in calls)

    def test_modelless_variants_always_deny(self):
        rng = random.Random(self.SEED)
        for i in range(self.N):
            script = self._script(rng, modeled=False)
            with self.subTest(i=i, script=script):
                res = run_hook(workflow_script(script))
                self.assertEqual(res.decision, "deny",
                                 "model-less script must deny:\n%s\n(reason=%r)"
                                 % (script, res.reason))

    def test_modeled_variants_always_abstain(self):
        rng = random.Random(self.SEED ^ 0x9999)
        for i in range(self.N):
            script = self._script(rng, modeled=True)
            with self.subTest(i=i, script=script):
                res = run_hook(workflow_script(script))
                self.assertEqual(res.decision, "abstain",
                                 "fully-modeled script must abstain:\n%s\n"
                                 "(reason=%r)" % (script, res.reason))


if __name__ == "__main__":
    unittest.main()
