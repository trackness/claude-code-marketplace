#!/usr/bin/env python3
"""Test suite for enforce_explicit_model.py.

Drives the hook as a subprocess with synthetic PreToolUse payloads and asserts
the permission decision. A clean/allowed call abstains (no stdout); a blocked
call emits hookSpecificOutput.permissionDecision of deny/ask.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "enforce_explicit_model.py")


def run_hook(payload_text, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, SCRIPT],
        input=payload_text,
        capture_output=True,
        text=True,
        env=full_env,
    )


def decide(payload, env=None):
    """Return (decision, reason). decision is 'abstain' when nothing is emitted,
    else the permissionDecision string ('deny' / 'ask' / 'allow')."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    p = run_hook(text, env=env)
    assert p.returncode == 0, "hook must always exit 0, got %r (stderr=%r)" % (
        p.returncode, p.stderr)
    out = p.stdout.strip()
    if not out:
        return ("abstain", "")
    data = json.loads(out)
    hso = data["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    return (hso["permissionDecision"], hso.get("permissionDecisionReason", ""))


def agent_payload(tool_input, cwd=None, tool_name="Agent"):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": cwd or os.getcwd(),
    }


def workflow_payload(tool_input, cwd=None):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Workflow",
        "tool_input": tool_input,
        "cwd": cwd or os.getcwd(),
    }


class AgentTests(unittest.TestCase):
    def test_no_model_deny(self):
        d, r = decide(agent_payload({"subagent_type": "general-purpose",
                                     "prompt": "reply done"}))
        self.assertEqual(d, "deny")
        self.assertIn("model", r)

    def test_haiku_allow(self):
        d, _ = decide(agent_payload({"model": "haiku", "prompt": "x"}))
        self.assertEqual(d, "abstain")

    def test_sonnet_allow(self):
        d, _ = decide(agent_payload({"model": "sonnet", "prompt": "x"}))
        self.assertEqual(d, "abstain")

    def test_opus_allow(self):
        d, _ = decide(agent_payload({"model": "opus", "prompt": "x"}))
        self.assertEqual(d, "abstain")

    def test_fable_deny(self):
        d, r = decide(agent_payload({"model": "fable", "prompt": "x"}))
        self.assertEqual(d, "deny")
        self.assertIn("banned", r.lower())

    def test_inherit_deny(self):
        d, _ = decide(agent_payload({"model": "inherit", "prompt": "x"}))
        self.assertEqual(d, "deny")

    def test_blank_model_deny(self):
        d, _ = decide(agent_payload({"model": "   ", "prompt": "x"}))
        self.assertEqual(d, "deny")

    def test_model_case_insensitive_ban(self):
        d, _ = decide(agent_payload({"model": "FABLE", "prompt": "x"}))
        self.assertEqual(d, "deny")

    def test_task_alias_no_model_deny(self):
        d, _ = decide(agent_payload({"prompt": "x"}, tool_name="Task"))
        self.assertEqual(d, "deny")

    def test_task_alias_valid_allow(self):
        d, _ = decide(agent_payload({"model": "opus"}, tool_name="Task"))
        self.assertEqual(d, "abstain")


