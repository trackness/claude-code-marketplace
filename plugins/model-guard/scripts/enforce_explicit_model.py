#!/usr/bin/env python3
# =============================================================================
# PreToolUse enforcement hook: every subagent / workflow spawn must name an
# explicit, allowed model.
#
# WHY. An omitted model silently inherits the parent session's model; a banned
# model (fable / inherit) defeats the "a model chosen for its task" rule. The
# hook denies via the PreToolUse permissionDecision schema so the block holds
# even under bypassPermissions, and the reason is fed back so the model retries
# with an explicit model in the same turn.
#
# Tools handled (matcher "Agent|Task|Workflow"):
#   Agent / Task : deny unless tool_input.model is an explicit non-banned model,
#                  or the subagent_type's agent .md frontmatter pins one.
#   Workflow     : statically lint the script TEXT; every agent() call must pass
#                  a top-level model: key in its options object. Fail-closed:
#                  anything the lint cannot positively verify is denied. Saved /
#                  bundled workflows invoked by name or resume id cannot be
#                  inspected -> "ask" (or deny under the strict env flag).
#
# -----------------------------------------------------------------------------
# LEXING MODEL (this is the rebuild's substance).
#
# The workflow lint used to run on the RAW string via character-index scanning:
# it stripped comments into a length-preserving copy, blanked string contents
# into a second copy, then matched agent( with a regex on the blanked copy and
# re-indexed back into the first copy. The one genuinely hard sub-problem --
# does a '/' start a regex literal or a division -- was answered by a quote
# PARITY heuristic: a division-context slash was denied whenever the span up to
# the next same-line '/' carried an odd number of a quote char. That is sound in
# the fatal direction but OVER-denies: any legitimate regex sitting in a value
# position (e.g. `if (x) /it's/`) tripped the parity check and was rejected even
# though every spawn in the script was correctly modeled.
#
# This version replaces the scanning + parity heuristic with a real ECMAScript
# lexical tokenizer. It implements the InputElementDiv / InputElementRegExp goal
# distinction the spec defines, using the standard PREVIOUS-SIGNIFICANT-TOKEN
# rule that production tokenizers (acorn, esprima) use:
#
#   * A '/' is DIVISION after anything that can END an expression -- and only
#     then does what follows keep executing as code, so a real call after the
#     slash MUST stay visible to the lint. That covers a value literal (number /
#     string / regex / template), an identifier, a keyword used as a PROPERTY
#     (obj.of, gen.return -- a '.'/'?.'-prefixed name is a member value, not a
#     regex-prefix keyword), a closing ) ] } (a call / group / member / block or
#     object close), or a postfix '++' / '--'.
#   * A '/' begins a REGEX only where the grammar demands a fresh expression:
#     start of input, after any operator or opener, or after a reserved beforeExpr
#     keyword that cannot end an expression (return, typeof, instanceof, in, new,
#     delete, void, throw, case, do, else, default, extends). The CONTEXTUAL
#     keywords of / yield / await are intentionally NOT in that set: each can be
#     an identifier or property that ends an expression, so their '/' is division
#     (see _REGEX_KEYWORDS) -- otherwise a model-less `of / agent('t', {}) / 2`
#     would be swallowed as a regex.
#   * '//' and '/*' are ALWAYS comments (an empty regex // is disallowed, and a
#     regex body cannot begin with '*'), so comments are matched before either
#     reading -- unambiguous.
#
# THE ) AND } POSITIONS (genuinely context-dependent).
# A '/' after ')' or '}' can, in valid code, be either a division (after a call /
# grouping / object literal) or a statement-position regex (after a control head
# or a block). A pure previous-token lexer cannot settle which. We resolve BOTH
# as DIVISION -- the fatal-safe reading the certified reference used. Reasoning:
# a division-classified slash never hides a following call (the rest is lexed as
# ordinary code, so any agent()/workflow() stays a visible token and is linted),
# and the _division_ambiguity net below still fails closed on the adversarial
# quote-bearing form. A genuine statement-position regex after '}' or ')' is
# rare; unless it carries an unbalanced quote it lexes to harmless tokens, and
# when it does carry one it fails closed exactly as the reference did.
#
# THE FAIL-CLOSED GUARDS (fatal-direction preservation).
#   * Quote-bearing ambiguity. Correct ECMA lexing of an ADVERSARIAL script can
#     still hide a spawn: `foo /a'/ agent('x', {t:1})` lexes, per spec, as `foo`
#     '/' `a` then a STRING `'/ agent('` -- the agent( vanishes inside a string.
#     The script is invalid JS (it would never run), but we must still deny it
#     rather than silently allow the model-less spawn it smuggles. So EVERY slash
#     we classify as division is checked once: if it also admits a single-line
#     regex reading (an unescaped '/' before the next newline) AND the span
#     between the two slashes carries an unbalanced quote / backtick, the two
#     lexings DISAGREE about where strings begin around any following call and we
#     cannot soundly pick -> LintError -> deny. This is the only surviving use of
#     quote counting, a localized fatal-safety net, not the primary mechanism.
#   * Stray backslash. A '\' can legally occur only inside a string / template /
#     regex (all consumed by their scanners). A bare '\' in the main stream is a
#     \uXXXX identifier escape (agent -> agent) trying to smuggle a call whose
#     escaped name would never lex to "agent"/"workflow" -> LintError -> deny.
#
# ONE-WAY CONTRACT vs the old heuristic.
#   * Fatal direction preserved: every payload the reference denied because it
#     contains a model-less / blank / banned / unverifiable spawn still denies,
#     and genuine structural anomalies (invalid JS around a slash) still fail
#     closed.
#   * Permitted improvement: payloads the reference OVER-denied purely because
#     its raw agent( scan matched a call-shape sitting INSIDE a genuine regex
#     literal (a value-position `/.../` its parity heuristic could not clear) now
#     ALLOW -- because the tokenizer reads that regex as one opaque token AND
#     every spawn that actually executes in them carries an explicit allowed
#     literal model.
#
# ALL downstream logic (call-site detection, argument splitting, depth-1 model
# key scan, value classification) operates on the TOKEN STREAM. There are no
# blanked intermediate texts and no cross-indexed derived strings. Template
# interpolations are tokenized recursively; an agent()/workflow() call inside an
# interpolation is linted independently, exactly as the reference did.
#
# stdlib only; python3 >= 3.9. Unparseable stdin abstains (exit 0) so a
# malformed payload never wedges a session. The hook always exits 0.
# =============================================================================

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
    """Raised on any structural anomaly the lint cannot resolve soundly:
    unterminated string / template / comment / regex, an unbalanced bracket
    around a call, or a genuinely ambiguous division-vs-regex slash. Every
    LintError maps to a deny (fail-closed)."""


