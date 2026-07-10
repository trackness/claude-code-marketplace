#!/usr/bin/env python3
"""PreToolUse entry point for model-guard's explicit-model enforcement.

The enforcement logic lives in the sibling module ``_enforce`` and is written
against Python 3.14+ (PEP 758 except groups, structural pattern matching,
dataclass ``slots``). On an older interpreter that module would not even
*parse*, and a hook that crashes exits non-zero WITHOUT emitting a permission
decision -- which Claude Code treats as a non-blocking error, letting the very
spawn the hook exists to gate proceed (fail-OPEN, the direction this plugin
forbids).

So this entry point is kept to syntax every Python 3 accepts. It checks the
interpreter first: too old -> it emits a fail-CLOSED deny itself and never
imports (nor crashes on) the 3.14-only module; new enough -> it hands off to
``_enforce.main``. ``_enforce`` is imported lazily, by name, so its 3.14 syntax
is compiled only once the version check has passed.

stdlib only. The hook always exits 0.
"""

import importlib
import json
import sys

_REQUIRED = (3, 14)


def _emit_deny(reason):
    """Print a PreToolUse deny decision (the plugin's fail-closed output)."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def main():
    """Fail closed on an unsupported interpreter, else run the real hook."""
    if sys.version_info < _REQUIRED:
        have = ".".join(str(p) for p in sys.version_info[:3])
        need = ".".join(str(p) for p in _REQUIRED)
        _emit_deny(
            f"model-guard's enforcement hook requires Python {need}+ but was "
            f"launched on {have}, so it cannot statically verify this spawn's "
            f"model and denies it (fail-closed). Point the hook's `python3` at a "
            f"Python {need}+ interpreter."
        )
        sys.exit(0)
    # Imported by name so the 3.14-only module is compiled only after the guard
    # above passes (a `from _enforce import ...` here would be compiled eagerly).
    importlib.import_module("_enforce").main()


if __name__ == "__main__":
    main()
