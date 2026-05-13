#!/usr/bin/env python
"""Adapter from openclaw-codex bridge actions to the Phase 1.5 bus worker."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import bus_worker  # noqa: E402
from tools.bus_worker import WorkerFailure  # noqa: E402  (public API since PR #75)


ACTION_MAP = {
    "bus.list": "action_list",
    "bus.read": "action_read",
    "bus.rg": "action_rg",
    "bus.handoff_to_codex": "action_handoff_to_codex",
    "bus.codex_exec": "action_codex_exec",
}


def load_bridge_module(bridge_path: Path | None = None) -> ModuleType:
    """Import openclaw-codex-bridge.py via importlib (filename has a dash)."""
    path = bridge_path or Path(__file__).with_name("openclaw-codex-bridge.py")
    spec = importlib.util.spec_from_file_location("openclaw_codex_bridge", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import bridge module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def register_all(
    workai_root: Path,
    bridge_root: Path | None = None,
    *,
    bridge_module=None,
) -> dict[str, Callable[[dict, dict], dict]]:
    """Register the 5 bus.* handlers with bus_worker.HANDLERS."""
    module = bridge_module or load_bridge_module()
    handlers = {}
    for kind, action_name in ACTION_MAP.items():
        handler = make_handler(getattr(module, action_name), workai_root, bridge_root)
        bus_worker.register_handler(kind, handler)
        handlers[kind] = handler
    return handlers


def make_handler(action_fn, workai_root: Path, bridge_root: Path | None):
    """Wrap a bridge action_fn into a (envelope, payload) -> dict bus handler."""
    bridge_error = action_fn.__globals__.get("BridgeError") if hasattr(action_fn, "__globals__") else None

    def handler(_envelope: dict, payload: dict) -> dict:
        try:
            return action_fn(payload, workai_root, bridge_root)
        except Exception as exc:
            is_bridge_error = (
                (bridge_error is not None and isinstance(exc, bridge_error))
                or type(exc).__name__ == "BridgeError"
            )
            if is_bridge_error:
                raise WorkerFailure("bridge_error", message=str(exc)) from exc
            raise

    return handler