# ---- output helpers -----------------------------------------------------
def _emit(decision, reason):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def deny(reason):
    _emit("deny", reason)


def ask(reason):
    _emit("ask", reason)


def abstain():
    # Emit nothing -> hook abstains, normal permission flow proceeds.
    sys.exit(0)


# =========================================================================
# ECMAScript lexer
# =========================================================================

# Reserved words after which a '/' begins a regex literal, not division -- the
# beforeExpr set: words that CANNOT end an expression, so what follows is a fresh
# expression (a regex is valid there). Anything NOT in this set that reads as a
# word (a plain identifier, or a value keyword such as this / true / null) CAN
# end an expression, so a following '/' is division.
#
# The contextual keywords of / yield / await are DELIBERATELY excluded. Each can
# also be an identifier (a sloppy-mode binding: `let of = 1`) or a property, so
# it CAN end an expression, which makes a following '/' a division. If their
# slash were classed regex, `of / agent('t', {}) / 2` -- a real, model-less
# spawn whenever `of` is an identifier -- would be swallowed whole as a regex
# literal and silently allowed. Treating them as expression-enders routes the
# slash through the division path, which exposes the call to the lint (and the
# _division_ambiguity net still guards the quote-bearing adversarial form). A
# genuine regex right after one of these words is rare and, unless it carries an
# unbalanced quote, still lints cleanly.
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


