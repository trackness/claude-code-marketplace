#!/usr/bin/env python3
r"""PreToolUse enforcement logic (the 3.14-only core the sibling
``enforce_explicit_model`` entry point runs after guarding the interpreter
version): every subagent / workflow spawn must name an explicit, allowed model.

WHY. An omitted model silently inherits the parent session's model; a banned
model (fable / inherit) defeats the "a model chosen for its task" rule. The hook
denies via the PreToolUse permissionDecision schema so the block holds even under
bypassPermissions, and the reason is fed back so the model retries in-turn.

SHAPE. The program is one total function -- `decide(payload) -> Decision` --
wrapped by a thin `main` that reads stdin, emits the decision, and exits 0. A
Decision is deny / ask / abstain; only deny and ask print JSON. Deciding never
touches stdout, so the policy stays a pure value and emission is in one place.

Tools handled (matcher "Agent|Task|Workflow"):
  Agent / Task : deny unless tool_input.model is an explicit non-banned model,
                 or the subagent_type's agent .md frontmatter pins one.
  Workflow     : statically lint the script TEXT; every agent() call must pass
                 a top-level model: key in its options object. Fail-closed:
                 anything the lint cannot positively verify is denied. Saved /
                 bundled workflows invoked by name or resume id cannot be
                 inspected -> "ask" (or deny under the strict env flag).

LEXING MODEL. The lint runs on a real ECMAScript token stream, implementing the
InputElementDiv / InputElementRegExp distinction via the standard PREVIOUS-
SIGNIFICANT-TOKEN rule (as acorn/esprima do), because whether a '/' is division
or a regex decides whether a following agent() stays a visible token.
_slash_context holds the rule; '//' / '/*' are always comments.

FAIL-CLOSED GUARDS (fatal-direction preservation). Correct lexing of adversarial
input can still hide a spawn, so two nets fail closed to a LintError -> deny:
quote-bearing division/regex ambiguity (_division_ambiguity) and a stray '\'
identifier escape. All downstream logic (call detection, argument splitting,
depth-1 model key scan, value classification) operates on the token stream, over
precomputed O(n) bracket indexes (see LintCtx) so the lint stays linear;
template interpolations are tokenized recursively and linted independently.

stdlib only; python3 >= 3.14. Unparsable stdin abstains (exit 0) so a malformed
payload never wedges a session. The hook always exits 0.
"""

import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from _jsstr import js_decode_string

BANNED_MODELS = {"fable", "inherit"}
VALID_CHOICES = ["haiku", "sonnet", "opus"]
VALID_CHOICES_STR = ", ".join(VALID_CHOICES)
ALLOW_FRONTMATTER_PIN = True

# An agent() spawn needs both a prompt and an options object; fewer top-level
# arguments than this cannot carry a model: key.
_MIN_AGENT_ARGS = 2


def _env_flag(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Default False per plan; env override exists so the strict path is exercisable.
STRICT_SAVED_WORKFLOWS = _env_flag("MODEL_GUARD_STRICT_SAVED_WORKFLOWS", False)


class LintError(Exception):
    """Raised on any structural anomaly the lint cannot resolve soundly:
    unterminated string / template / comment / regex, an unbalanced bracket
    around a call, or a genuinely ambiguous division-vs-regex slash. Every
    LintError maps to a deny (fail-closed)."""


# ---- decision model + emission ----
@dataclass(frozen=True, slots=True)
class Decision:
    """The hook's whole output: one of deny / ask / abstain. deny and ask carry
    a reason the caller sees in the permission prompt; abstain carries none and
    prints nothing (normal permission flow proceeds)."""

    kind: str  # "deny" | "ask" | "abstain"
    reason: str = ""


def deny(reason):
    """A deny decision: the spawn is blocked and `reason` is fed back."""
    return Decision("deny", reason)


def ask(reason):
    """An ask decision: the user gates the spawn at runtime with `reason`."""
    return Decision("ask", reason)


# The hook governs nothing here; stay out of the way. Reused, never rebuilt.
ABSTAIN = Decision("abstain")


def emit(decision):
    """Render a Decision: deny/ask print hookSpecificOutput JSON, abstain nothing."""
    if decision.kind == "abstain":
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision.kind,
                    "permissionDecisionReason": decision.reason,
                }
            }
        )
    )


# ---- entry point + dispatch ----
def main():
    """Read the PreToolUse payload on stdin, emit the decision, exit 0."""
    try:
        payload = json.load(sys.stdin)
    except ValueError, OSError, RecursionError:
        sys.exit(0)  # unparsable -> abstain, never wedge the session
    emit(decide(payload))
    sys.exit(0)