class FrontmatterPinTests(unittest.TestCase):
    def _make_agent_file(self, base_dir, type_name, model):
        agents_dir = os.path.join(base_dir, ".claude", "agents")
        os.makedirs(agents_dir, exist_ok=True)
        with open(os.path.join(agents_dir, type_name + ".md"), "w") as f:
            f.write("---\nname: %s\nmodel: %s\ndescription: t\n---\nBody.\n"
                    % (type_name, model))

    def test_frontmatter_pin_allows_omitted_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_agent_file(tmp, "reviewer", "sonnet")
            d, _ = decide(agent_payload({"subagent_type": "reviewer"}, cwd=tmp))
            self.assertEqual(d, "abstain")

    def test_frontmatter_banned_pin_denies(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_agent_file(tmp, "reviewer", "fable")
            d, r = decide(agent_payload({"subagent_type": "reviewer"}, cwd=tmp))
            self.assertEqual(d, "deny")
            self.assertIn("banned", r.lower())

    def test_frontmatter_pin_found_walking_up_from_subdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_agent_file(tmp, "reviewer", "opus")
            deep = os.path.join(tmp, "a", "b", "c")
            os.makedirs(deep, exist_ok=True)
            d, _ = decide(agent_payload({"subagent_type": "reviewer"}, cwd=deep))
            self.assertEqual(d, "abstain")

    def test_unknown_subagent_type_still_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, _ = decide(agent_payload({"subagent_type": "does-not-exist"},
                                        cwd=tmp))
            self.assertEqual(d, "deny")

    def test_explicit_model_beats_frontmatter_lookup(self):
        # explicit banned model denies even if a valid pin exists
        with tempfile.TemporaryDirectory() as tmp:
            self._make_agent_file(tmp, "reviewer", "opus")
            d, _ = decide(agent_payload({"subagent_type": "reviewer",
                                         "model": "fable"}, cwd=tmp))
            self.assertEqual(d, "deny")


class WorkflowTests(unittest.TestCase):
    def wf(self, script):
        return decide(workflow_payload({"script": script}))

    def test_clean_inline_allow(self):
        d, _ = self.wf("agent('do a', { model: 'opus' });")
        self.assertEqual(d, "abstain")

    def test_missing_model_deny(self):
        d, r = self.wf("agent('do a', { temperature: 1 });")
        self.assertEqual(d, "deny")
        self.assertIn("model", r)

    def test_no_second_arg_deny(self):
        d, _ = self.wf("agent('just a prompt');")
        self.assertEqual(d, "deny")

    def test_empty_call_deny(self):
        d, _ = self.wf("agent();")
        self.assertEqual(d, "deny")

    def test_one_good_one_bad_deny_names_bad(self):
        script = ("agent('good', { model: 'opus' });\n"
                  "agent('the-bad-one', { temperature: 1 });\n")
        d, r = self.wf(script)
        self.assertEqual(d, "deny")
        self.assertIn("the-bad-one", r)

    def test_model_only_in_single_quote_prompt_deny(self):
        d, _ = self.wf("agent('please set model: opus here', { other: 1 });")
        self.assertEqual(d, "deny")

    def test_model_only_in_double_quote_prompt_deny(self):
        d, _ = self.wf('agent("model: opus in text", { other: 1 });')
        self.assertEqual(d, "deny")

    def test_model_only_in_backtick_prompt_deny(self):
        d, _ = self.wf("agent(`model: opus templated`, { other: 1 });")
        self.assertEqual(d, "deny")

    def test_interpolation_with_quotes_and_parens_parsed(self):
        # template prompt with nested ${...} containing quotes/parens/braces;
        # a valid top-level model on the opts object -> allow.
        script = ("agent(`process "
                  "${JSON.stringify({a: \"x\", b: [1, 2], c: fn(3, 4)})} now`, "
                  "{ model: 'sonnet' });")
        d, _ = self.wf(script)
        self.assertEqual(d, "abstain")

    def test_interpolation_hides_bad_but_still_no_model_deny(self):
        # interpolation is code; a model: inside the interpolated code text is
        # not a top-level opts key -> still deny when opts lacks model.
        script = "agent(`x ${a(\"model: y\")}`, { other: 1 });"
        d, _ = self.wf(script)
        self.assertEqual(d, "deny")

    def test_nested_schema_model_deny(self):
        script = ("agent('x', { schema: { type: 'object', "
                  "properties: { model: { type: 'string' } } } });")
        d, _ = self.wf(script)
        self.assertEqual(d, "deny")

    def test_block_comment_model_deny(self):
        d, _ = self.wf("agent('x', { /* model: 'opus' */ other: 1 });")
        self.assertEqual(d, "deny")

    def test_line_comment_model_deny(self):
        d, _ = self.wf("agent('x', {\n  // model: 'opus'\n  other: 1\n});")
        self.assertEqual(d, "deny")

    def test_comment_does_not_break_valid_model(self):
        d, _ = self.wf("agent('x', {\n  // pick carefully\n  model: 'haiku'\n});")
        self.assertEqual(d, "abstain")

    def test_regex_literal_no_silent_allow(self):
        # /model:/ is a regex value, not a model key -> must NOT allow.
        d, _ = self.wf("agent('x', { validate: /model:/ });")
        self.assertNotEqual(d, "abstain")
        self.assertEqual(d, "deny")

    def test_quoted_model_key_allow(self):
        d, _ = self.wf("agent('x', { \"model\": 'sonnet' });")
        self.assertEqual(d, "abstain")

    def test_single_quoted_model_key_allow(self):
        d, _ = self.wf("agent('x', { 'model': 'haiku' });")
        self.assertEqual(d, "abstain")

    def test_spread_opts_deny(self):
        d, _ = self.wf("agent('x', { ...base });")
        self.assertEqual(d, "deny")

    def test_dynamic_opts_identifier_deny(self):
        d, _ = self.wf("agent('x', opts);")
        self.assertEqual(d, "deny")

    def test_pipeline_all_modeled_allow(self):
        script = ("pipeline(\n"
                  "  agent('a', { model: 'haiku' }),\n"
                  "  agent('b', { model: 'sonnet' }),\n"
                  ");")
        d, _ = self.wf(script)
        self.assertEqual(d, "abstain")

    def test_pipeline_one_bad_deny(self):
        script = ("pipeline(\n"
                  "  agent('a', { model: 'haiku' }),\n"
                  "  agent('b', { temperature: 1 }),\n"
                  ");")
        d, _ = self.wf(script)
        self.assertEqual(d, "deny")

    def test_nested_workflow_deny(self):
        d, r = self.wf("agent('a', { model: 'opus' }); workflow('other');")
        self.assertEqual(d, "deny")
        self.assertIn("workflow", r.lower())

    def test_unbalanced_parens_deny(self):
        d, _ = self.wf("agent('x', { model: 'opus' ")
        self.assertEqual(d, "deny")

    def test_unterminated_string_deny(self):
        d, _ = self.wf("agent('unterminated, { model: 'opus' })")
        self.assertEqual(d, "deny")

    def test_agent_method_call_not_matched(self):
        # obj.agent(...) is a method call, not the workflow DSL agent().
        d, _ = self.wf("obj.agent('x');")
        self.assertEqual(d, "abstain")

    def test_similar_identifier_not_matched(self):
        d, _ = self.wf("superagent('x');")
        self.assertEqual(d, "abstain")

    def test_empty_script_allow(self):
        d, _ = self.wf("")
        self.assertEqual(d, "abstain")

    def test_ternary_value_bareword_model_not_a_key_deny(self):
        # `cond ? model : other` puts the bareword `model` right before the
        # ternary's own colon; it is a VALUE, not a top-level opts key -> deny.
        d, _ = self.wf("agent('do', { x: cond ? model : other });")
        self.assertEqual(d, "deny")

    def test_ternary_value_quoted_model_not_a_key_deny(self):
        d, _ = self.wf("agent('do', { onErr: flag ? \"model\" : \"skip\" });")
        self.assertEqual(d, "deny")

    def test_ternary_value_single_quoted_model_not_a_key_deny(self):
        d, _ = self.wf("agent('do', { x: c ? 'model' : y });")
        self.assertEqual(d, "deny")

    def test_ternary_value_multiline_model_not_a_key_deny(self):
        d, _ = self.wf("agent('do', { k: cond\n ? model\n : fallback });")
        self.assertEqual(d, "deny")

    def test_ternary_value_model_but_real_model_key_present_allow(self):
        # a genuine top-level model key still counts even alongside a ternary
        # value-position `model` token elsewhere in the object.
        d, _ = self.wf(
            "agent('do', { x: cond ? model : other, model: 'opus' });")
        self.assertEqual(d, "abstain")

    def test_model_key_after_call_value_comma_allow(self):
        # model key in key position after a comma following a call value.
        d, _ = self.wf("agent('do', { a: fn(1, 2), model: 'sonnet' });")
        self.assertEqual(d, "abstain")

    # --- Finding 1: regex literals containing a quote must not blank/hide a
    # real model-less agent() call across the intervening code (silent-allow).
    def test_regex_single_quote_around_call_no_silent_allow(self):
        d, r = self.wf("a.split(/'/); agent('do', {t:1}); b.split(/'/);")
        self.assertEqual(d, "deny")
        self.assertIn("model", r)

    def test_regex_double_quote_around_call_no_silent_allow(self):
        d, r = self.wf('r = /"/; agent("x", {t:1}); q = /"/;')
        self.assertEqual(d, "deny")
        self.assertIn("model", r)

    def test_regex_assignment_quote_around_call_no_silent_allow(self):
        d, _ = self.wf("r = /'/; agent('x', {t:1}); q = /'/;")
        self.assertEqual(d, "deny")

    def test_regex_quote_in_class_around_call_no_silent_allow(self):
        d, _ = self.wf("x = /a'b/; agent('m', {t:1}); y = /c'd/;")
        self.assertEqual(d, "deny")

    def test_regex_with_quote_does_not_hide_valid_model(self):
        # the fix must not over-deny: a quote-bearing regex elsewhere leaves a
        # properly-modeled agent() call still allowed.
        d, _ = self.wf("a.split(/'/); agent('do', { model: 'opus' });")
        self.assertEqual(d, "abstain")

    def test_regex_line_comment_marker_inside_is_not_a_comment(self):
        # // inside a regex literal must not be treated as a line comment that
        # swallows the rest of the line (which holds the model-less call).
        d, _ = self.wf("x = /a\\/\\//; agent('m', {t:1});")
        self.assertEqual(d, "deny")

    # --- Finding: a quote-bearing regex literal in KEYWORD position (return,
    # typeof, ...) or a division-context slash that also reads as a
    # quote-bearing regex must NOT be misread as division, which would pair the
    # inner quote across the code and blank a real model-less agent( token out
    # of the scan (silent-allow). These are the untested keyword/division holes.
    def test_regex_after_typeof_keyword_no_silent_allow(self):
        d, r = self.wf(
            "x = typeof /it's/; agent('y', { t: 1 }); z = typeof /don't/;")
        self.assertEqual(d, "deny")
        self.assertIn("model", r)

    def test_regex_after_return_keyword_no_silent_allow(self):
        d, r = self.wf("return /it's/; agent('x', {t:1}); return /don't/;")
        self.assertEqual(d, "deny")
        self.assertIn("model", r)

    def test_division_context_backtick_regex_no_silent_allow(self):
        d, _ = self.wf("y = z /`/; agent('m', {t:1}); w = q /`/;")
        self.assertEqual(d, "deny")

    def test_division_context_quote_regex_no_silent_allow(self):
        d, _ = self.wf("foo /a'/ ; agent('do', {t:1}) ; bar = z /b'/;")
        self.assertEqual(d, "deny")

    def test_keyword_regex_with_quote_does_not_over_deny_valid_model(self):
        # a legit regex containing a quote after a keyword must be skipped as a
        # regex, leaving a properly-modeled agent() call allowed.
        d, _ = self.wf(
            "return /it's a test/.test(x); agent('y', { model: 'opus' });")
        self.assertEqual(d, "abstain")

    def test_plain_division_before_call_does_not_over_deny(self):
        # ordinary arithmetic division must not be misread as a regex and deny a
        # properly-modeled call.
        d, _ = self.wf("const r = a / b; agent('x', { model: 'opus' });")
        self.assertEqual(d, "abstain")

    def test_balanced_quote_arithmetic_does_not_over_deny(self):
        # two divisions sandwiching a balanced string literal stay local (the
        # string cannot swallow the call) -> not flagged.
        d, _ = self.wf(
            "const r = a / b + \"note\" + c / d; "
            "agent('x', { model: 'opus' });")
        self.assertEqual(d, "abstain")

    def test_division_by_agent_call_result_allowed(self):
        # `x / agent(...)` divides by a real, properly-modeled call; the slash
        # is division and the call is still verified.
        d, _ = self.wf("const r = total / agent('y', { model: 'sonnet' });")
        self.assertEqual(d, "abstain")

    # --- Finding 2: a top-level model: key must have an ALLOWED value; a banned
    # or blank literal is a laundering path and must be denied, matching Agent.
    def test_workflow_model_fable_deny(self):
        d, r = self.wf("agent('x', { model: 'fable' });")
        self.assertEqual(d, "deny")
        self.assertIn("banned", r.lower())

    def test_workflow_model_inherit_deny(self):
        d, r = self.wf("agent('x', { model: 'inherit' });")
        self.assertEqual(d, "deny")
        self.assertIn("banned", r.lower())

    def test_workflow_model_blank_deny(self):
        d, _ = self.wf("agent('x', { model: '  ' });")
        self.assertEqual(d, "deny")

    def test_workflow_model_empty_string_deny(self):
        d, _ = self.wf("agent('x', { model: '' });")
        self.assertEqual(d, "deny")

    def test_workflow_model_fable_case_insensitive_deny(self):
        d, _ = self.wf("agent('x', { model: 'FABLE' });")
        self.assertEqual(d, "deny")

    def test_workflow_model_fable_double_quoted_deny(self):
        d, _ = self.wf('agent("x", { model: "fable" });')
        self.assertEqual(d, "deny")

    def test_workflow_model_fable_backtick_deny(self):
        d, _ = self.wf("agent('x', { model: `fable` });")
        self.assertEqual(d, "deny")

    def test_workflow_quoted_key_banned_value_deny(self):
        d, _ = self.wf("agent('x', { \"model\": 'inherit' });")
        self.assertEqual(d, "deny")

    def test_workflow_one_valid_one_banned_deny_names_banned(self):
        script = ("agent('good', { model: 'opus' });\n"
                  "agent('the-bad-one', { model: 'fable' });\n")
        d, r = self.wf(script)
        self.assertEqual(d, "deny")
        self.assertIn("the-bad-one", r)

    def test_workflow_valid_model_still_allows(self):
        # regression guard: valid literal values keep passing after value check.
        for mv in ("haiku", "sonnet", "opus"):
            d, _ = self.wf("agent('x', { model: '%s' });" % mv)
            self.assertEqual(d, "abstain", "%s should allow" % mv)

    def test_workflow_dynamic_model_value_allowed(self):
        # a non-literal (dynamic) model value cannot be statically resolved; it
        # is treated as satisfying presence, not denied (documented behavior).
        d, _ = self.wf("agent('x', { model: chosenModel });")
        self.assertEqual(d, "abstain")


class WorkflowShapeTests(unittest.TestCase):
    def test_name_only_ask(self):
        d, r = decide(workflow_payload({"name": "deep-research"}))
        self.assertEqual(d, "ask")
        self.assertIn("statically", r.lower())

    def test_resume_only_ask(self):
        d, _ = decide(workflow_payload({"resumeFromRunId": "run_123"}))
        self.assertEqual(d, "ask")

    def test_name_only_strict_deny(self):
        d, _ = decide(workflow_payload({"name": "deep-research"}),
                      env={"MODEL_GUARD_STRICT_SAVED_WORKFLOWS": "1"})
        self.assertEqual(d, "deny")

    def test_no_verifiable_field_deny(self):
        d, _ = decide(workflow_payload({"args": {"foo": 1}}))
        self.assertEqual(d, "deny")

    def test_scriptpath_read_and_linted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "wf.js")
            with open(path, "w") as f:
                f.write("agent('x', { temperature: 1 });")
            d, _ = decide(workflow_payload({"scriptPath": "wf.js"}, cwd=tmp))
            self.assertEqual(d, "deny")

    def test_scriptpath_clean_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "wf.js")
            with open(path, "w") as f:
                f.write("agent('x', { model: 'opus' });")
            d, _ = decide(workflow_payload({"scriptPath": "wf.js"}, cwd=tmp))
            self.assertEqual(d, "abstain")

    def test_scriptpath_unreadable_deny(self):
        with tempfile.TemporaryDirectory() as tmp:
            d, _ = decide(workflow_payload({"scriptPath": "missing.js"},
                                           cwd=tmp))
            self.assertEqual(d, "deny")


class OtherToolTests(unittest.TestCase):
    def test_bash_payload_abstains(self):
        d, _ = decide({"tool_name": "Bash",
                       "tool_input": {"command": "ls"}})
        self.assertEqual(d, "abstain")

    def test_read_payload_abstains(self):
        d, _ = decide({"tool_name": "Read",
                       "tool_input": {"file_path": "/etc/hosts"}})
        self.assertEqual(d, "abstain")

    def test_garbage_stdin_exits_zero(self):
        p = run_hook("this is not json {{{")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")

    def test_empty_stdin_exits_zero(self):
        p = run_hook("")
        self.assertEqual(p.returncode, 0)

    def test_non_object_json_abstains(self):
        p = run_hook("[1, 2, 3]")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")

    def test_missing_tool_input_abstains(self):
        d, _ = decide({"tool_name": "Agent"})
        # no model -> deny (tool_input defaults to empty, model omitted)
        self.assertEqual(d, "deny")


if __name__ == "__main__":
    unittest.main()