class Tok:
    """A significant token. Whitespace and comments are never emitted.

    type: one of str, template, regex, num, name, punct.
    text: the raw source slice (delimiters / operator chars included).
    start, end: source offsets (end exclusive) for RAW-source deny snippets.
    kind: set to 'member' on a name that is a property access (its previous
          significant token is '.' / '?.'), so a keyword used as a PROPERTY --
          obj.of, gen.return, p.catch -- is treated as the value it is (a
          following '/' is division; a following '(' is an ordinary call, not a
          control-flow head), never as a regex-prefix / control keyword. None
          everywhere else.
    interps: only set on a template token: the list of token-lists, one per
             ${...} interpolation, so calls inside interpolations are lintable.
    """

    # __slots__ deliberately mirror the __init__ parameter order
    # (type/text/start/end/kind/interps); that reads clearer here than the
    # alphabetical order RUF023 wants.
    __slots__ = ("type", "text", "start", "end", "kind", "interps")  # noqa: RUF023

    def __init__(self, type, text, start, end, kind=None, interps=None):
        self.type = type
        self.text = text
        self.start = start
        self.end = end
        self.kind = kind
        self.interps = interps


def _wordchar(c):
    return c.isalnum() or c in "_$"


# ---- literal scanners (return the index just past the literal) ----------
def _skip_quoted(s, i):
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


def _skip_regex(s, i):
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


def _read_number(s, i):
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
    t = prev.type
    if t in ("num", "str", "regex", "template"):
        return "div"  # a value that ends an expression
    if t == "name":
        if prev.kind == "member":
            return "div"  # obj.<keyword> is a property value
        return "regex" if prev.text in _REGEX_KEYWORDS else "div"
    # punctuator
    p = prev.text
    if p in (")", "]", "}"):
        return "div"  # a call / group / member / close
    if p in ("++", "--"):
        return "div"  # postfix update ends an expression
    return "regex"  # any other operator / punctuator


def _read_template(s, i):
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


def _lex(s, start, interp=False):
    """Tokenize s from `start`. Returns (tokens, next_index).

    When interp is True we are inside a ${...}; lexing stops at the '}' that has
    no matching '{' in this invocation (its index is returned, the '}' NOT
    consumed, so _read_template can resume the surrounding template there).

    Raises LintError on any unterminated literal / comment, or on a genuinely
    ambiguous division-context slash. Stray closing brackets at the top level are
    tolerated (they cannot hide a call), matching the reference's leniency."""
    tokens = []
    i = 0 if start is None else start
    n = len(s)
    prev = None
    brace_depth = 0  # tracks '{' depth for interp exit

    def push(tok):
        tokens.append(tok)
        return tok

    while i < n:
        c = s[i]
        if c in " \t\r\n":
            i += 1
            continue
        # comments (always win over regex/division)
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            j = s.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            close = s.find("*/", i + 2)
            if close == -1:
                raise LintError("unterminated block comment")
            i = close + 2
            continue
        # string
        if c in "\"'":
            j = _skip_quoted(s, i)
            prev = push(Tok("str", s[i:j], i, j))
            i = j
            continue
        # template (with recursive interpolations)
        if c == "`":
            j, interps = _read_template(s, i)
            prev = push(Tok("template", s[i:j], i, j, interps=interps))
            i = j
            continue
        # slash: regex literal or division
        if c == "/":
            if _slash_context(prev) == "regex":
                j = _skip_regex(s, i)
                prev = push(Tok("regex", s[i:j], i, j))
                i = j
                continue
            if _division_ambiguity(s, i):
                raise LintError(
                    "ambiguous '/': reads as both division and a quote-bearing "
                    "regex literal, so agent()/workflow() calls around it cannot "
                    "be safely verified"
                )
            if i + 1 < n and s[i + 1] == "=":
                prev = push(Tok("punct", "/=", i, i + 2))
                i += 2
            else:
                prev = push(Tok("punct", "/", i, i + 1))
                i += 1
            continue
        # number
        if c.isdigit() or (c == "." and i + 1 < n and s[i + 1].isdigit()):
            j = _read_number(s, i)
            prev = push(Tok("num", s[i:j], i, j))
            i = j
            continue
        # identifier / keyword ('.'/'?.'-prefixed -> a member/property value)
        if _wordchar(c) and not c.isdigit():
            j = i + 1
            while j < n and _wordchar(s[j]):
                j += 1
            member = prev is not None and prev.type == "punct" and prev.text == "."
            prev = push(Tok("name", s[i:j], i, j, kind="member" if member else None))
            i = j
            continue
        # braces: track depth so an interpolation ends at its unmatched '}'
        if c == "{":
            brace_depth += 1
            prev = push(Tok("punct", "{", i, i + 1))
            i += 1
            continue
        if c == "}":
            if brace_depth == 0:
                if interp:
                    return tokens, i  # closes the enclosing ${...}
                # stray top-level close: tolerate (cannot hide a call)
                prev = push(Tok("punct", "}", i, i + 1))
                i += 1
                continue
            brace_depth -= 1
            prev = push(Tok("punct", "}", i, i + 1))
            i += 1
            continue
        if c in "()":
            prev = push(Tok("punct", c, i, i + 1))
            i += 1
            continue
        # A bare backslash cannot legally appear outside a string / template /
        # regex (all consumed above). The only way one reaches here is a
        # \\uXXXX identifier escape, which JS folds into an identifier char
        # (so "\\u0061gent(...)" invokes `agent`) -- smuggling a call past the
        # lexer: the escaped name never lexes to "agent"/"workflow", so the call
        # would slip through unverified. Fail closed.
        if c == "\\":
            raise LintError(
                "stray backslash outside a string / regex / template; a "
                "unicode-escaped identifier cannot be statically verified"
            )
        # multi-char punctuators that affect slash classification
        two = s[i : i + 2]
        if two in ("++", "--", "=>"):
            prev = push(Tok("punct", two, i, i + 2))
            i += 2
            continue
        # any other single punctuator char
        prev = push(Tok("punct", c, i, i + 1))
        i += 1
        continue

    if interp:
        raise LintError("unterminated template interpolation")
    return tokens, i