def decide(payload):
    """Map a PreToolUse payload to a Decision. Anything the hook does not govern
    -- a non-object payload, a tool other than Agent / Task / Workflow -- abstains
    so normal permission flow is untouched."""
    if not isinstance(payload, dict):
        return ABSTAIN
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    cwd = payload.get("cwd") or os.getcwd()
    if tool in ("Agent", "Task"):
        return decide_agent(tool_input, cwd)
    if tool == "Workflow":
        return decide_workflow(tool_input, cwd)
    return ABSTAIN


# ---- Agent / Task policy ----
def decide_agent(tool_input, cwd):
    """Enforce the Agent / Task model policy for one tool_input.

    An explicit, non-banned string model abstains. A banned one denies. An
    omitted / blank / non-string model denies -- unless the subagent_type's
    frontmatter pins an acceptable model, which counts as the explicit choice."""
    model = tool_input.get("model")
    if isinstance(model, str) and model.strip():
        if model.strip().lower() in BANNED_MODELS:
            return deny(
                f"Model {model.strip()!r} is banned for subagents: it lets the "
                f"agent inherit or silently downgrade instead of running a model "
                f"chosen for the task. Re-issue the Agent call with an explicit "
                f"model (one of {VALID_CHOICES_STR})."
            )
        return ABSTAIN  # explicit, allowed model -> proceed
    if ALLOW_FRONTMATTER_PIN:
        subagent_type = tool_input.get("subagent_type")
        pinned = frontmatter_model(cwd, subagent_type)
        if pinned:
            if pinned.strip().lower() in BANNED_MODELS:
                return deny(
                    f"subagent_type {subagent_type!r} pins a banned model "
                    f"{pinned!r} in its frontmatter. Set an explicit model at "
                    f"the call site (one of {VALID_CHOICES_STR})."
                )
            return ABSTAIN  # a frontmatter pin is an explicit per-task choice
    return deny(
        f"This subagent spawn omits an explicit `model`, so it would silently "
        f"inherit the parent session's model. Re-issue the Agent call with an "
        f"explicit model parameter (one of {VALID_CHOICES_STR})."
    )


# ---- frontmatter pin lookup ----
_FM_MODEL_RE = re.compile(r"(?im)^\s*model\s*:\s*[\"']?([A-Za-z0-9._\[\]-]+)")


def frontmatter_model(cwd, subagent_type):
    """Walk UP from cwd through every parent dir checking
    <dir>/.claude/agents/<type>.md, then ~/.claude/agents/<type>.md; return the
    first pinned model, else None. A subagent_type carrying a path separator (or
    . / ..) is refused so no pin is read from outside an agents directory."""
    if not subagent_type or not isinstance(subagent_type, str):
        return None
    if "/" in subagent_type or "\\" in subagent_type or subagent_type in (".", ".."):
        return None
    leaf = subagent_type + ".md"
    d = os.path.abspath(cwd or ".")
    while True:
        pinned = _read_frontmatter_model(os.path.join(d, ".claude", "agents", leaf))
        if pinned:
            return pinned
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    home = os.path.join(os.path.expanduser("~"), ".claude", "agents", leaf)
    return _read_frontmatter_model(home)


def _read_frontmatter_model(path):
    """Return the `model:` pinned in `path`'s well-formed leading YAML frontmatter
    block (first line `---`, closed by a later `---`), else None. Body prose, an
    unclosed `---`, and a >1-`model:` block (ambiguous: YAML keeps the last, a
    first-wins read the first) each yield no pin; unreadable / non-UTF-8 is None."""
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(8192)
    except OSError, ValueError:  # ValueError: NUL in path / non-UTF-8 (fail-closed)
        return None
    lines = head.split("\n")
    ends = [i for i in range(1, len(lines)) if lines[i].rstrip() == "---"]
    if lines[0].rstrip() != "---" or not ends:  # no well-formed leading block
        return None
    end = ends[0]  # first closing '---'; body after it is never a pin source
    matches = _FM_MODEL_RE.findall("\n".join(lines[1:end]))
    return matches[0] if len(matches) == 1 else None  # 0 or >1 keys -> no pin


