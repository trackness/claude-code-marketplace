#!/usr/bin/env python3
# PreToolUse enforcement hook: every subagent / workflow spawn must name an
# explicit, allowed model. An omitted model silently inherits the parent
# session's model; a banned model (fable / inherit) defeats the "chosen for its
# task" rule. The hook denies via the PreToolUse permissionDecision schema so
# the block holds even under bypassPermissions, and the reason is fed back to
# the model so it retries with an explicit model in the same turn.
#
# Tools handled (matcher "Agent|Task|Workflow"):
#   Agent / Task : deny unless tool_input.model is an explicit non-banned model,
#                  or the subagent_type's agent .md frontmatter pins one.
#   Workflow     : statically lint the script text; every agent() call must pass
#                  a top-level model: key in its options object. Fail-closed:
#                  anything the lint cannot positively verify is denied (never
#                  silently allowed). Saved/bundled workflows invoked by name or
#                  resume id cannot be inspected -> "ask" (or deny under strict).
#
# stdlib only. Unparseable stdin abstains (exit 0) so a malformed payload never
# wedges a session.

import json
import os
import re
import sys

# ---- config -------------------------------------------------------------
BANNED_MODELS = {"fable", "inherit"}
VALID_CHOICES = ["haiku", "sonnet", "opus"]
VALID_CHOICES_STR = ", ".join(VALID_CHOICES)
ALLOW_FRONTMATTER_PIN = True


def _env_flag(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Default False per plan; env override exists so the strict path is exercisable.
STRICT_SAVED_WORKFLOWS = _env_flag("MODEL_GUARD_STRICT_SAVED_WORKFLOWS", False)
# -------------------------------------------------------------------------


class LintError(Exception):
    """Raised on any structural anomaly (unterminated string/template/comment,
    unbalanced parens/braces). Every LintError maps to a deny (fail-closed)."""


# ---- output helpers -----------------------------------------------------
def _emit(decision, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))
    sys.exit(0)


def deny(reason):
    _emit("deny", reason)


def ask(reason):
    _emit("ask", reason)


def abstain():
    # Emit nothing -> hook abstains, normal permission flow proceeds.
    sys.exit(0)


# ---- string / template scanning primitives ------------------------------
def _wordchar(c):
    return c.isalnum() or c in "_$"


def _skip_quoted(s, i):
    """s[i] is ' or ". Return index just past the closing quote.
    Raises LintError on an unterminated string (raw newline or EOF)."""
    q = s[i]
    n = len(s)
    i += 1
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == q:
            return i + 1
        if c == "\n":
            raise LintError("unterminated string literal")
        i += 1
    raise LintError("unterminated string literal")


def _skip_template(s, i):
    """s[i] is a backtick. Return index just past the closing backtick,
    recursing through ${ ... } interpolations (which are code and may contain
    quotes, parens, braces and nested templates). Raises on non-termination."""
    n = len(s)
    i += 1
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            return i + 1
        if c == "$" and i + 1 < n and s[i + 1] == "{":
            i = _skip_interp(s, i + 2)
            continue
        i += 1
    raise LintError("unterminated template literal")


def _skip_interp(s, i):
    """i points just past the '${'. Return index just past the matching '}',
    tracking brace depth while skipping nested strings and templates."""
    n = len(s)
    depth = 1
    while i < n:
        c = s[i]
        if c in "\"'":
            i = _skip_quoted(s, i)
            continue
        if c == "`":
            i = _skip_template(s, i)
            continue
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return i
            continue
        i += 1
    raise LintError("unterminated template interpolation")


def _skip_any_string(s, i):
    if s[i] == "`":
        return _skip_template(s, i)
    return _skip_quoted(s, i)


def _skip_regex(s, i):
    """s[i] is '/', assumed to start a regex literal. Return index past the
    closing '/'. Handles escapes and character classes. Raises on non-term."""
    n = len(s)
    i += 1
    in_class = False
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            return i + 1
        elif c == "\n":
            raise LintError("unterminated regex literal")
        i += 1
    raise LintError("unterminated regex literal")