# =========================================================================
# Token-stream lint
# =========================================================================
def _snippet(raw, start, end):
    return re.sub(r"\s+", " ", raw[start:end]).strip()[:120]


def _str_inner(text):
    """Inner text of a '...' / "..." token (raw, no unescaping -- model names
    carry no escapes)."""
    return text[1:-1]


def _match_paren(tokens, open_i):
    """tokens[open_i] is a '(' punct. Return the index of the matching ')'."""
    depth = 0
    for k in range(open_i, len(tokens)):
        t = tokens[k]
        if t.type == "punct":
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth -= 1
                if depth == 0:
                    return k
    raise LintError("unbalanced parentheses")


def _match_brace(toks, open_i):
    """toks[open_i] is a '{' punct. Return the index of the matching '}'."""
    depth = 0
    for k in range(open_i, len(toks)):
        t = toks[k]
        if t.type == "punct":
            if t.text == "{":
                depth += 1
            elif t.text == "}":
                depth -= 1
                if depth == 0:
                    return k
    raise LintError("unbalanced braces")


def _split_args(tokens, open_i, close_i):
    """Split the arguments in tokens[open_i+1:close_i] on top-level commas,
    tracking () [] {} depth. Returns a list of token-list segments (one per
    argument; an empty call yields a single empty segment)."""
    args = []
    seg = []
    depth = 0
    for k in range(open_i + 1, close_i):
        t = tokens[k]
        if t.type == "punct":
            if t.text in ("(", "[", "{"):
                depth += 1
                seg.append(t)
                continue
            if t.text in (")", "]", "}"):
                depth -= 1
                seg.append(t)
                continue
            if t.text == "," and depth == 0:
                args.append(seg)
                seg = []
                continue
        seg.append(t)
    args.append(seg)
    return args


def _is_model_key(t):
    """A depth-1 key token naming `model`: the bareword `model`, or a quoted
    'model' / "model" key."""
    if t.type == "name":
        return t.text == "model"
    if t.type == "str":
        return _str_inner(t.text) == "model"
    return False


