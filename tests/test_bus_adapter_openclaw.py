import sys
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import bus_adapter_openclaw, bus_worker


BUS_KINDS = {
    "bus.list",
    "bus.read",
    "bus.rg",
    "bus.handoff_to_codex",
    "bus.codex_exec",
}


def stub_bridge_module():
    module = types.SimpleNamespace()

    class BridgeError(Exception):
        pass

    module.BridgeError = BridgeError
    module.action_list = Mock(return_value={"action": "list"})
    module.action_read = Mock(return_value={"action": "read"})
    module.action_rg = Mock(return_value={"action": "rg"})
    module.action_handoff_to_codex = Mock(return_value={"action": "handoff"})
    module.action_codex_exec = Mock(return_value={"action": "codex"})
    return module


class BusAdapterOpenClawTests(unittest.TestCase):
    def setUp(self):
        self.original_handlers = dict(bus_worker.HANDLERS)
        self.addCleanup(self.restore_handlers)
        # Nominal paths — register_all only captures them in handler closures;
        # this test passes a stub bridge so the FS is never touched. Neutral
        # placeholders (no local-FS disclosure per secret-scan 2026-05-16).
        self.workai_root = Path("/work")
        self.bridge_root = Path("/tmp/codex-bridge")

    def restore_handlers(self):
        bus_worker.HANDLERS.clear()
        bus_worker.HANDLERS.update(self.original_handlers)

    def test_register_all_returns_5_handlers(self):
        handlers = bus_adapter_openclaw.register_all(
            self.workai_root,
            self.bridge_root,
            bridge_module=stub_bridge_module(),
        )

        self.assertEqual(set(handlers), BUS_KINDS)
        self.assertEqual(len(handlers), 5)

    def test_register_all_writes_to_bus_worker_HANDLERS(self):
        bus_adapter_openclaw.register_all(
            self.workai_root,
            self.bridge_root,
            bridge_module=stub_bridge_module(),
        )

        self.assertIn("bus.list", bus_worker.HANDLERS)
        self.assertTrue(callable(bus_worker.HANDLERS["bus.list"]))

    def test_make_handler_passes_payload_as_request(self):
        action_fn = Mock(return_value={"ok": True})
        payload = {"path": "README.md"}
        envelope = {"kind": "bus.read"}

        handler = bus_adapter_openclaw.make_handler(action_fn, self.workai_root, self.bridge_root)
        handler(envelope, payload)

        action_fn.assert_called_once_with(payload, self.workai_root, self.bridge_root)

    def test_make_handler_returns_action_result(self):
        result = {"items": [{"path": "README.md"}]}
        action_fn = Mock(return_value=result)

        handler = bus_adapter_openclaw.make_handler(action_fn, self.workai_root, self.bridge_root)

        self.assertIs(handler({"kind": "bus.list"}, {"path": "."}), result)

    def test_make_handler_translates_BridgeError_to_WorkerFailure(self):
        class BridgeError(Exception):
            pass

        def action_fn(_request, _workai_root, _bridge_root):
            raise BridgeError("invalid id")

        handler = bus_adapter_openclaw.make_handler(action_fn, self.workai_root, self.bridge_root)

        with self.assertRaises(bus_adapter_openclaw.WorkerFailure) as caught:
            handler({"kind": "bus.read"}, {"path": ".env"})

        self.assertEqual(caught.exception.reason, "bridge_error")
        self.assertEqual(caught.exception.details["message"], "invalid id")

    def test_make_handler_lets_other_exceptions_propagate(self):
        def action_fn(_request, _workai_root, _bridge_root):
            raise RuntimeError("oops")

        handler = bus_adapter_openclaw.make_handler(action_fn, self.workai_root, self.bridge_root)

        with self.assertRaisesRegex(RuntimeError, "oops"):
            handler({"kind": "bus.read"}, {"path": "README.md"})

    def test_load_bridge_module_imports_dash_named_file(self):
        bridge_path = ROOT / f"_tmp-{uuid.uuid4().hex}-fake-bridge-name.py"
        try:
            bridge_path.write_text(
                "class BridgeError(Exception):\n"
                "    pass\n",
                encoding="utf-8",
            )

            module = bus_adapter_openclaw.load_bridge_module(bridge_path)
        finally:
            try:
                bridge_path.unlink()
            except FileNotFoundError:
                pass

        self.assertTrue(hasattr(module, "BridgeError"))
        self.assertTrue(issubclass(module.BridgeError, Exception))

    def test_register_all_uses_injected_bridge_module(self):
        stub = stub_bridge_module()
        with patch(
            "tools.bus_adapter_openclaw.load_bridge_module",
            side_effect=AssertionError("should not load real bridge"),
        ):
            handlers = bus_adapter_openclaw.register_all(
                self.workai_root,
                self.bridge_root,
                bridge_module=stub,
            )

        result = handlers["bus.list"]({"kind": "bus.list"}, {"path": "."})
        self.assertEqual(result, {"action": "list"})
        stub.action_list.assert_called_once_with({"path": "."}, self.workai_root, self.bridge_root)


if __name__ == "__main__":
    unittest.main()