# JS keywords after which a '/' begins a regex literal, not division. Without
# these, a regex in keyword position (return /re/, typeof /re/) is misread as
# division, so a quote inside the regex is paired as a string delimiter across
# the surrounding code and can blank a real agent( token out of the scan.
_REGEX_PREFIX_KEYWORDS = frozenset({
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "throw", "case", "do", "else", "yield", "await",
})


def _prev_word(s, i):
    """Return the identifier / keyword ending immediately before index i
    (skipping intervening whitespace), or '' if the previous significant char
    is not a word char."""
    j = i - 1
    while j >= 0 and s[j] in " \t\r\n":
        j -= 1
    if j < 0 or not _wordchar(s[j]):
        return ""
    end = j + 1
    while j >= 0 and _wordchar(s[j]):
        j -= 1
    return s[j + 1:end]


def _ambiguous_division_regex(s, i):
    """s[i] is a '/' in DIVISION (value) context. Return True iff it would ALSO
    parse as a single-line regex literal (a closing '/' before any newline)
    whose body carries an UNBALANCED quote or backtick.

    Such a slash is genuinely ambiguous: the regex reading skips the quote, but
    the division reading treats it as a string delimiter that pairs across the
    intervening code -- which is exactly how an adversarial `foo /a'/ agent(...)`
    hides a model-less spawn. A body whose quotes are balanced is safe to read
    as division (its strings stay local and cannot swallow a later agent(), so
    the call is still scanned) and is NOT flagged, keeping ordinary one-line
    arithmetic like `a / b + "x" / c` from over-denying."""
    n = len(s)
    j = i + 1
    in_class = False
    counts = {"'": 0, '"': 0, "`": 0}
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c in counts:
            counts[c] += 1
        elif c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            return any(v % 2 for v in counts.values())
        elif c == "\n":
            return False
        j += 1
    return False


def _slash_is_regex(s, i, prev):
    """Classify the '/' at s[i] (already known not to open a // or /* comment).
    Returns True if it begins a regex literal (skip it as one), False if it is a
    division operator (advance past it). Raises LintError when the slash is
    ambiguous in a way that could hide an agent() call -> fail closed.

    prev is the previous significant (non-whitespace) char, '' at start."""
    # Operator / opener / start-of-input, or a regex-prefix keyword -> regex.
    if prev == "" or prev in "(,{[:;=!&|?+-*%^~<>":
        return True
    if _wordchar(prev) and _prev_word(s, i) in _REGEX_PREFIX_KEYWORDS:
        return True
    # Division (value) context. A benign division just advances; but a
    # division-context slash that also reads as a quote-bearing regex is the
    # fatal ambiguity -> deny rather than risk a silent allow.
    if _ambiguous_division_regex(s, i):
        raise LintError(
            "ambiguous '/': parses as both division and a quote-bearing regex "
            "literal, so agent() calls around it cannot be safely verified")
    return False


# ---- comment stripping (string-aware, length-preserving) ----------------
def strip_comments(s):
    """Replace // line comments and /* */ block comments with spaces (newlines
    kept), skipping over string / template literals AND regex literals so a
    // or /* (or a quote) inside them is not misread. Length is preserved.

    A '/' that begins a regex literal (per _slash_is_regex) is skipped as a
    regex, not treated as division or a comment: otherwise a regex containing a
    quote char (e.g. /'/ , /"/) would have that quote paired as a string
    delimiter across the intervening code."""
    out = list(s)
    n = len(s)
    i = 0
    prev = ""
    while i < n:
        c = s[i]
        if c in "\"'`":
            i = _skip_any_string(s, i)
            prev = c
            continue
        # Comments take precedence over regex: a regex can begin with neither
        # // (empty regex is invalid; // is always a line comment) nor /*
        # (a leading * quantifier is invalid), so these are unambiguous.
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            j = s.find("\n", i)
            if j == -1:
                j = n
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            close = s.find("*/", i + 2)
            if close == -1:
                raise LintError("unterminated block comment")
            end = close + 2
            for k in range(i, end):
                if s[k] != "\n":
                    out[k] = " "
            i = end
            continue
        if c == "/" and _slash_is_regex(s, i, prev):
            i = _skip_regex(s, i)
            prev = "/"
            continue
        if c not in " \t\r\n":
            prev = c
        i += 1
    return "".join(out)


