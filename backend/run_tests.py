#!/usr/bin/env python3
"""Tiny dependency-free test runner.

Discovers ``test_*`` functions in the test modules and runs them, so the suite
works even where pytest is not installed (it is also fully pytest-compatible:
just run ``pytest`` in this directory). Exits non-zero on any failure.
"""

import importlib.util
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))
sys.path.insert(0, os.path.join(HERE, "tools"))


def load_test_module(modname):
    """Load a test module by explicit path from the ``tests/`` directory.

    Resolving by bare name (``importlib.import_module``) goes through
    ``sys.path`` - which every test module mutates at import time, prepending
    ``backend/`` so it stays runnable standalone. That makes bare-name lookup
    order-dependent: once ``backend/`` sits at the front of ``sys.path``, a stray
    top-level ``backend/<modname>.py`` would shadow the canonical
    ``tests/<modname>.py``. (Exactly this happened on a Pi that still carried
    orphaned pre-reorg copies, where the suite was green in isolation but failed
    after a sibling module ran.) Loading by file path pins resolution to the
    canonical test file regardless of ``sys.path`` state.
    """
    path = os.path.join(HERE, "tests", modname + ".py")
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod

TEST_MODULES = [
    "test_protocol",
    "test_cmdchecksum",
    "test_state",
    "test_store",
    "test_migration",
    "test_predict",
    "test_sessions",
    "test_mqtt",
    "test_mqtt_service",
    "test_notify",
    "test_photos",
    "test_autotune",
    "test_autotune_session",
    "test_hostinteractive",
    "test_cookprogram",
    "test_cookdone",
    "test_probewatch",
    "test_guided",
    "test_fuel",
    "test_events",
    "test_report",
    "test_auth",
    "test_firmware",
    "test_hostupdate",
    "test_service",
    "test_api",
    "test_shakedown",
    "test_integration_pty",
]


def main() -> int:
    passed = failed = 0
    failures = []
    for modname in TEST_MODULES:
        mod = load_test_module(modname)
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            # Only run functions defined in this module, not ones imported
            # into its namespace from another module.
            if getattr(fn, "__module__", None) != modname:
                continue
            try:
                fn()
            except Exception:
                failed += 1
                failures.append(f"{modname}.{name}")
                print(f"FAIL  {modname}.{name}")
                traceback.print_exc()
            else:
                passed += 1
                print(f"ok    {modname}.{name}")

    print(f"\n{passed} passed, {failed} failed")
    if failures:
        print("failures:")
        for f in failures:
            print(f"  - {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
