#!/usr/bin/env python3
r"""A correct, bounded JavaScript string-escape decoder.

WHY. The workflow lint (``_enforce``) must resolve a single string / template
model value to the runtime string JavaScript would produce, so a banned model
hidden behind an escape (``'fabl\x65'`` folds to ``fable``) is caught by the
banned check instead of compared raw and waved through. :func:`js_decode_string`
takes the INNER text of one literal (quotes / backticks already stripped) and
returns that decoded string.

SCOPE. Every escape a ``"``/``'`` string or a NON-interpolated template literal
can carry: ``\xHH``, ``\uHHHH``, ``\u{...}``, legacy octal ``\NNN`` / ``\0``, the
single-char escapes (``\n \t \r \b \f \v`` plus the self-mapping ``\\ \' \" \` \$``)
and a line continuation (``\<newline>`` -> empty). A malformed escape is a JS
SyntaxError that never runs, so it is decoded best-effort (an unrecognised ``\c``
becomes the JS identity ``c``); a best-effort decode only ever tightens the later
banned / blank check toward deny, it never opens a bypass.

The scan is a single linear pass with no backtracking or recursion, so it is
bounded by the literal length. stdlib only; python3 >= 3.14.
"""

# Length of the fixed-width hex escape bodies (`\xHH` / `\uHHHH`), named so the
# validity checks are not magic-value comparisons.
_HEX_ESCAPE_LEN = 2
_UNICODE_ESCAPE_LEN = 4

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_OCTAL_DIGITS = frozenset("01234567")
# An octal escape may span 3 digits only when the first is 0-3 (value <= 255).
_OCTAL_LEAD_LONG = frozenset("0123")
_OCTAL_MAX_LONG = 3
_OCTAL_MAX_SHORT = 2
_MAX_CODE_POINT = 0x10FFFF

# Escape body -> replacement for the single-char escapes that do NOT map to
# themselves (a line continuation drops to empty). Every other `\c` is the JS
# identity `c`, produced by this dict's lookup default.
_SIMPLE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "\n": "",
    "\r": "",
}


def _is_hex(text):
    """True iff `text` is non-empty and every char is an ASCII hex digit."""
    return bool(text) and all(c in _HEX_DIGITS for c in text)


def _hex_escape(text, i):
    r"""Decode `\xHH` whose 'x' is at text[i]; return (decoded, next_index). A
    malformed `\x` (a JS SyntaxError) folds to the identity char 'x'."""
    start = i + 1
    end = start + _HEX_ESCAPE_LEN
    body = text[start:end]
    if len(body) == _HEX_ESCAPE_LEN and _is_hex(body):
        return chr(int(body, 16)), end
    return "x", start


def _unicode_escape(text, i):
    r"""Decode `\uHHHH` or `\u{...}` whose 'u' is at text[i]; return (decoded,
    next_index). A malformed / out-of-range form folds to the identity char 'u'."""
    start = i + 1
    if start < len(text) and text[start] == "{":
        body_start = start + 1
        close = text.find("}", body_start)
        if close != -1:
            body = text[body_start:close]
            if _is_hex(body) and int(body, 16) <= _MAX_CODE_POINT:
                return chr(int(body, 16)), close + 1
        return "u", start
    end = start + _UNICODE_ESCAPE_LEN
    body = text[start:end]
    if len(body) == _UNICODE_ESCAPE_LEN and _is_hex(body):
        return chr(int(body, 16)), end
    return "u", start


def _octal_escape(text, i):
    r"""Decode a legacy octal escape whose first octal digit is at text[i]; return
    (decoded, next_index). 1-3 octal digits, capped at 2 when the first is 4-7 (so
    the value stays <= 255); `\0` with no trailing octal digit is NUL."""
    n = len(text)
    digits = text[i]
    limit = _OCTAL_MAX_LONG if text[i] in _OCTAL_LEAD_LONG else _OCTAL_MAX_SHORT
    j = i + 1
    while j < n and len(digits) < limit and text[j] in _OCTAL_DIGITS:
        digits += text[j]
        j += 1
    return chr(int(digits, 8)), j


def _decode_escape(text, i):
    r"""Decode the escape body at text[i] (the '\' sits at text[i-1]); return
    (decoded, next_index). A trailing '\' (i past the end) decodes to empty.
    Dispatch hex / unicode / octal; every other body is the JS identity char."""
    if i >= len(text):
        return "", i
    body = text[i]
    if body == "x":
        return _hex_escape(text, i)
    if body == "u":
        return _unicode_escape(text, i)
    if body in _OCTAL_DIGITS:
        return _octal_escape(text, i)
    return _SIMPLE_ESCAPES.get(body, body), i + 1


def js_decode_string(text):
    r"""Return the runtime string JavaScript would produce from the INNER `text` of
    one string / non-interpolated template literal: every escape decoded, every
    other character copied verbatim. Linear, bounded, and never raises."""
    if "\\" not in text:
        return text  # no escapes: the raw inner text is already the value
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        decoded, i = _decode_escape(text, i + 1)
        out.append(decoded)
    return "".join(out)