def _extract_value(toks, start, close):
    """Value token-slice beginning at index `start`, ending at the next depth-0
    ',' or the object close index. Tracks () [] {} depth."""
    out = []
    depth = 0
    k = start
    while k < close:
        t = toks[k]
        if t.type == "punct":
            if t.text in ("(", "[", "{"):
                depth += 1
                out.append(t)
                k += 1
                continue
            if t.text in (")", "]", "}"):
                depth -= 1
                out.append(t)
                k += 1
                continue
            if t.text == "," and depth == 0:
                break
        out.append(t)
        k += 1
    return out


def _object_model(opts):
    """opts is the token-slice of an agent() call's second argument. Return
    (present, value_tokens): present is True iff opts is a brace object literal
    whose DEPTH-1 keys include a `model` key in KEY position (the previous
    significant token is the opening '{' or a property-separating ','). A `model`
    nested deeper, or one sitting in value position (a ternary's true branch,
    `cond ? model : x`), does not count."""
    if not opts or not (opts[0].type == "punct" and opts[0].text == "{"):
        return (False, None)
    close = _match_brace(opts, 0)
    prev = opts[0]  # the opening '{'
    depth = 0
    k = 1
    while k < close:
        t = opts[k]
        if t.type == "punct" and t.text in ("(", "[", "{"):
            depth += 1
            prev = t
            k += 1
            continue
        if t.type == "punct" and t.text in (")", "]", "}"):
            depth -= 1
            prev = t
            k += 1
            continue
        if (
            depth == 0
            and _is_model_key(t)
            and prev.type == "punct"
            and prev.text in ("{", ",")
            and k + 1 < close
            and opts[k + 1].type == "punct"
            and opts[k + 1].text == ":"
        ):
            return (True, _extract_value(opts, k + 2, close))
        prev = t
        k += 1
    return (False, None)


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


def _classify_model_value(value_tokens):
    """Classify a top-level model key's value. Returns:
    ("banned", display) - a literal naming a BANNED_MODELS entry,
    ("blank",  "")      - a literal that is empty / all whitespace,
    ("ok",     None)    - a valid literal, OR a non-literal (dynamic) value the
                          static lint cannot resolve (treated as satisfying)."""
    lit = _literal_string(value_tokens)
    if lit is None:
        return ("ok", None)
    norm = lit.strip().lower()
    if norm == "":
        return ("blank", "")
    if norm in BANNED_MODELS:
        return ("banned", lit.strip())
    return ("ok", None)


def _lint_call(tokens, name_i, open_i, close_i, raw, problems):
    """Lint one agent() call given its name / '(' / ')' token indices."""
    name_tok = tokens[name_i]
    close_tok = tokens[close_i]
    snip = _snippet(raw, name_tok.start, close_tok.end)
    args = _split_args(tokens, open_i, close_i)
    if len(args) < 2 or not args[1]:
        problems.append(
            "agent() call has no options object (second argument), so no "
            "explicit model can be set -> %s" % snip
        )
        return
    present, value_tokens = _object_model(args[1])
    if not present:
        problems.append(
            "agent() options object has no top-level model: key -> %s" % snip
        )
        return
    status, disp = _classify_model_value(value_tokens)
    if status == "banned":
        problems.append(
            "agent() options pin a banned model %r; it must be one of %s -> %s"
            % (disp, VALID_CHOICES_STR, snip)
        )
    elif status == "blank":
        problems.append(
            "agent() options set a blank model; it must be one of %s -> %s"
            % (VALID_CHOICES_STR, snip)
        )


def _lint_tokens(tokens, raw, problems):
    """Walk a token stream: lint every agent() call, flag every nested
    workflow() call, and recurse into template interpolations. The walk does NOT
    skip a call's arguments, so nested agent()/workflow() calls inside argument
    positions are found too (matching the reference, which matched agent(
    anywhere)."""
    n = len(tokens)
    for i in range(n):
        tok = tokens[i]
        if tok.type == "template":
            if tok.interps:
                for sub in tok.interps:
                    _lint_tokens(sub, raw, problems)
            continue
        if tok.type != "name" or tok.text not in ("agent", "workflow"):
            continue
        prevtok = tokens[i - 1] if i > 0 else None
        if (
            prevtok is not None
            and prevtok.type == "punct"
            and prevtok.text in (".", "?.")
        ):
            continue  # member access: obj.agent(...)
        if not (
            i + 1 < n and tokens[i + 1].type == "punct" and tokens[i + 1].text == "("
        ):
            continue  # not a call
        open_i = i + 1
        close_i = _match_paren(tokens, open_i)
        if tok.text == "workflow":
            problems.append(
                "nested workflow() call cannot be statically verified for "
                "explicit per-agent models -> %s"
                % _snippet(raw, tok.start, tokens[close_i].end)
            )
            continue
        _lint_call(tokens, i, open_i, close_i, raw, problems)


