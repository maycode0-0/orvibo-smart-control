"""Update ACK action checks and log-independent service diagnostics, offline."""

from copy import deepcopy
import importlib
import unittest
from unittest.mock import AsyncMock

from test_speech import sample_table, speech
import test_speech_integration as wire_tests


class UpdateAckTests(unittest.TestCase):
    def setUp(self):
        self.binding = speech.parse_speech_target(sample_table(), "HA播报", "私人播报").binding

    def ack(self, **fields):
        return speech.summarize_update_ack({"status": 0, **fields}, self.binding)

    def test_matching_action_success_is_separate_from_playback(self):
        result = self.ack(successList=[deepcopy(self.binding)], failList=[])
        self.assertTrue(result)
        self.assertEqual(result.details["binding_confirmation"], "matched")
        self.assertTrue(result.details["returned_text_matches"])
        self.assertEqual(result.details["success_count"], 1)
        self.assertEqual(result.details["fail_count"], 0)
        self.assertNotIn("私人", repr(result))
        self.assertNotIn("私人", repr(result.details))
        self.assertNotIn("panel_sound_verified", result.details)

    def test_missing_lists_never_claim_action_confirmation(self):
        result = self.ack()
        self.assertTrue(result.accepted)
        self.assertEqual(result.details["binding_confirmation"], "not_reported")
        self.assertFalse(result.details["success_list_present"])
        self.assertIsNone(result.details["success_count"])

    def test_status_zero_cannot_override_action_failure(self):
        result = self.ack(successList=[self.binding], failList=[{"error": "PRIVATE"}])
        self.assertFalse(result)
        self.assertEqual(result.details["fail_count"], 1)
        self.assertNotIn("PRIVATE", repr(result.details))

    def test_other_or_conflicting_action_and_old_returned_text_rejected(self):
        for changes in ({"sceneBindId": "other"}, {"deviceId": "other"},
                        {"uid": "other"}, {"actionName": "以前的文字"}):
            with self.subTest(changes=changes):
                binding = {**self.binding, **changes}
                self.assertFalse(self.ack(successList=[binding]))
        self.assertFalse(self.ack(successList=[{}, self.binding]))

    def test_absent_echoed_text_does_not_invent_text_verification(self):
        result = self.ack(successList=[{"sceneBindId": self.binding["sceneBindId"]}])
        self.assertTrue(result)
        self.assertNotIn("returned_text_matches", result.details)

    def test_empty_or_malformed_present_lists_are_rejected(self):
        for fields in ({"successList": []}, {"successList": None},
                       {"successList": [None]}, {"successList": "PRIVATE"},
                       {"failList": None}, {"failList": {}}, {"failList": "PRIVATE"}):
            with self.subTest(fields=fields):
                self.assertFalse(self.ack(**fields))

    def test_no_arbitrary_response_values_enter_diagnostics(self):
        result = self.ack(successList=[self.binding], failList=[],
                          token="PRIVATE-TOKEN", debugInfo={"message": "PRIVATE-CONTENT"})
        self.assertNotIn("PRIVATE", repr(result.details))
        self.assertNotIn("token", result.details)
        self.assertNotIn("debugInfo", result.details)

    def test_nonzero_missing_and_malformed_status_never_succeed(self):
        for status in (None, False, 0.0, "", "00", "PRIVATE", 1, "1"):
            with self.subTest(status=status):
                self.assertFalse(speech.summarize_update_ack(
                    {"status": status, "successList": [self.binding]}, self.binding))
        self.assertFalse(speech.summarize_update_ack(None, self.binding))
        self.assertTrue(self.ack(status="0"))


class AckWireAndManagerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        wire_tests.SpeechWireTests.setUpClass()
        cls.wire = wire_tests.SpeechWireTests()
        cls.speech_module = importlib.import_module(f"{cls.wire.module.__package__}.speech")

    async def run_manager(self, *, fail=False):
        client = self.wire.make_client()
        table = sample_table()
        packets = []

        async def send(packet, key):
            packets.append(deepcopy(packet))
            response = {**client._speech_request_identity, "status": 0}
            if packet["cmd"] == 195:
                response.update(successList=deepcopy(packet["sceneBindList"]),
                                failList=[{"error": "PRIVATE"}] if fail else [])
                table["sceneBind"][0].update(packet["sceneBindList"][0])
            client._handle_speech_scene_response(response)

        client._send_packet = AsyncMock(side_effect=send)
        manager = self.speech_module.SpeechManager(
            AsyncMock(side_effect=lambda: deepcopy(table)),
            client.update_speech_scene, client.execute_speech_scene,
        )
        if fail:
            with self.assertLogs(self.speech_module.__name__, level="WARNING") as logged:
                with self.assertRaises(self.speech_module.SpeechError) as raised:
                    await manager.speak("HA播报", "新的私人播报")
            self.assertEqual(raised.exception.stage, "update_failed")
            self.assertNotIn("PRIVATE", repr(logged.output))
            self.assertNotIn("私人", repr(logged.output))
            return packets
        result = await manager.speak("HA播报", "新的私人播报")
        self.assertEqual(result["freshness_revision"], 2)
        self.assertEqual(result["update_ack_revision"], 1)
        self.assertEqual(result["update_ack"]["binding_confirmation"], "matched")
        self.assertTrue(result["cloud_text_verified"])
        self.assertFalse(result["panel_sound_verified"])
        self.assertNotIn("私人", repr(result))
        return packets

    async def test_accepted_action_and_readback_execute_once_and_return_diagnostics(self):
        self.assertEqual([p["cmd"] for p in await self.run_manager()], [195, 197])

    async def test_action_failure_with_status_zero_never_executes_or_retries(self):
        self.assertEqual([p["cmd"] for p in await self.run_manager(fail=True)], [195])

    async def test_old_result_is_not_replaced_by_next_call_summary(self):
        client = self.wire.make_client()
        binding = self.speech_module.parse_speech_target(sample_table(), "HA播报", "第一条").binding

        async def send(packet, key):
            response = {**client._speech_request_identity, "status": 0}
            if packet["sceneBindList"][0]["actionName"] == "第二条":
                response["failList"] = [{"error": 1}]
            client._handle_speech_scene_response(response)

        client._send_packet = AsyncMock(side_effect=send)
        first = await client.update_speech_scene("scene-no", binding)
        second = await client.update_speech_scene("scene-no", {**binding, "actionName": "第二条"})
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertIsNone(first.details["fail_count"])
        self.assertEqual(second.details["fail_count"], 1)


if __name__ == "__main__":
    unittest.main()