# ---- Workflow policy ----
def decide_workflow(tool_input, cwd):
    """Decide a Workflow payload. An inline `script` (or a readable `scriptPath`)
    is statically linted. A saved workflow invoked by name / resume id cannot be
    inspected -> ask (deny under the strict flag). A payload with nothing to
    inspect, or an unreadable scriptPath, denies (fail-closed)."""
    script = _workflow_script(tool_input, cwd)
    if isinstance(script, Decision):
        return script  # no inspectable script -> ask / deny already decided
    try:
        problems = lint_workflow(script)
    except LintError as e:
        return deny(
            f"Workflow script could not be safely parsed ({e}), so its agent() "
            f"spawns cannot be verified. Ensure strings, parentheses and braces "
            f"are balanced and give every agent() an explicit top-level model:."
        )
    except RecursionError:
        return deny(
            "Workflow script nests template literals / interpolations too deeply "
            "to statically verify, so its agent() spawns cannot be checked and it "
            "is denied. Reduce the nesting depth and give every agent() an "
            "explicit top-level model:."
        )
    except Exception as e:  # fail closed on ANY non-LintError lint failure
        return deny(
            f"Workflow script could not be statically analysed "
            f"({type(e).__name__}), so its agent() spawns cannot be verified and "
            f"it is denied. Give every agent() an explicit top-level model:."
        )
    if problems:
        joined = "\n- ".join(problems)
        return deny(
            f"Every agent() a workflow spawns must pass a top-level model: in "
            f"its options object (one of {VALID_CHOICES_STR}). Problems found:\n"
            f"- {joined}\nRewrite each as agent(prompt, "
            f"{{ model: 'opus'|'sonnet'|'haiku', ... }})."
        )
    return ABSTAIN  # clean


def _workflow_script(tool_input, cwd):
    """Resolve the script TEXT to lint from a Workflow tool_input. Returns the
    script string when one is inspectable, or a terminal Decision (ask / deny)
    when the payload names a script that cannot be read or inspected."""
    script = tool_input.get("script")
    if isinstance(script, str):
        return script
    sp = tool_input.get("scriptPath")
    if isinstance(sp, str) and sp:
        path = sp if os.path.isabs(sp) else os.path.join(cwd or ".", sp)
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except (OSError, ValueError) as e:  # ValueError: NUL / non-UTF-8 script
            return deny(
                f"Workflow scriptPath {sp!r} could not be read to verify "
                f"explicit per-agent models ({e}). Pass the script inline "
                f"so every agent() model can be checked."
            )
    if any(tool_input.get(k) is not None for k in ("name", "resumeFromRunId")):
        msg = (
            "This workflow is invoked by name / resume id, so its saved "
            "script cannot be statically inspected for explicit "
            "per-agent models. Approve only if you trust it; otherwise "
            "re-invoke Workflow with an inline `script` whose every "
            "agent() call sets a top-level model:."
        )
        if STRICT_SAVED_WORKFLOWS:
            return deny(msg + " (STRICT_SAVED_WORKFLOWS is on, so it is denied.)")
        return ask(msg)
    return deny(
        "Workflow payload has none of script / scriptPath / name; "
        "nothing can be statically verified, so it is denied."
    )


# ---- Workflow static lint (token stream) ----
# Tok is the shared vocabulary of the lint and the lexer: the lexer (lower in
# the file) produces these tokens and the lint here consumes them, so the type
# is defined once at the top of the consuming layer -- before its first use --
# rather than as a forward reference.
@dataclass(slots=True)
class Tok:
    """A significant token. Whitespace and comments are never emitted.

    type: one of str, template, regex, num, name, punct.
    text: the raw source slice (delimiters / operator chars included).
    start, end: source offsets (end exclusive) for RAW-source deny snippets.
    kind: 'member' on a name whose previous significant token is a '.' (a '?.'
          chain reduces to that '.'), so a keyword used as a PROPERTY (obj.of,
          gen.return, p.catch) is the value it is -- a following '/' divides, a
          following '(' is an ordinary call -- never a regex-prefix / control
          keyword. None else.
    interps: only on a template token: the token-lists, one per ${...}
             interpolation, so calls inside interpolations are lintable.
    """

    type: str
    text: str
    start: int
    end: int
    kind: str | None = None
    interps: list[list[Tok]] | None = None


@dataclass(slots=True)
class LintCtx:
    """Shared context threaded through the token-stream lint: the token list, the
    raw source (for deny snippets), the accumulating problem list, and four O(n)
    bracket indexes over ``tokens`` (:func:`_index_brackets`). Interpolations walk
    a fresh context over their own token list (indexed once) but share raw/problems.

    The indexes keep the lint LINEAR: without them every agent()/workflow() token
    rescans to its ')' and re-splits its argument region, so D nested calls cost
    O(D^2) and a deeply-nested script can pad a model-less spawn past the hook
    timeout -- whereupon the hook is killed before emitting a decision, the
    FAIL-OPEN this plugin forbids."""

    tokens: list[Tok]
    raw: str
    problems: list[str] = field(default_factory=list)
    pre: list[int] = field(default_factory=list, init=False)
    jump: list[int | None] = field(default_factory=list, init=False)
    cp: list[int | None] = field(default_factory=list, init=False)
    cb: list[int | None] = field(default_factory=list, init=False)

    def __post_init__(self):
        self.pre, self.jump, self.cp, self.cb = _index_brackets(self.tokens)