def lint_workflow(script):
    """Return a list of problem descriptions; empty means clean. Raises
    LintError on any structural anomaly (-> caller denies, fail-closed)."""
    tokens, _ = _lex(script, 0, interp=False)
    problems = []
    _lint_tokens(tokens, script, problems)
    return problems


# =========================================================================
# frontmatter pin lookup
# =========================================================================
_FM_MODEL_RE = re.compile(r"(?im)^\s*model\s*:\s*[\"']?([A-Za-z0-9._\[\]-]+)")


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
        candidates.append(os.path.join(d, ".claude", "agents", subagent_type + ".md"))
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    candidates.append(
        os.path.join(
            os.path.expanduser("~"), ".claude", "agents", subagent_type + ".md"
        )
    )
    for p in candidates:
        pinned = _read_frontmatter_model(p)
        if pinned:
            return pinned
    return None


# =========================================================================
# per-tool handlers
# =========================================================================
def handle_agent(ti, cwd):
    model = ti.get("model")
    if isinstance(model, str) and model.strip():
        norm = model.strip().lower()
        if norm in BANNED_MODELS:
            deny(
                "Model %r is banned for subagents: it lets the agent inherit "
                "or silently downgrade instead of running a model chosen for "
                "the task. Re-issue the Agent call with an explicit model "
                "(one of %s)." % (model.strip(), VALID_CHOICES_STR)
            )
        abstain()  # explicit, allowed model -> proceed
    # model omitted or blank
    if ALLOW_FRONTMATTER_PIN:
        st = ti.get("subagent_type")
        pinned = frontmatter_model(cwd, st)
        if pinned:
            if pinned.strip().lower() in BANNED_MODELS:
                deny(
                    "subagent_type %r pins a banned model %r in its "
                    "frontmatter. Set an explicit model at the call site "
                    "(one of %s)." % (st, pinned, VALID_CHOICES_STR)
                )
            abstain()  # a frontmatter pin is an explicit per-task choice
    deny(
        "This subagent spawn omits an explicit `model`, so it would silently "
        "inherit the parent session's model. Re-issue the Agent call with an "
        "explicit model parameter (one of %s)." % VALID_CHOICES_STR
    )


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
                deny(
                    "Workflow scriptPath %r could not be read to verify "
                    "explicit per-agent models (%s). Pass the script inline "
                    "so every agent() model can be checked." % (sp, e)
                )
        elif ti.get("name") is not None or ti.get("resumeFromRunId") is not None:
            msg = (
                "This workflow is invoked by name / resume id, so its saved "
                "script cannot be statically inspected for explicit "
                "per-agent models. Approve only if you trust it; otherwise "
                "re-invoke Workflow with an inline `script` whose every "
                "agent() call sets a top-level model:."
            )
            if STRICT_SAVED_WORKFLOWS:
                deny(msg + " (STRICT_SAVED_WORKFLOWS is on, so it is denied.)")
            ask(msg)
        else:
            deny(
                "Workflow payload has none of script / scriptPath / name; "
                "nothing can be statically verified, so it is denied."
            )
    try:
        problems = lint_workflow(script)
    except LintError as e:
        deny(
            "Workflow script could not be safely parsed (%s), so its agent() "
            "spawns cannot be verified. Ensure strings, parentheses and braces "
            "are balanced and give every agent() an explicit top-level model:." % e
        )
        return
    if problems:
        deny(
            "Every agent() a workflow spawns must pass a top-level model: in "
            "its options object (one of %s). Problems found:\n- %s\nRewrite "
            "each as agent(prompt, { model: 'opus'|'sonnet'|'haiku', ... })."
            % (VALID_CHOICES_STR, "\n- ".join(problems))
        )
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
