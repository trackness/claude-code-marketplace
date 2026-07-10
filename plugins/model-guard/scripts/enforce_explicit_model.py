#!/usr/bin/env python3
r"""PreToolUse enforcement hook: every subagent / workflow spawn must name an
explicit, allowed model.

WHY. An omitted model silently inherits the parent session's model; a banned
model (fable / inherit) defeats the "a model chosen for its task" rule. The
hook denies via the PreToolUse permissionDecision schema so the block holds
even under bypassPermissions, and the reason is fed back so the model retries
with an explicit model in the same turn.

SHAPE. The whole program is one total function -- `decide(payload) -> Decision`
-- wrapped by a thin `main` that reads stdin, emits the decision, and exits 0.
A Decision is deny / ask / abstain; only deny and ask print JSON, abstain is
silent (normal permission flow proceeds). Deciding never touches stdout, so the
policy stays a pure value computation and emission happens in exactly one place.

Tools handled (matcher "Agent|Task|Workflow"):
  Agent / Task : deny unless tool_input.model is an explicit non-banned model,
                 or the subagent_type's agent .md frontmatter pins one.
  Workflow     : statically lint the script TEXT; every agent() call must pass
                 a top-level model: key in its options object. Fail-closed:
                 anything the lint cannot positively verify is denied. Saved /
                 bundled workflows invoked by name or resume id cannot be
                 inspected -> "ask" (or deny under the strict env flag).

LEXING MODEL. The workflow lint runs on a real ECMAScript token stream, not a
raw-string scan, implementing the InputElementDiv / InputElementRegExp goal
distinction via the standard PREVIOUS-SIGNIFICANT-TOKEN rule (as acorn/esprima
do), because whether a '/' is division or a regex decides whether a following
agent() call stays a visible token. _slash_context holds the full rule; in
brief, a '/' is DIVISION after anything that ends an expression and a REGEX only
where the grammar demands a fresh one, and '//' / '/*' are always comments.

FAIL-CLOSED GUARDS (fatal-direction preservation). Correct lexing of adversarial
input can still hide a spawn, so two nets fail closed to a LintError -> deny:
  * Quote-bearing ambiguity. `foo /a'/ agent('x', {t:1})` lexes with the agent(
    swallowed inside a string. Every division-slash is rechecked
    (_division_ambiguity): if it also admits a single-line regex reading whose
    span carries an unbalanced quote / backtick, the two lexings disagree.
  * Stray backslash. A bare '\' in the main stream is a \uXXXX identifier escape
    (agent -> agent) whose escaped name never lexes to "agent"/"workflow".

All downstream logic (call detection, argument splitting, depth-1 model key
scan, value classification) operates on the token stream; template
interpolations are tokenized recursively and linted independently.

stdlib only; python3 >= 3.14. Unparsable stdin abstains (exit 0) so a
malformed payload never wedges a session. The hook always exits 0.
"""

import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

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


# =========================================================================
# decision model + emission
# =========================================================================
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
    """Render a Decision to the PreToolUse contract. deny / ask print the
    hookSpecificOutput JSON; abstain prints nothing."""
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


# =========================================================================
# entry point + dispatch
# =========================================================================
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


# =========================================================================
# Agent / Task policy
# =========================================================================
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


# =========================================================================
# frontmatter pin lookup
# =========================================================================
_FM_MODEL_RE = re.compile(r"(?im)^\s*model\s*:\s*[\"']?([A-Za-z0-9._\[\]-]+)")


def frontmatter_model(cwd, subagent_type):
    """Walk UP from cwd through every parent directory checking
    <dir>/.claude/agents/<type>.md, then ~/.claude/agents/<type>.md. Return the
    first pinned model found, else None. A subagent_type carrying a path
    separator (or . / ..) is refused so no pin is read from outside an agents
    directory."""
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
    """Return the `model:` pinned in `path`'s YAML frontmatter, else None. Only
    the leading `--- ... ---` block is searched; an unreadable file is None."""
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