def _index_brackets(tokens):
    """Precompute four bracket indexes over ``tokens`` in one O(n) pass; a mixed
    and a per-kind model run together because the lint uses both.

    pre[i]  : MIXED depth before token i (( [ { are +1, ) ] } are -1, so kinds
              cancel: `a } , b` is one segment); a top-level comma splits iff its
              pre equals the region base depth.
    jump[i] : opener -> its mixed single-stack match (else None), to skip a
              balanced sub-group in one step.
    cp / cb : the PER-KIND '(' -> ')' / '{' -> '}' match (an interior '{' cannot
              steal a '(' from its ')'); an opener with no closer stays None so a
              lookup needing it raises LintError -> deny (fail-closed)."""
    n = len(tokens)
    pre = [0] * n
    jump: list[int | None] = [None] * n
    cp: list[int | None] = [None] * n
    cb: list[int | None] = [None] * n
    per = {"(": (cp, []), "{": (cb, [])}  # opener -> (its result array, its stack)
    closer_opener = {")": "(", "]": "[", "}": "{"}
    mix: list[int] = []
    depth = 0
    for i, t in enumerate(tokens):
        pre[i] = depth
        if t.type != "punct":
            continue
        if t.text in ("(", "[", "{"):
            depth += 1
            mix.append(i)
            if t.text in per:
                per[t.text][1].append(i)
        elif (opener := closer_opener.get(t.text)) is not None:
            depth -= 1
            if mix:
                jump[mix.pop()] = i
            if opener in per and per[opener][1]:
                arr, stack = per[opener]
                arr[stack.pop()] = i
    return pre, jump, cp, cb


def lint_workflow(script):
    """Return a list of problem descriptions; empty means clean. Raises
    LintError on any structural anomaly (-> caller denies, fail-closed)."""
    tokens, _ = _lex(script, 0, interp=False)
    ctx = LintCtx(tokens, script)
    _lint_tokens(ctx)
    return ctx.problems


def _lint_tokens(ctx):
    """Walk a token stream: lint every agent() call, flag every nested workflow()
    call, and recurse into template interpolations. The walk does NOT skip a
    call's arguments, so nested agent()/workflow() calls in argument positions are
    found too (matching the reference, which matched agent( anywhere)."""
    tokens = ctx.tokens
    n = len(tokens)
    for i in range(n):
        tok = tokens[i]
        if tok.type == "template":
            for sub in tok.interps or ():
                _lint_tokens(LintCtx(sub, ctx.raw, ctx.problems))
            continue
        if tok.type != "name" or tok.text not in ("agent", "workflow"):
            continue
        prevtok = tokens[i - 1] if i > 0 else None
        if prevtok is not None and prevtok.type == "punct" and prevtok.text == ".":
            continue  # member access: obj.agent(...) / obj?.agent(...)
        open_i = _call_open_index(tokens, i)
        if open_i is None:
            continue  # not a call
        close_i = ctx.cp[open_i]
        if close_i is None:  # '(' with no matching ')' -> fail closed (as before)
            raise LintError("unbalanced parentheses")
        if tok.text == "workflow":
            snip = _snippet(ctx.raw, tok.start, tokens[close_i].end)
            ctx.problems.append(
                f"nested workflow() call cannot be statically verified for "
                f"explicit per-agent models -> {snip}"
            )
            continue
        _lint_call(ctx, i, open_i, close_i)


def _is_punct(tokens, k, text):
    """True iff tokens[k] exists and is the punctuator `text`."""
    return 0 <= k < len(tokens) and tokens[k].type == "punct" and tokens[k].text == text


def _call_open_index(tokens, name_i):
    """Index of the '(' making tokens[name_i] a call, else None: matches `agent(`
    AND the optional call `agent?.(` (name '?' '.' '('); `agent ? a : b` -> None."""
    j = name_i + 1
    if _is_punct(tokens, j, "?"):
        if _is_punct(tokens, j + 1, ".") and _is_punct(tokens, j + 2, "("):
            return j + 2  # optional call: name ?. (
        return None  # a `name ? a : b` ternary, not a call
    if _is_punct(tokens, j, "("):
        return j
    return None


