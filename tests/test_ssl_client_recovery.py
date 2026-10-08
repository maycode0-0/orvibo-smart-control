"""Regression tests for silent cloud-control failures and reconnects."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibo_smart_control"


def _load_client_module():
    package_name = "orvibo_smart_control_ssl_recovery_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    # Keep cryptography's native modules loaded outside the temporary HA stubs.
    importlib.import_module(f"{package_name}.packet")
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    with patch.dict(sys.modules, {"homeassistant": homeassistant, "homeassistant.core": core}):
        return importlib.import_module(f"{package_name}.ssl_client")


class SSLClientRecoveryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_client_module()

    def make_client(self):
        client = self.module.SSLClient(
            object(), "cloud.invalid", 10002, "test-user", "A" * 32,
            "test-family", lambda _session: None, lambda _device, _state: None,
            retry_interval=0.001,
        )
        client.connected = True
        client.session_id = "s" * 32
        client.session_key = b"0123456789abcdef"
        client.reader = object()
        client.writer = types.SimpleNamespace(is_closing=lambda: False, close=lambda: None)
        return client

    async def test_reader_failure_relogs_in_and_releases_pending_controls(self):
        for error in (asyncio.IncompleteReadError(b"", 42), OSError("socket failed")):
            with self.subTest(error=type(error).__name__):
                client = self.make_client()
                pending = client._pending_requests.register("control:test-device")
                client.transport.readexactly = AsyncMock(side_effect=error)
                client.transport.close = AsyncMock()
                client._listening_task = asyncio.current_task()

                async def login():
                    client.connected = True
                    client._closed = False
                    return True

                client.connect_and_login = AsyncMock(side_effect=login)
                await client._listen_loop()

                client.connect_and_login.assert_awaited_once()
                client.transport.close.assert_awaited_once()
                self.assertTrue(pending.done())
                self.assertIsNone(pending.result())

    async def test_missing_reader_still_attempts_recovery(self):
        client = self.make_client()
        client.reader = None
        client._reconnect = AsyncMock(return_value=True)

        await client._listen_loop()

        client._reconnect.assert_awaited_once()
        self.assertFalse(client.connected)

    async def test_failed_switch_write_raises_instead_of_reporting_success(self):
        client = self.make_client()
        client.connect_and_login = AsyncMock(return_value=True)
        client.transport.write = AsyncMock(side_effect=OSError("socket failed"))

        with self.assertRaises(ConnectionError):
            await client.send_control_switch("test-device", "test-gateway", True)

        self.assertFalse(client.connected)
        future = client._pending_requests.get("control:test-device")
        self.assertTrue(future is None or future.done())

    async def test_missing_writer_does_not_drop_control_and_report_success(self):
        client = self.make_client()
        client.writer = None
        client.connect_and_login = AsyncMock(return_value=True)
        client._reconnect = AsyncMock(return_value=True)

        with self.assertRaises(ConnectionError):
            await client.send_control_switch("test-device", "test-gateway", False)

        client._reconnect.assert_not_awaited()

    async def test_recovery_retries_after_first_login_attempt_fails(self):
        client = self.make_client()
        client.transport.readexactly = AsyncMock(side_effect=OSError("socket failed"))
        client.transport.close = AsyncMock()
        client.connect_and_login = AsyncMock(side_effect=[False, True])
        with patch.object(self.module.asyncio, "sleep", new=AsyncMock()):
            await client._listen_loop()

        self.assertEqual(client.connect_and_login.await_count, 2)

    async def test_stalled_write_times_out_instead_of_blocking_future_controls(self):
        client = self.make_client()
        client.connect_and_login = AsyncMock(return_value=True)
        client.PACKET_WRITE_TIMEOUT = 0.001
        gate = asyncio.Event()
        async def blocked_write(_data):
            await gate.wait()

        client.transport.write = AsyncMock(side_effect=blocked_write)
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(
                client.send_control_switch("test-device", "test-gateway", True),
                timeout=0.1,
            )

        self.assertFalse(client.connected)

    async def test_normal_switch_control_sends_the_requested_payload(self):
        client = self.make_client()
        client.connect_and_login = AsyncMock(return_value=True)
        client.transport.write = AsyncMock()

        self.assertTrue(await client.send_control_switch("test-device", "test-gateway", True))

        client.transport.write.assert_awaited_once()
        encoded = client.transport.write.call_args.args[0]
        packet = self.module.HomematePacket(encoded, {client.session_id: client.session_key})
        self.assertEqual(packet.json_payload["deviceId"], "test-device")
        self.assertEqual(packet.json_payload["uid"], "test-gateway")
        self.assertEqual(packet.json_payload["order"], "set property")
        self.assertEqual(packet.json_payload["properties"], {"onoff": {"status": "on"}})

    async def test_explicit_shutdown_does_not_restart_listener(self):
        client = self.make_client()
        client.transport.close = AsyncMock()
        client._reconnect = AsyncMock()

        await client._disconnect()
        client.reader = None
        await client._listen_loop()

        self.assertTrue(client._closed)
        self.assertFalse(client.connected)
        client._reconnect.assert_not_awaited()

    async def test_repeated_recovery_failure_stops_after_retry_limit(self):
        client = self.make_client()
        client.transport.readexactly = AsyncMock(side_effect=OSError("socket failed"))
        client.transport.close = AsyncMock()
        client.connect_and_login = AsyncMock(return_value=False)
        with patch.object(self.module.asyncio, "sleep", new=AsyncMock()):
            await client._listen_loop()

        self.assertEqual(client.connect_and_login.await_count, 5)
        self.assertFalse(client.connected)


if __name__ == "__main__":
    unittest.main()