# =========================================================================
# Workflow policy
# =========================================================================
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
        except OSError as e:
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


# =========================================================================
# Workflow static lint (token stream)
# =========================================================================
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
    kind: set to 'member' on a name that is a property access (its previous
          significant token is a '.', which the '?.' optional chain also reduces
          to since '?.' lexes as '?' then '.'), so a keyword used as a PROPERTY --
          obj.of, gen.return, p.catch -- is treated as the value it is (a
          following '/' is division; a following '(' is an ordinary call, not a
          control-flow head), never as a regex-prefix / control keyword. None
          everywhere else.
    interps: only set on a template token: the list of token-lists, one per
             ${...} interpolation, so calls inside interpolations are lintable.
    """

    type: str
    text: str
    start: int
    end: int
    kind: str | None = None
    interps: list[list[Tok]] | None = None


@dataclass(slots=True)
class LintCtx:
    """Shared context threaded through the token-stream lint: the token list
    being walked, the raw source (for deny snippets), and the accumulating
    problem list. Interpolations are walked with a fresh context over their own
    token list but the same raw source and problem list."""

    tokens: list[Tok]
    raw: str
    problems: list[str] = field(default_factory=list)


def lint_workflow(script):
    """Return a list of problem descriptions; empty means clean. Raises
    LintError on any structural anomaly (-> caller denies, fail-closed)."""
    tokens, _ = _lex(script, 0, interp=False)
    ctx = LintCtx(tokens, script)
    _lint_tokens(ctx)
    return ctx.problems


def _lint_tokens(ctx):
    """Walk a token stream: lint every agent() call, flag every nested
    workflow() call, and recurse into template interpolations. The walk does NOT
    skip a call's arguments, so nested agent()/workflow() calls inside argument
    positions are found too (matching the reference, which matched agent(
    anywhere)."""
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
            continue  # member access: obj.agent(...)
        if not (
            i + 1 < n and tokens[i + 1].type == "punct" and tokens[i + 1].text == "("
        ):
            continue  # not a call
        open_i = i + 1
        close_i = _match_bracket(tokens, open_i)
        if tok.text == "workflow":
            snip = _snippet(ctx.raw, tok.start, tokens[close_i].end)
            ctx.problems.append(
                f"nested workflow() call cannot be statically verified for "
                f"explicit per-agent models -> {snip}"
            )
            continue
        _lint_call(ctx, i, open_i, close_i)


def _lint_call(ctx, name_i, open_i, close_i):
    """Lint one agent() call given its name / '(' / ')' token indices."""
    tokens = ctx.tokens
    snip = _snippet(ctx.raw, tokens[name_i].start, tokens[close_i].end)
    args = _top_level_split(tokens, open_i + 1, close_i)
    if len(args) < _MIN_AGENT_ARGS or not args[1]:
        ctx.problems.append(
            f"agent() call has no options object (second argument), so no "
            f"explicit model can be set -> {snip}"
        )
        return
    present, value_tokens = _object_model(args[1])
    if not present:
        ctx.problems.append(
            f"agent() options object has no top-level model: key -> {snip}"
        )
        return
    status, disp = _classify_model_value(value_tokens)
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


def _object_model(opts):
    """opts is the token-slice of an agent() call's second argument. Return
    (present, value_tokens): present is True iff opts is a brace object literal
    with a DEPTH-1 `model` entry (its key in KEY position, since entries are the
    top-level comma-split of the object). A `model` nested deeper, or one in a
    value position (`cond ? model : x`), is not a top-level entry key.

    Each entry is the [key, colon-separator, *value] shape a `key: value` binding
    tokenizes to; the model entry is matched by that shape and its value tokens
    (everything past the ':') returned directly."""
    if not _opens_object(opts):
        return False, None
    close = _match_bracket(opts, 0)
    for entry in _top_level_split(opts, 1, close):
        match entry:
            case [key, sep, *value] if (
                _is_model_key(key) and sep.type == "punct" and sep.text == ":"
            ):
                return True, value
    return False, None


def _opens_object(opts):
    """True iff the token-slice begins with a '{' (an object literal opener)."""
    return bool(opts) and opts[0].type == "punct" and opts[0].text == "{"


def _is_model_key(t):
    """A key token naming `model`: the bareword `model`, or a quoted 'model' /
    "model"."""
    if t.type == "name":
        return t.text == "model"
    if t.type == "str":
        return _str_inner(t.text) == "model"
    return False


def _classify_model_value(value_tokens):
    """Classify a top-level model key's value. Returns:
    ("banned", display) - a literal naming a BANNED_MODELS entry,
    ("blank",  "")      - a literal that is empty / all whitespace,
    ("ok",     None)    - a valid literal, OR a non-literal (dynamic) value the
                          static lint cannot resolve (treated as satisfying)."""
    lit = _literal_string(value_tokens)
    if lit is None:
        return "ok", None
    norm = lit.strip().lower()
    if norm == "":
        return "blank", ""
    if norm in BANNED_MODELS:
        return "banned", lit.strip()
    return "ok", None


def _literal_string(value_tokens):
    """If value_tokens is exactly one string literal, or a backtick template
    with no interpolation, return its inner text; else None (identifier, call,
    concatenation, interpolated template -> not a statically-resolvable
    literal)."""
    if len(value_tokens) != 1:
        return None
    t = value_tokens[0]
    if t.type == "str":
        return _str_inner(t.text)
    if t.type == "template" and not t.interps:
        return t.text[1:-1]  # strip backticks
    return None


def _top_level_split(tokens, start, stop):
    """Split tokens[start:stop] on top-level (depth-0) commas, tracking () [] {}
    nesting. Returns one token-list segment per item; a region with no items
    still yields a single empty segment (so an empty call reads as one empty
    argument). This is the whole grammar the lint needs: a call's arguments and
    an object literal's entries are both depth-0 comma-separated lists."""
    segments = []
    segment = []
    depth = 0
    for k in range(start, stop):
        tok = tokens[k]
        if tok.type == "punct":
            if tok.text in ("(", "[", "{"):
                depth += 1
            elif tok.text in (")", "]", "}"):
                depth -= 1
            elif tok.text == "," and depth == 0:
                segments.append(segment)
                segment = []
                continue
        segment.append(tok)
    segments.append(segment)
    return segments


def _match_bracket(tokens, open_i):
    """tokens[open_i] is an opening '(' or '{'. Return the index of the matching
    closer, counting only that bracket kind (interior strings are opaque single
    tokens, so the other bracket kinds cannot stray). Raises on imbalance."""
    opener = tokens[open_i].text
    closer, name = _BRACKETS[opener]
    depth = 0
    for k in range(open_i, len(tokens)):
        t = tokens[k]
        if t.type == "punct":
            if t.text == opener:
                depth += 1
            elif t.text == closer:
                depth -= 1
                if depth == 0:
                    return k
    raise LintError(f"unbalanced {name}")


_BRACKETS = {"(": (")", "parentheses"), "{": ("}", "braces")}


def _snippet(raw, start, end):
    """A whitespace-collapsed, length-capped slice of the raw source for a deny
    reason, so the offending call is quoted back readably."""
    return re.sub(r"\s+", " ", raw[start:end]).strip()[:120]


def _str_inner(text):
    """Inner text of a '...' / "..." token (raw, no unescaping -- model names
    carry no escapes)."""
    return text[1:-1]


# =========================================================================
# ECMAScript lexer
# =========================================================================
# Reserved beforeExpr words: a word that CANNOT end an expression, so a '/'
# after it opens a regex, not division. Any other word (a plain identifier or a
# value keyword such as this / true / null) ends an expression -> its '/' is
# division. The contextual keywords of / yield / await are DELIBERATELY excluded:
# each can also be an identifier (`let of = 1`) or a property, so it ends an
# expression and its '/' is division -- else a model-less `of / agent('t', {})
# / 2` would be swallowed whole as a regex and allowed.
_REGEX_KEYWORDS = frozenset(
    {
        "return",
        "typeof",
        "instanceof",
        "in",
        "new",
        "delete",
        "void",
        "throw",
        "case",
        "do",
        "else",
        "default",
        "extends",
    }
)

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
    """s[i] is '/', already known to start a regex literal. Return index past
    the closing '/'. Handles escapes and [ ] character classes (a '/' inside a
    class is literal). Trailing flags are left for the next token (an identifier
    read); this matches the reference and never hides a call. Raises on
    non-termination (a regex cannot cross a newline)."""
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
    to end the number so a following '/' is division and the next identifier is
    not swallowed."""
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
    newline) whose span carries an UNBALANCED quote / backtick.

    Such a slash is the fatal ambiguity: the regex reading skips the quote, but
    the division reading treats it as a string delimiter that pairs across the
    intervening code -- exactly how an adversarial `foo /a'/ agent(...)` hides a
    model-less spawn. A balanced span is safe to read as division (its strings
    stay local and cannot swallow a later call), so ordinary one-line arithmetic
    like `a / b + "x" / c` is NOT flagged."""
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
    """Classify a '/' by the previous significant token. Returns 'regex' or
    'div'.

    A '/' is DIVISION after anything that can END an expression, because only
    then does what follows keep executing as code (so a real call after the
    slash must stay visible to the lint): a value literal, an identifier, a
    keyword used as a property (obj.of), or a closing ) ] } (a call / group /
    member / block or object close). It begins a REGEX only in positions that
    demand a fresh expression: start of input, after an operator or opener, or
    after a reserved beforeExpr keyword.

    The ) and } cases are the genuinely context-dependent ones. We resolve them
    as DIVISION -- the fatal-safe reading the certified reference used: a
    division-classified slash never hides a following call (it is lexed as
    ordinary code), and the _division_ambiguity net still fails closed on the
    adversarial quote-bearing form. A legitimate statement-position regex after
    a '}' or ')' is rare and, unless it carries an unbalanced quote, still lexes
    to harmless tokens."""
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
    source ``s`` and cursor ``i`` (length ``n``), emitted ``tokens``, the
    previous significant token ``prev`` (drives the regex-vs-division decision),
    and ``brace_depth`` -- the '{' nesting that locates the '}' closing an
    enclosing ${...} (present only while ``interp``). Handlers mutate it in
    place, so no one function carries the whole branchy state machine."""

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
    enclosing ${...}: return :data:`_INTERP_CLOSE` and leave the '}' unconsumed
    for _scan_template to resume on. Otherwise emit a brace token, unwinding one
    level of interpolation nesting when inside it; a stray top-level '}' is
    tolerated -- it cannot hide a call, matching the reference."""
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
    """A bare backslash cannot legally appear outside a string / template /
    regex (all consumed above); it can only be a \\uXXXX identifier escape JS
    folds into an identifier char (so "\\u0061gent(...)" invokes `agent`),
    smuggling a call whose escaped name never lexes to "agent"/"workflow". Fail
    closed. (``_st``: the cursor is unused -- this handler only ever fails.)"""
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
    """Tokenize s from `start`. Returns (tokens, next_index).

    When interp is True we are inside a ${...}; lexing stops at the '}' that has
    no matching '{' in this invocation (its index is returned, the '}' NOT
    consumed, so _scan_template can resume the surrounding template there).

    Raises LintError on any unterminated literal / comment, or on a genuinely
    ambiguous division-context slash. Stray closing brackets at the top level are
    tolerated (they cannot hide a call), matching the reference's leniency.

    A slim dispatch loop drives the state machine: it looks up the leading char
    in :data:`_DISPATCH` (else :func:`_read_rest`), so each single-purpose
    `_read_*` handler mutates the shared :class:`_Cursor`; the regex-vs-division
    decision lives in :func:`_read_slash`."""
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