def _lint_call(ctx, name_i, open_i, close_i):
    """Lint one agent() call given its name / '(' / ')' token indices."""
    tokens = ctx.tokens
    snip = _snippet(ctx.raw, tokens[name_i].start, tokens[close_i].end)
    args = _top_level_ranges(ctx, open_i + 1, close_i)
    if len(args) < _MIN_AGENT_ARGS or args[1][0] == args[1][1]:
        ctx.problems.append(
            f"agent() call has no options object (second argument), so no "
            f"explicit model can be set -> {snip}"
        )
        return
    present, value_range = _object_model(ctx, args[1])
    if not present:
        ctx.problems.append(
            f"agent() options object has no top-level model: key -> {snip}"
        )
        return
    status, disp = _classify_model_value(ctx, value_range)
    if status == "banned":
        ctx.problems.append(
            f"agent() options pin a banned model {disp!r}; it must be one of "
            f"{VALID_CHOICES_STR} -> {snip}"
        )
    elif status == "blank":
        ctx.problems.append(
            f"agent() options set a blank model; it must be one of "
            f"{VALID_CHOICES_STR} -> {snip}"
        )


def _object_model(ctx, opts):
    """opts is the (lo, hi) token range of an agent() call's second argument.
    Return (present, value_range) for its top-level `model` entry after proving
    the object statically simple. FAIL CLOSED (LintError -> deny) on a non-brace
    opener, any entry :func:`_simple_key` rejects, or a duplicate `model` key."""
    tokens = ctx.tokens
    lo, hi = opts
    if not (lo < hi and tokens[lo].type == "punct" and tokens[lo].text == "{"):
        return False, None  # not an object-literal opener
    close = ctx.cb[lo]
    if close is None or close >= hi:  # '{' not closed within this argument
        raise LintError("unbalanced braces")
    value_range = None
    for elo, ehi in _top_level_ranges(ctx, lo + 1, close):
        if elo == ehi or _simple_key(tokens, elo, ehi) != "model":
            continue  # empty entry (trailing comma) or a non-model simple key
        if value_range is not None:  # JS keeps the LAST duplicate; deny both
            raise LintError("duplicate top-level model key")
        value_range = (elo + 2, ehi)
    return value_range is not None, value_range


def _simple_key(tokens, elo, ehi):
    """Return the NAME of a WHITELISTED-simple `simple-key : value` object entry
    (bareword or escape-free quoted key, then ':'); FAIL CLOSED (LintError ->
    deny) on a spread, computed `[expr]` key, getter/setter/method/shorthand,
    escaped quoted key, or `__proto__` key -- any construct that could hide it."""
    sep = tokens[elo + 1] if ehi > elo + 1 else None  # need a key then its ':'
    if sep is None or sep.type != "punct" or sep.text != ":":
        raise LintError("non-simple options entry (spread/computed/getter/shorthand)")
    key = tokens[elo]
    if key.type == "name":
        name = key.text
    elif key.type == "str" and "\\" not in key.text:
        name = key.text[1:-1]  # escape-free quoted key: strip its quotes
    else:
        raise LintError("computed or escaped options key cannot be verified")
    if name == "__proto__":
        raise LintError("__proto__ options key mutates the prototype")
    return name


def _classify_model_value(ctx, value_range):
    """Classify a top-level model key's value range. Returns:
    ("banned", display) - a literal that JS-DECODES to a BANNED_MODELS entry,
    ("blank",  "")      - a literal that decodes to empty / all whitespace,
    ("ok",     None)    - any other literal, OR a non-literal (dynamic) value the
                          static lint cannot resolve (treated as satisfying)."""
    lit = _literal_string(ctx, value_range)
    if lit is None:
        return "ok", None
    norm = lit.strip().lower()
    if norm == "":
        return "blank", ""
    if norm in BANNED_MODELS:
        return "banned", lit.strip()
    return "ok", None


def _literal_string(ctx, value_range):
    """Resolve a model value that is exactly ONE string / non-interpolated template
    literal to the runtime string JS would produce -- its escapes JS-decoded by
    :func:`js_decode_string`, so an escaped banned name folds and is caught. None
    when the value carries no string / template token (a bare identifier / call,
    treated as satisfying). FAIL CLOSED (LintError -> deny) on any OTHER string-
    bearing value -- a concatenation, ternary, `||` default, or interpolation can
    fold to a banned model no cheap check proves safe; a real name is one literal."""
    lo, hi = value_range
    if hi - lo == 1:
        t = ctx.tokens[lo]
        if t.type == "str" or (t.type == "template" and not t.interps):
            return js_decode_string(t.text[1:-1])  # strip delimiters, decode escapes
    if any(ctx.tokens[k].type in ("str", "template") for k in range(lo, hi)):
        raise LintError(
            "model value is not one plain string literal (a concatenation, "
            "ternary, or interpolation can fold to a banned model)"
        )
    return None