# ---- string blanking (length-preserving) --------------------------------
def blank_strings(s):
    """Blank the CONTENTS of every '...' / "..." string and the literal text of
    `...` templates (delimiters kept). Inside a template, ${ ... } is code and
    is preserved, but strings nested within it are blanked recursively. This
    neutralises any model: / agent( / stray parens hiding in string text while
    keeping structure and offsets identical to the input.

    Regex literals (per _slash_is_regex) are skipped intact, not entered as
    strings: a regex containing a quote (e.g. /'/) must not have that quote
    paired as a string delimiter across the surrounding code, which would blank
    a real agent( token and silently allow a model-less spawn."""
    out = list(s)
    i = 0
    n = len(s)
    prev = ""
    while i < n:
        c = s[i]
        if c in "\"'":
            j = _skip_quoted(s, i)
            for k in range(i + 1, j - 1):
                out[k] = " "
            i = j
            prev = c
            continue
        if c == "`":
            i = _blank_template(s, i, out)
            prev = "`"
            continue
        if c == "/" and _slash_is_regex(s, i, prev):
            i = _skip_regex(s, i)
            prev = "/"
            continue
        if c not in " \t\r\n":
            prev = c
        i += 1
    return "".join(out)


def _blank_template(s, i, out):
    n = len(s)
    i += 1
    while i < n:
        c = s[i]
        if c == "\\":
            if s[i] != "\n":
                out[i] = " "
            if i + 1 < n and s[i + 1] != "\n":
                out[i + 1] = " "
            i += 2
            continue
        if c == "`":
            return i + 1
        if c == "$" and i + 1 < n and s[i + 1] == "{":
            i = _blank_interp(s, i + 2, out)
            continue
        if c != "\n":
            out[i] = " "
        i += 1
    raise LintError("unterminated template literal")


def _blank_interp(s, i, out):
    n = len(s)
    depth = 1
    while i < n:
        c = s[i]
        if c in "\"'":
            j = _skip_quoted(s, i)
            for k in range(i + 1, j - 1):
                out[k] = " "
            i = j
            continue
        if c == "`":
            i = _blank_template(s, i, out)
            continue
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth -= 1
            i += 1
            if depth == 0:
                return i
            continue
        i += 1
    raise LintError("unterminated template interpolation")


