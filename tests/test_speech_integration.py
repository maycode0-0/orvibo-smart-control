"""Speech service and encrypted scene transactions, without a physical device."""

from __future__ import annotations

import asyncio
import importlib
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_ssl_client_recovery import _load_client_module
from test_service_handlers import _load_module


class SpeechWireTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_client_module()

    def make_client(self):
        client = self.module.SSLClient(
            object(), "cloud.invalid", 10002, "test-user", "A" * 32,
            "family", lambda _session: None, lambda _device, _state: None,
        )
        client.connected = True
        client.session_id = "s" * 32
        client.session_key = b"0123456789abcdef"
        client.writer = types.SimpleNamespace(is_closing=lambda: False, close=lambda: None)
        client.connect_and_login = AsyncMock(return_value=True)
        return client

    async def test_encrypted_update_then_execution_wait_for_matching_acks(self):
        client = self.make_client()
        incoming = asyncio.Queue()
        sent = []
        async def read(_size):
            return await incoming.get()

        client.transport.readexactly = AsyncMock(side_effect=read)

        async def write(frame):
            packet = self.module.HomematePacket(
                frame, {client.session_id: client.session_key},
            ).json_payload
            sent.append(packet)
            response = {
                "cmd": packet["cmd"], "serial": packet["serial"],
                "uniSerial": packet["uniSerial"], "status": 0,
            }
            ack = self.module.HomematePacket.build_packet(
                b"dk", client.session_key, client.session_id.encode(), response,
            )
            await incoming.put(ack[:42])
            await incoming.put(ack[42:])

        client.transport.write = AsyncMock(side_effect=write)
        listener = asyncio.create_task(client._listen_loop())
        try:
            binding = {
                "deviceId": "panel", "uid": "gateway", "sceneBindId": "binding",
                "order": "tts play", "actionName": '测试"播报\n第二行',
                "value1": 0, "value2": 0, "value3": 0, "value4": 0,
                "delayTime": 0, "sceneBindTag": 0,
            }
            self.assertTrue(await client.update_speech_scene("scene", binding))
            self.assertTrue(await client.execute_speech_scene("scene", 42))
        finally:
            listener.cancel()
            await listener
        self.assertEqual([packet["cmd"] for packet in sent], [195, 197])
        self.assertEqual(sent[0]["sceneBindList"], [binding])
        self.assertEqual(sent[1]["sceneId"], 42)
        self.assertEqual(sent[1]["triggerType"], 0)
        for packet in sent:
            self.assertEqual(packet["userName"], "test-user")
            self.assertEqual(packet["sceneNo"], "scene")
            self.assertNotIn("uid", packet)
            self.assertNotIn("deviceId", packet)
        self.assertNotIn("sceneBindList", sent[1])

    async def test_stale_or_conflicting_ack_cannot_authorize_execution(self):
        client = self.make_client()
        client._speech_request_identity = {
            "cmd": 195, "serial": 8, "uniSerial": 90, "sceneNo": "new-scene",
        }
        future = client._pending_requests.register("speech_scene")
        for response in (
            {"cmd": 197, "serial": 8, "status": 0},
            {"cmd": 195, "serial": 7, "status": 0},
            {"cmd": 195, "status": 0},
            {"cmd": 195, "serial": 8, "uniSerial": 89, "status": 0},
            {"cmd": 195, "serial": 8, "sceneNo": "old-scene", "status": 0},
        ):
            self.assertFalse(client._handle_speech_scene_response(response))
            self.assertFalse(future.done())
        self.assertTrue(client._handle_speech_scene_response(
            {"cmd": 195, "serial": "8", "status": 0},
        ))
        self.assertEqual(future.result()["status"], 0)
        client._pending_requests.discard("speech_scene", future)

    async def test_timeout_or_write_failure_is_not_replayed(self):
        for fail_write in (False, True):
            client = self.make_client()
            client.transport.write = AsyncMock(
                side_effect=OSError("private details") if fail_write else None,
            )
            payload = self.module.HomemateJsonData.ssl_execute_speech_scene(
                "test-user", "scene", 42,
            )
            with self.assertRaises(ConnectionError):
                await client._send_speech_scene_request(payload, timeout=0.001)
            self.assertEqual(client.transport.write.await_count, 1)
            self.assertIsNone(client._speech_request_identity)
            self.assertIsNone(client._pending_requests.get("speech_scene"))

    async def test_only_explicit_zero_status_is_success(self):
        for status in (None, False, 0.0, 5, "5", 0, "0"):
            client = self.make_client()

            async def acknowledge(_data, _key):
                identity = client._speech_request_identity
                client._handle_speech_scene_response({**identity, "status": status})

            client._send_packet = AsyncMock(side_effect=acknowledge)
            accepted = await client.execute_speech_scene("scene", 42)
            expected = type(status) is int and status == 0 or status == "0"
            self.assertEqual(accepted, expected)


class SpeechServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_module()
        self.handlers = {}
        self.coordinator = types.SimpleNamespace(
            async_speak=AsyncMock(return_value={"stage": "speech_scene_executed"}),
        )
        self.hass = types.SimpleNamespace(
            data={"orvibo_smart_control": {"entry": self.coordinator}},
            bus=types.SimpleNamespace(async_listen_once=lambda *args: None),
            services=types.SimpleNamespace(async_register=(
                lambda _domain, name, handler, **kwargs: self.handlers.update({name: handler})
            )),
        )
        await self.module.async_register_services(self.hass)

        class HAError(Exception):
            def __init__(self, **kwargs):
                self.translation_key = kwargs["translation_key"]
                super().__init__(self.translation_key)

        class ValidationError(HAError):
            pass

        self.error_module = types.ModuleType("homeassistant.exceptions")
        self.error_module.HomeAssistantError = HAError
        self.error_module.ServiceValidationError = ValidationError
        self.error = HAError
        self.validation_error = ValidationError

    async def call(self, **data):
        with patch.dict("sys.modules", {"homeassistant.exceptions": self.error_module}):
            return await self.handlers["speak"](types.SimpleNamespace(data=data))

    async def test_single_entry_invokes_only_selected_scene_once(self):
        result = await self.call(scene_name="HA语音播报", message="你好")
        self.assertEqual(result["stage"], "speech_scene_executed")
        self.coordinator.async_speak.assert_awaited_once_with("HA语音播报", "你好")

    async def test_multiple_or_unknown_entries_never_broadcast(self):
        other = types.SimpleNamespace(async_speak=AsyncMock())
        self.hass.data["orvibo_smart_control"]["other"] = other
        for entry_id in (None, "missing", [], ""):
            with self.assertRaises(self.validation_error) as caught:
                await self.call(entry_id=entry_id, scene_name="speech", message="hello")
            self.assertEqual(caught.exception.translation_key, "speech_select_entry")
        self.coordinator.async_speak.assert_not_awaited()
        other.async_speak.assert_not_awaited()
        await self.call(entry_id="entry", scene_name="speech", message="hello")
        self.coordinator.async_speak.assert_awaited_once()
        other.async_speak.assert_not_awaited()

    async def test_service_surfaces_stage_without_speech_or_private_error(self):
        speech = importlib.import_module(f"{self.module.__package__}.speech")
        self.coordinator.async_speak.side_effect = speech.SpeechError("update_failed")
        with self.assertRaises(self.error) as caught:
            await self.call(scene_name="speech", message="PRIVATE SPEECH")
        self.assertEqual(caught.exception.translation_key, "speech_update_failed")
        self.assertNotIn("PRIVATE", str(caught.exception))


class SpeechReadtableTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_chunks_cannot_hide_other_scene_actions(self):
        ssl = _load_client_module()
        https = importlib.import_module(f"{ssl.__package__}.https_client")
        for response in (
            {"data": [{"scene": [], "sceneBind": []}, None]},
            {"data": [{"sceneBind": {}}, {"sceneBind": []}]},
            {"data": [{"sceneBind": {"same": {}}}, {"sceneBind": {"same": {}}}]},
            {"data": {"scene": [], "sceneBind": []}, "hasMore": True},
        ):
            client = https.HttpsClient("test-user", "A" * 32)
            client.ensure_login = AsyncMock(return_value=True)
            client._send_request = AsyncMock(return_value=response)
            with self.subTest(response=response):
                self.assertIsNone(await client.fetch_speech_table())

    async def test_transport_error_does_not_expose_response_or_credentials(self):
        ssl = _load_client_module()
        https = importlib.import_module(f"{ssl.__package__}.https_client")
        client = https.HttpsClient("test-user", "A" * 32)
        client.ensure_login = AsyncMock(return_value=True)
        client._readtable = AsyncMock(side_effect=https.aiohttp.ClientError("PRIVATE"))
        with self.assertLogs(https.__name__, level="WARNING") as logs:
            self.assertIsNone(await client.fetch_speech_table())
        self.assertNotIn("PRIVATE", repr(logs.output))

    async def test_missing_scene_tables_are_fetched_without_caching_text(self):
        ssl = _load_client_module()
        https = importlib.import_module(f"{ssl.__package__}.https_client")
        client = https.HttpsClient("test-user", "A" * 32)
        client.ensure_login = AsyncMock(return_value=True)
        client._readtable = AsyncMock(side_effect=[
            {"gateway": []}, {"scene": [], "sceneBind": []},
        ])
        self.assertEqual(await client.fetch_speech_table(), {
            "gateway": [], "scene": [], "sceneBind": [],
        })
        self.assertEqual([call.kwargs["device_flag"] for call in client._readtable.await_args_list], [0, 1])