def _top_level_ranges(ctx, start, stop):
    """Split tokens[start:stop] on top-level commas, returning one (lo, hi) index
    range per item; an item-less region still yields a single empty range (an
    empty call reads as one empty argument). A comma at index k splits iff
    ``pre[k] == base`` (the region's entry depth), reproducing the old mixed
    running-depth == 0 test exactly (negative excursions included). O(items) not
    O(tokens): at depth >= base an opener's group is deeper and holds no splitter,
    so ``jump`` skips it in one step."""
    if start >= stop:
        return [(start, stop)]
    tokens = ctx.tokens
    pre = ctx.pre
    jump = ctx.jump
    base = pre[start]
    ranges = []
    lo = start
    k = start
    while k < stop:
        tok = tokens[k]
        if tok.type == "punct":
            if tok.text == "," and pre[k] == base:
                ranges.append((lo, k))
                lo = k + 1
                k += 1
                continue
            if tok.text in ("(", "[", "{") and pre[k] >= base:
                m = jump[k]
                if m is not None and m < stop:
                    k = m + 1  # skip the balanced sub-group in one step
                    continue
        k += 1
    ranges.append((lo, stop))
    return ranges


# Source chars scanned per deny snippet (a whitespace-collapsed slice truncated
# to 120). Bounding it (>> 120, so ordinary calls quote identically) keeps each
# snippet O(1) not O(call-span) -- else an outer call spanning the whole script
# rescans every byte, restoring the quadratic this lint's indexing removes.
_SNIPPET_SCAN = 512


def _snippet(raw, start, end):
    """A whitespace-collapsed, length-capped slice of the raw source for a deny
    reason, so the offending call is quoted back readably. Callers pass a token's
    (non-whitespace) start offset, so scanning a bounded prefix yields the same
    first-120 chars as the full span for any realistically-spaced call."""
    stop = min(end, start + _SNIPPET_SCAN)
    return re.sub(r"\s+", " ", raw[start:stop]).strip()[:120]


# ---- ECMAScript lexer ----
# Reserved beforeExpr words: a word that CANNOT end an expression, so a '/'
# after it opens a regex, not division. Any other word (a plain identifier or a
# value keyword such as this / true / null) ends an expression -> its '/' is
# division. The contextual keywords of / yield / await are DELIBERATELY excluded:
# each can also be an identifier (`let of = 1`) or a property, so it ends an
# expression and its '/' is division -- else a model-less `of / agent('t', {})
# / 2` would be swallowed whole as a regex and allowed.
_REGEX_KEYWORD_WORDS = (
    "return typeof instanceof in new delete void throw case do else default extends"
)
_REGEX_KEYWORDS = frozenset(_REGEX_KEYWORD_WORDS.split())

# Token types whose presence ENDS an expression, so a following '/' is division.
_VALUE_TYPES = frozenset({"num", "str", "regex", "template"})
# Punctuators that end an expression (a close bracket or a postfix update).
_DIV_PUNCT = frozenset({")", "]", "}", "++", "--"})


def _wordchar(c):
    return c.isalnum() or c in "_$"


# ---- literal scanners (return the index/tuple just past the literal) -----
def _scan_string(s, i):
    """s[i] is ' or ". Return index just past the closing quote. Raises on an
    unterminated string (raw newline or EOF)."""
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


def _scan_regex(s, i):
    """s[i] is '/', already known to start a regex literal. Return index past the
    closing '/'. Handles escapes and [ ] character classes (a '/' inside a class
    is literal); trailing flags are left for the next (identifier) token. Raises
    on non-termination (a regex cannot cross a newline)."""
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


def _scan_number(s, i):
    """s[i] starts a numeric literal. Return index just past it. Deliberately
    permissive (hex/bin/oct/float/exponent/bigint/separators) -- its only job is
    to end the number so a following '/' divides and the next identifier shows."""
    n = len(s)
    j = i
    if s[j] == ".":
        j += 1
        while j < n and s[j].isdigit():
            j += 1
        return j
    if s[j] == "0" and j + 1 < n and s[j + 1] in "xXbBoO":
        j += 2
        while j < n and (s[j].isalnum() or s[j] == "_"):
            j += 1
        return j
    while j < n and (s[j].isdigit() or s[j] == "_"):
        j += 1
    if j < n and s[j] == ".":
        j += 1
        while j < n and (s[j].isdigit() or s[j] == "_"):
            j += 1
    if j < n and s[j] in "eE":
        j += 1
        if j < n and s[j] in "+-":
            j += 1
        while j < n and s[j].isdigit():
            j += 1
    if j < n and s[j] == "n":
        j += 1
    return j