# ---- call / argument structural scanning --------------------------------
def _find_close_paren(s, open_idx):
    """open_idx indexes a '('. Return the index of the matching ')'.
    Skips strings and templates. Raises LintError if unbalanced."""
    depth = 0
    i = open_idx
    n = len(s)
    while i < n:
        c = s[i]
        if c in "\"'`":
            i = _skip_any_string(s, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise LintError("unbalanced parentheses")


def _find_close_brace(s, open_idx):
    depth = 0
    i = open_idx
    n = len(s)
    while i < n:
        c = s[i]
        if c in "\"'`":
            i = _skip_any_string(s, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise LintError("unbalanced braces")


def _split_args(s, start, end):
    """Split the call arguments in s[start:end] on top-level commas, skipping
    strings, templates and nested () [] {}. Returns a list of raw arg slices."""
    args = []
    depth = 0
    i = start
    seg_start = start
    while i < end:
        c = s[i]
        if c in "\"'`":
            i = _skip_any_string(s, i)
            continue
        if c in "([{":
            depth += 1
            i += 1
            continue
        if c in ")]}":
            depth -= 1
            i += 1
            continue
        if c == "," and depth == 0:
            args.append(s[seg_start:i])
            seg_start = i + 1
            i += 1
            continue
        i += 1
    args.append(s[seg_start:end])
    return args


def _colon_after(s, start):
    """If the next significant char at/after start is ':', return its index;
    else -1."""
    n = len(s)
    k = start
    while k < n and s[k] in " \t\r\n":
        k += 1
    return k if (k < n and s[k] == ":") else -1


def _extract_value(a, start, obj_close):
    """Raw text of the value beginning at `start`, ending at the next depth-0
    ',' or the object's closing brace at `obj_close`. Skips strings, templates,
    regex literals and nested () [] {}. Returned slice is stripped."""
    i = start
    depth = 0
    prev = ""
    while i < obj_close:
        c = a[i]
        if c in "\"'":
            i = _skip_quoted(a, i)
            prev = "\""
            continue
        if c == "`":
            i = _skip_template(a, i)
            prev = "`"
            continue
        if c == "/" and _slash_is_regex(a, i, prev):
            i = _skip_regex(a, i)
            prev = "/"
            continue
        if c in "([{":
            depth += 1
            prev = c
            i += 1
            continue
        if c in ")]}":
            depth -= 1
            prev = c
            i += 1
            continue
        if c == "," and depth == 0:
            break
        if c not in " \t\r\n":
            prev = c
        i += 1
    return a[start:i].strip()


def _string_literal_value(raw):
    """If `raw` is exactly one quoted string ('...'/"...") or a backtick
    template with no ${...} interpolation, return its inner text; else None
    (identifier, call, concatenation, interpolated template -> not a plain
    literal the lint can resolve)."""
    if len(raw) < 2:
        return None
    q = raw[0]
    if q in "\"'":
        try:
            end = _skip_quoted(raw, 0)
        except LintError:
            return None
        return raw[1:end - 1] if end == len(raw) else None
    if q == "`":
        if "${" in raw:
            return None
        try:
            end = _skip_template(raw, 0)
        except LintError:
            return None
        return raw[1:end - 1] if end == len(raw) else None
    return None


def _classify_model_value(raw):
    """Classify a top-level model key's raw value text. Returns one of:
      ("banned", display) - a string literal naming a BANNED_MODELS entry,
      ("blank",  "")      - a string literal that is empty / all whitespace,
      ("ok",     None)    - a valid string literal, OR a non-literal expression
                            (dynamic value the static lint cannot resolve)."""
    if raw is None:
        return ("ok", None)
    lit = _string_literal_value(raw)
    if lit is None:
        return ("ok", None)
    norm = lit.strip().lower()
    if norm == "":
        return ("blank", "")
    if norm in BANNED_MODELS:
        return ("banned", lit.strip())
    return ("ok", None)


def _toplevel_model(arg):
    """arg is a raw (comment-stripped, strings intact) argument slice. Return
    (present, value_raw): present is True iff `arg` is a brace object literal
    whose DEPTH-1 keys include a `model` key (bare or quoted) in KEY position;
    value_raw is that key's raw value text (or None when absent). A model key
    nested deeper, or inside a string / regex / value position, does not count.

    A `model`/`"model"` token only counts as a key when the previous
    significant char is the opening `{` or a property-separating `,`. This
    rejects value-position tokens such as the true-branch of a ternary
    (`cond ? model : x`), whose token sits immediately before the ternary's own
    colon and would otherwise masquerade as a top-level key."""
    a = arg.strip()
    if not a.startswith("{"):
        return (False, None)
    close = _find_close_brace(a, 0)
    n = len(a)
    i = 1
    depth = 0
    prev = "{"
    while i < close:
        c = a[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c in "\"'":
            j = _skip_quoted(a, i)
            content = a[i + 1:j - 1]
            if depth == 0 and content == "model" and prev in ("{", ","):
                col = _colon_after(a, j)
                if col != -1:
                    return (True, _extract_value(a, col + 1, close))
            prev = "\""
            i = j
            continue
        if c == "`":
            i = _skip_template(a, i)
            prev = "`"
            continue
        if c == "/" and _slash_is_regex(a, i, prev):
            i = _skip_regex(a, i)
            prev = "/"
            continue
        if c in "([{":
            depth += 1
            prev = c
            i += 1
            continue
        if c in ")]}":
            depth -= 1
            prev = c
            i += 1
            continue
        if (depth == 0 and c == "m" and a[i:i + 5] == "model"
                and not _wordchar(a[i - 1]) and prev in ("{", ",")):
            after = a[i + 5] if i + 5 < n else ""
            if not _wordchar(after):
                col = _colon_after(a, i + 5)
                if col != -1:
                    return (True, _extract_value(a, col + 1, close))
            prev = "l"
            i += 5
            continue
        prev = c
        i += 1
    return (False, None)


CALL_RE = re.compile(r"(?<![\w$.])(agent|workflow)\s*\(")


def _snippet(s, start, end):
    return re.sub(r"\s+", " ", s[start:end + 1]).strip()[:120]


def lint_workflow(script):
    """Return a list of problem descriptions; empty means clean. Raises
    LintError on any structural anomaly (-> caller denies, fail-closed)."""
    s0 = strip_comments(script)
    s1 = blank_strings(s0)   # validates strings; neutralises string text
    problems = []
    for m in CALL_RE.finditer(s1):
        name = m.group(1)
        paren = m.end() - 1
        close = _find_close_paren(s0, paren)
        if name == "workflow":
            problems.append(
                "nested workflow() call cannot be statically verified for "
                "explicit per-agent models -> %s" % _snippet(s0, m.start(), close))
            continue
        args = _split_args(s0, paren + 1, close)
        if len(args) < 2 or not args[1].strip():
            problems.append(
                "agent() call has no options object (second argument), so no "
                "explicit model can be set -> %s" % _snippet(s0, m.start(), close))
            continue
        present, value_raw = _toplevel_model(args[1])
        if not present:
            problems.append(
                "agent() options object has no top-level model: key -> %s"
                % _snippet(s0, m.start(), close))
            continue
        status, disp = _classify_model_value(value_raw)
        if status == "banned":
            problems.append(
                "agent() options pin a banned model %r; it must be one of %s "
                "-> %s" % (disp, VALID_CHOICES_STR, _snippet(s0, m.start(), close)))
        elif status == "blank":
            problems.append(
                "agent() options set a blank model; it must be one of %s -> %s"
                % (VALID_CHOICES_STR, _snippet(s0, m.start(), close)))
    return problems


# ---- frontmatter pin lookup ---------------------------------------------
_FM_MODEL_RE = re.compile(
    r"(?im)^\s*model\s*:\s*[\"']?([A-Za-z0-9._\[\]-]+)")


def _read_frontmatter_model(path):
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(8192)
    except OSError:
        return None
    fm = head
    if head.startswith("---"):
        end = head.find("\n---", 3)
        if end != -1:
            fm = head[:end]
    m = _FM_MODEL_RE.search(fm)
    return m.group(1) if m else None


def frontmatter_model(cwd, subagent_type):
    """Walk UP from cwd through every parent directory checking
    <dir>/.claude/agents/<type>.md, then ~/.claude/agents/<type>.md. Return the
    first pinned model found, else None."""
    if not subagent_type or not isinstance(subagent_type, str):
        return None
    if "/" in subagent_type or "\\" in subagent_type or subagent_type in (".", ".."):
        return None
    d = os.path.abspath(cwd or ".")
    candidates = []
    while True:
        candidates.append(
            os.path.join(d, ".claude", "agents", subagent_type + ".md"))
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    candidates.append(
        os.path.join(os.path.expanduser("~"), ".claude", "agents",
                     subagent_type + ".md"))
    for p in candidates:
        pinned = _read_frontmatter_model(p)
        if pinned:
            return pinned
    return None


# ---- per-tool handlers --------------------------------------------------
def handle_agent(ti, cwd):
    model = ti.get("model")
    if isinstance(model, str) and model.strip():
        norm = model.strip().lower()
        if norm in BANNED_MODELS:
            deny("Model %r is banned for subagents: it lets the agent inherit "
                 "or silently downgrade instead of running a model chosen for "
                 "the task. Re-issue the Agent call with an explicit model "
                 "(one of %s)." % (model.strip(), VALID_CHOICES_STR))
        abstain()  # explicit, allowed model -> proceed
    # model omitted or blank
    if ALLOW_FRONTMATTER_PIN:
        st = ti.get("subagent_type")
        pinned = frontmatter_model(cwd, st)
        if pinned:
            if pinned.strip().lower() in BANNED_MODELS:
                deny("subagent_type %r pins a banned model %r in its "
                     "frontmatter. Set an explicit model at the call site "
                     "(one of %s)." % (st, pinned, VALID_CHOICES_STR))
            abstain()  # a frontmatter pin is an explicit per-task choice
    deny("This subagent spawn omits an explicit `model`, so it would silently "
         "inherit the parent session's model. Re-issue the Agent call with an "
         "explicit model parameter (one of %s)." % VALID_CHOICES_STR)


def handle_workflow(ti, cwd):
    script = ti.get("script")
    if not isinstance(script, str):
        sp = ti.get("scriptPath")
        if isinstance(sp, str) and sp:
            path = sp if os.path.isabs(sp) else os.path.join(cwd or ".", sp)
            try:
                with open(path, encoding="utf-8") as f:
                    script = f.read()
            except OSError as e:
                deny("Workflow scriptPath %r could not be read to verify "
                     "explicit per-agent models (%s). Pass the script inline "
                     "so every agent() model can be checked." % (sp, e))
        elif ti.get("name") is not None or ti.get("resumeFromRunId") is not None:
            msg = ("This workflow is invoked by name / resume id, so its saved "
                   "script cannot be statically inspected for explicit "
                   "per-agent models. Approve only if you trust it; otherwise "
                   "re-invoke Workflow with an inline `script` whose every "
                   "agent() call sets a top-level model:.")
            if STRICT_SAVED_WORKFLOWS:
                deny(msg + " (STRICT_SAVED_WORKFLOWS is on, so it is denied.)")
            ask(msg)
        else:
            deny("Workflow payload has none of script / scriptPath / name; "
                 "nothing can be statically verified, so it is denied.")
    try:
        problems = lint_workflow(script)
    except LintError as e:
        deny("Workflow script could not be safely parsed (%s), so its agent() "
             "spawns cannot be verified. Ensure strings, parentheses and braces "
             "are balanced and give every agent() an explicit top-level model:."
             % e)
        return
    if problems:
        deny("Every agent() a workflow spawns must pass a top-level model: in "
             "its options object (one of %s). Problems found:\n- %s\nRewrite "
             "each as agent(prompt, { model: 'opus'|'sonnet'|'haiku', ... })."
             % (VALID_CHOICES_STR, "\n- ".join(problems)))
    abstain()  # clean


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # unparseable -> abstain, never wedge the session
    if not isinstance(data, dict):
        sys.exit(0)
    tool = data.get("tool_name", "")
    ti = data.get("tool_input")
    if not isinstance(ti, dict):
        ti = {}
    cwd = data.get("cwd") or os.getcwd()
    if tool in ("Agent", "Task"):
        handle_agent(ti, cwd)
    elif tool == "Workflow":
        handle_workflow(ti, cwd)
    sys.exit(0)


if __name__ == "__main__":
    main()
