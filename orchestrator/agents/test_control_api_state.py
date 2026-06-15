"""Tests for GET /state version field (AIO-8)."""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


def _make_fake_modules():
    """Stub out heavy imports so control_api can be imported in-process."""
    # workflow stub
    workflow_mod = types.ModuleType("workflow")
    workflow_mod.status_snapshot = lambda: {"agent": {}, "polling": {}}
    sys.modules.setdefault("workflow", workflow_mod)

    # fastapi stubs (use real fastapi if available, else minimal shim)
    try:
        import fastapi  # noqa: F401
    except ImportError:
        for name in ("fastapi", "fastapi.responses"):
            sys.modules.setdefault(name, types.ModuleType(name))


_make_fake_modules()


def _state_response():
    """Call the state() view with a mocked DB and orchestrator."""
    import importlib
    import sys
    import types

    # Stub orchestrator before importing control_api
    orch_mod = types.ModuleType("orchestrator")

    class _IssueState:
        TRIAGING = MagicMock(value="triaging")
        DEV_ASSIGNED = MagicMock(value="dev_assigned")
        DEV_IN_PROGRESS = MagicMock(value="dev_in_progress")
        JUNIOR_QA = MagicMock(value="junior_qa")
        ACCEPTANCE_CHECK = MagicMock(value="acceptance_check")
        DEV_DEPLOY = MagicMock(value="dev_deploy")
        SENIOR_QA = MagicMock(value="senior_qa")
        DESIGN_QA = MagicMock(value="design_qa")
        SANITY_CHECK = MagicMock(value="sanity_check")
        BLOCKED = MagicMock(value="blocked")
        CANCELLED = MagicMock(value="cancelled")
        DONE = MagicMock(value="done")

    orch_mod.IssueState = _IssueState
    orch_mod.VERSION = "0.1.0"
    sys.modules["orchestrator"] = orch_mod

    # Re-import control_api fresh (or import for the first time)
    if "agents.control_api" in sys.modules:
        del sys.modules["agents.control_api"]
    if "control_api" in sys.modules:
        del sys.modules["control_api"]

    # Add orchestrator dir to path so `import control_api` works
    import os
    agents_dir = os.path.dirname(__file__)
    if agents_dir not in sys.path:
        sys.path.insert(0, agents_dir)

    import control_api  # type: ignore

    # Fake DB context manager
    fake_row = MagicMock()
    fake_row.__getitem__ = lambda self, k: {"state": "triaging", "n": 1}.get(k, "")

    fake_conn = MagicMock()
    fake_conn.__enter__ = lambda s: s
    fake_conn.__exit__ = MagicMock(return_value=False)
    fake_conn.execute.return_value.fetchall.return_value = [
        MagicMock(**{"__getitem__": lambda s, k: {"state": "triaging", "n": 1, "identifier": "AIO-1"}[k]})
    ]
    fake_conn.execute.return_value.fetchone.return_value = MagicMock(**{"__getitem__": lambda s, k: 0})

    with patch.object(control_api, "_conn", return_value=fake_conn):
        return control_api.state()


def test_state_includes_version():
    result = _state_response()
    assert "version" in result, "/api/v1/state response must include a 'version' key"


def test_state_version_matches_constant():
    result = _state_response()
    import sys
    orch = sys.modules["orchestrator"]
    assert result["version"] == orch.VERSION