def _division_ambiguity(s, i):
    """s[i] is a '/' the lexer classified as DIVISION. Return True iff it ALSO
    admits a single-line regex reading (an unescaped closing '/' before the next
    newline) whose span carries an UNBALANCED quote / backtick -- the fatal
    ambiguity where the regex reading skips the quote but the division reading
    pairs it across the code, hiding a model-less spawn (`foo /a'/ agent(...)`). A
    balanced span stays local, so ordinary `a / b + "x" / c` is NOT flagged."""
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


def _slash_context(prev):
    """Classify a '/' by the previous significant token as 'regex' or 'div'.

    A '/' is DIVISION after anything that can END an expression (a value literal,
    an identifier, a keyword-as-property like obj.of, or a closing ) ] }), so a
    real call after the slash stays a visible token; it opens a REGEX only where a
    fresh expression is demanded (start of input, after an operator / opener, or a
    reserved beforeExpr keyword). The ) and } cases are resolved as DIVISION --
    the fatal-safe reading -- since it never hides a following call and
    _division_ambiguity still fails closed on the quote-bearing form."""
    if prev is None:
        return "regex"  # start of input
    if prev.type == "name":
        # obj.<keyword> is a property value (division); a bare beforeExpr keyword
        # opens a regex; any other word ends an expression (division).
        if prev.kind != "member" and prev.text in _REGEX_KEYWORDS:
            return "regex"
        return "div"
    if prev.type in _VALUE_TYPES:
        return "div"  # a value that ends an expression
    # punctuator: a close bracket / postfix update ends an expression (division);
    # every other operator / opener demands a fresh expression (regex).
    return "div" if prev.text in _DIV_PUNCT else "regex"


def _scan_template(s, i):
    """s[i] is a backtick. Return (end_index, interps) where end_index is just
    past the closing backtick and interps is a list of token-lists, one per
    ${...} interpolation (recursively lexed). Raises on non-termination."""
    n = len(s)
    j = i + 1
    interps = []
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            return j + 1, interps
        if c == "$" and j + 1 < n and s[j + 1] == "{":
            sub, close = _lex(s, j + 2, interp=True)
            interps.append(sub)
            j = close + 1  # step past the interpolation's '}'
            continue
        j += 1
    raise LintError("unterminated template literal")


# Sentinel a token-kind handler returns to signal the '}' that closes the
# enclosing ${...} interpolation: the loop stops WITHOUT consuming that '}'.
_INTERP_CLOSE = object()


@dataclass
class _Cursor:
    """Shared, mutable scan state threaded through the tokenizer's handlers:
    source ``s`` / cursor ``i`` (length ``n``), emitted ``tokens``, the previous
    significant token ``prev`` (drives the regex-vs-division decision), and
    ``brace_depth`` -- the '{' nesting locating the '}' that closes an enclosing
    ${...} (only while ``interp``). Handlers mutate it in place."""

    s: str
    i: int
    n: int
    interp: bool
    tokens: list[Tok] = field(default_factory=list)
    prev: Tok | None = None
    brace_depth: int = 0


def _push(st, tok):
    """Append a significant token and make it the new previous token."""
    st.tokens.append(tok)
    st.prev = tok


def _read_slash(st):
    """Handle '/': line/block comment, regex literal, or division."""
    s, i, n = st.s, st.i, st.n
    # comments (always win over regex/division)
    if i + 1 < n and s[i + 1] == "/":
        j = s.find("\n", i)
        st.i = n if j == -1 else j
        return
    if i + 1 < n and s[i + 1] == "*":
        close = s.find("*/", i + 2)
        if close == -1:
            raise LintError("unterminated block comment")
        st.i = close + 2
        return
    if _slash_context(st.prev) == "regex":
        j = _scan_regex(s, i)
        _push(st, Tok("regex", s[i:j], i, j))
        st.i = j
        return
    if _division_ambiguity(s, i):
        raise LintError(
            "ambiguous '/': reads as both division and a quote-bearing "
            "regex literal, so agent()/workflow() calls around it cannot "
            "be safely verified"
        )
    if i + 1 < n and s[i + 1] == "=":
        _push(st, Tok("punct", "/=", i, i + 2))
        st.i = i + 2
    else:
        _push(st, Tok("punct", "/", i, i + 1))
        st.i = i + 1


def _read_string(st):
    """Handle a ' or " string literal as one opaque token."""
    s, i = st.s, st.i
    j = _scan_string(s, i)
    _push(st, Tok("str", s[i:j], i, j))
    st.i = j


def _read_template(st):
    """Handle a backtick template, recording its ${...} interpolations."""
    s, i = st.s, st.i
    j, interps = _scan_template(s, i)
    _push(st, Tok("template", s[i:j], i, j, interps=interps))
    st.i = j


