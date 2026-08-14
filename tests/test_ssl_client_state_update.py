"""Tests for SSL state-update normalization without Home Assistant."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibo_smart_control"


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_ssl_client_module():
    package_name = "orvibo_smart_control_ssl_state_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package

    homeassistant = _module("homeassistant")
    homeassistant.__path__ = []
    _module("homeassistant.core", HomeAssistant=object)
    _module(
        f"{package_name}.packet",
        HomematePacket=object,
        HomemateJsonData=object,
    )
    _module(
        f"{package_name}.protocol",
        normalize_password_hash=lambda value: value,
    )
    _module(
        f"{package_name}.ssl_transport",
        SSLTransport=object,
        TlsFiles=object,
    )

    class PendingRequests:
        def resolve(self, _key, _data):
            return False

    _module(f"{package_name}.pending_requests", PendingRequests=PendingRequests)
    _module(
        f"{package_name}.const",
        CLIENT_CERT="client.crt",
        CLIENT_KEY="client.key",
        SERVER_CA="server.crt",
        ID_UNSET="",
        DEFAULT_KEY="test-key",
        SSL_MAX_RECONNECT_ATTEMPTS=1,
        CMD_HELLO=1,
        CMD_LOGIN=2,
        CMD_STATE_UPDATE=42,
        CMD_CONTROL=43,
        CMD_HEARTBEAT=3,
        CMD_HANDSHAKE=4,
        CMD_CLOTHES_HORSE_CONTROL=98,
        CMD_CLOTHES_HORSE_STATE=99,
        CMD_CLOTHES_HORSE_QUERY=100,
        CMD_COS_AUTH=313,
        CMD_TEMP_PASSWORD=314,
        CMD_DELETE_AUTHORIZATION=315,
    )
    loaded = importlib.import_module(f"{package_name}.ssl_client")
    loaded.TestPendingRequests = PendingRequests
    return loaded


class SSLStateUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_ssl_client_module()

    def test_event_time_is_preserved_in_normalized_status(self) -> None:
        client = object.__new__(self.module.SSLClient)
        client._pending_requests = self.module.TestPendingRequests()
        updates = []
        client.on_status_update = lambda device_id, status: updates.append(
            (device_id, status)
        )

        asyncio.run(
            client._handle_state_update(
                {
                    "cmd": 352,
                    "deviceId": "w-lock",
                    "uid": "lock-uid",
                    "time": 1786675012,
                    "event": {
                        "name": "doorbell_ring",
                        "value": {"picture_url": "captured.jpg"},
                    },
                }
            )
        )

        self.assertEqual(updates[0][0], "w-lock")
        self.assertEqual(updates[0][1]["time"], 1786675012)


if __name__ == "__main__":
    unittest.main()
