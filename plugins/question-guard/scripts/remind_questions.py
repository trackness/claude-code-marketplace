#!/usr/bin/env python3
"""UserPromptSubmit entry point for question-guard.

The reminder logic lives in the sibling module ``_remind`` and is written against
Python 3.14+. On an older interpreter that module might not even parse, and a
crashing hook exits non-zero -- so this entry point is kept to syntax every
Python 3 accepts. It checks the interpreter first: too old -> it exits 0 SILENTLY
(fail-OPEN, deliberately the opposite of model-guard's fail-closed guard, because
a missing reminder is harmless while noise or a blocked prompt is not); new enough
-> it imports ``_remind`` lazily, by name, and hands off to its ``main``.

stdlib only. Always exits 0.
"""

import importlib
import sys

_REQUIRED = (3, 14)


def main():
    """Silently no-op on an unsupported interpreter, else run the real hook."""
    if sys.version_info < _REQUIRED:
        return
    # Imported by name so the 3.14-only module is compiled only after the guard
    # above passes (a top-level ``import _remind`` would be compiled eagerly).
    importlib.import_module("_remind").main()


if __name__ == "__main__":
    main()