def _read_open_brace(st):
    """Handle '{': push it and deepen the interpolation brace tracker."""
    st.brace_depth += 1
    _push(st, Tok("punct", "{", st.i, st.i + 1))
    st.i += 1


def _read_close_brace(st):
    """Handle '}'. A '}' at brace_depth 0 inside an interpolation closes the
    enclosing ${...}: return :data:`_INTERP_CLOSE`, leaving the '}' unconsumed for
    _scan_template. Otherwise emit a brace token, unwinding one interpolation
    nesting level when inside it; a stray top-level '}' is tolerated (it cannot
    hide a call)."""
    if st.brace_depth == 0 and st.interp:
        return _INTERP_CLOSE  # closes the enclosing ${...}; '}' left unconsumed
    if st.brace_depth > 0:
        st.brace_depth -= 1
    _push(st, Tok("punct", "}", st.i, st.i + 1))
    st.i += 1
    return None


def _read_paren(st):
    """Handle a '(' or ')' punctuator."""
    _push(st, Tok("punct", st.s[st.i], st.i, st.i + 1))
    st.i += 1


def _read_backslash(_st):
    """A bare backslash cannot legally appear outside a string / template / regex
    (all consumed above); it can only be a \\uXXXX identifier escape JS folds into an
    identifier char (so "\\u0061gent(...)" invokes `agent`), smuggling a call whose
    escaped name never lexes to "agent"/"workflow". Fail closed. (``_st`` unused.)"""
    raise LintError(
        "stray backslash outside a string / regex / template; a "
        "unicode-escaped identifier cannot be statically verified"
    )


def _read_word(st):
    """Handle an identifier / keyword; flag a '.'-prefixed member (a '?.'
    optional chain reduces to that trailing '.')."""
    s, i, n = st.s, st.i, st.n
    j = i + 1
    while j < n and _wordchar(s[j]):
        j += 1
    member = st.prev is not None and st.prev.type == "punct" and st.prev.text == "."
    _push(st, Tok("name", s[i:j], i, j, kind="member" if member else None))
    st.i = j


def _read_punct(st):
    """Handle a generic punctuator, taking the multi-char ++ / -- / => forms
    that affect slash classification before any single char."""
    s, i = st.s, st.i
    nxt = i + 2
    two = s[i:nxt]
    if two in ("++", "--", "=>"):
        _push(st, Tok("punct", two, i, nxt))
        st.i = nxt
        return
    _push(st, Tok("punct", s[i], i, i + 1))
    st.i = i + 1


def _read_rest(st):
    """Fallback for a leading char with no dedicated handler: a numeric literal
    (`.5` included), an identifier / keyword, or a generic punctuator."""
    s, i = st.s, st.i
    c = s[i]
    if c.isdigit() or (c == "." and i + 1 < st.n and s[i + 1].isdigit()):
        j = _scan_number(s, i)  # opaque numeric token
        _push(st, Tok("num", s[i:j], i, j))
        st.i = j
    elif _wordchar(c):
        _read_word(st)
    else:
        _read_punct(st)


# Leading char -> handler; numbers/identifiers/punctuators fall to _read_rest.
# Insignificant whitespace never reaches here -- the driver loop skips it.
_DISPATCH: dict[str, Callable[[_Cursor], object]] = {
    "/": _read_slash,
    '"': _read_string,
    "'": _read_string,
    "`": _read_template,
    "{": _read_open_brace,
    "}": _read_close_brace,
    "(": _read_paren,
    ")": _read_paren,
    "\\": _read_backslash,
}


def _lex(s, start, interp=False):
    """Tokenize s from `start`. Returns (tokens, next_index). When interp is True
    we are inside a ${...}; lexing stops at the '}' with no matching '{' in this
    invocation (its index returned, the '}' NOT consumed, so _scan_template
    resumes the surrounding template there).

    Raises LintError on any unterminated literal / comment or a genuinely
    ambiguous division-context slash; stray top-level closing brackets are
    tolerated (they cannot hide a call). A dispatch loop drives it: the leading
    char is looked up in :data:`_DISPATCH` (else :func:`_read_rest`) and each
    `_read_*` handler mutates the shared :class:`_Cursor`."""
    st = _Cursor(s, start, len(s), interp)
    while st.i < st.n:
        if s[st.i] in " \t\r\n":  # insignificant whitespace: advance, emit nothing
            st.i += 1
            continue
        if _DISPATCH.get(s[st.i], _read_rest)(st) is _INTERP_CLOSE:
            return st.tokens, st.i
    if interp:
        raise LintError("unterminated template interpolation")
    return st.tokens, st.i


if __name__ == "__main__":
    main()
