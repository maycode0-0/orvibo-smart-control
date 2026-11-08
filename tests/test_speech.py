"""Offline announcement scene safety and transaction regressions."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch


MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "orvibo_smart_control" / "speech.py"
)
SPEC = importlib.util.spec_from_file_location("orvibo_speech_test", MODULE_PATH)
speech = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = speech
SPEC.loader.exec_module(speech)


def sample_table() -> dict:
    """Synthetic full tables, not a captured home configuration."""

    return {
        "scene": [
            {"sceneNo": "scene-no", "sceneId": 7, "sceneName": "HA播报", "delFlag": 0},
        ],
        "sceneBind": [
            {
                "sceneNo": "scene-no", "sceneBindId": "bind-id", "delFlag": 0,
                "uid": "panel-uid", "deviceId": "panel-device", "sceneBindTag": 0,
                "order": "tts play", "actionName": "原来的文字", "value1": 0,
                "value2": 12, "value3": "0", "value4": None, "delayTime": 0,
                "freq": 0, "pluseNum": 0, "pluseData": "", "themeId": "",
                "sceneBindTagId": "", "updateTime": 1234,
            },
        ],
    }


class SpeechParsingTests(unittest.TestCase):
    def assert_stage(self, stage: str, table: object, name: object = "HA播报",
                     message: object = "请注意") -> None:
        with self.assertRaises(speech.SpeechError) as raised:
            speech.parse_speech_target(table, name, message)
        self.assertEqual(raised.exception.stage, stage)
        self.assertEqual(str(raised.exception), stage)

    def test_update_preserves_wire_values_and_source_without_exposing_text_in_repr(self):
        table = sample_table()
        table["sceneBind"][0]["authList"] = [
            {"userId": "owner", "isAuthorized": 1, "databaseMetadata": "unused"},
        ]
        original = deepcopy(table)
        target = speech.parse_speech_target(table, "HA播报", "  到家了\n欢迎  ")
        self.assertEqual(target.scene_no, "scene-no")
        self.assertEqual(target.scene_id, 7)
        expected = {
            key: value for key, value in original["sceneBind"][0].items()
            if key in speech.SCENE_BIND_FIELDS
        }
        expected["actionName"] = "  到家了\n欢迎  "
        expected["value3"] = 0
        expected["value4"] = 0
        expected["authList"] = [{"userId": "owner", "isAuthorized": 1}]
        self.assertEqual(target.binding, expected)
        self.assertEqual(table, original)
        self.assertNotIn("到家了", repr(target))
        target.binding["authList"][0]["isAuthorized"] = 0
        self.assertEqual(table, original)

    def test_accepts_keyed_tables_and_command_alias(self):
        table = sample_table()
        binding = table["sceneBind"][0]
        binding["command"] = binding.pop("order")
        binding["sceneBindTag"] = "0"
        table["scene"] = {"scene-no": table["scene"][0]}
        table["sceneBind"] = {"bind-id": binding}
        target = speech.parse_speech_target(table, "HA播报", "早上好")
        self.assertEqual(target.binding["order"], "tts play")
        self.assertEqual(target.binding["sceneBindTag"], 0)
        self.assertNotIn("command", target.binding)

    def test_keyed_tables_supply_only_their_known_primary_key(self):
        table = sample_table()
        table["scene"][0].pop("sceneNo")
        table["sceneBind"][0].pop("sceneBindId")
        table["scene"] = {"scene-no": table["scene"][0]}
        table["sceneBind"] = {"bind-id": table["sceneBind"][0]}
        target = speech.parse_speech_target(table, "HA播报", "测试")
        self.assertEqual(target.scene_no, "scene-no")
        self.assertEqual(target.binding["sceneBindId"], "bind-id")
        self.assertNotIn("sceneNo", table["scene"]["scene-no"])
        self.assertNotIn("sceneBindId", table["sceneBind"]["bind-id"])
        table["scene"]["scene-no"]["sceneNo"] = "conflicting-no"
        self.assert_stage("read_failed", table)

    def test_scene_identifiers_match_packet_types_without_arbitrary_conversion(self):
        table = sample_table()
        table["scene"][0]["sceneId"] = "007"
        self.assertEqual(speech.parse_speech_target(table, "HA播报", "测试").scene_id, 7)
        for scene_id in (0, "0", -1, "-1", None):
            table["scene"][0]["sceneId"] = scene_id
            with self.subTest(scene_id=scene_id):
                self.assertIsNone(speech.parse_speech_target(table, "HA播报", "测试").scene_id)
        table["scene"][0].pop("sceneId")
        self.assertIsNone(speech.parse_speech_target(table, "HA播报", "测试").scene_id)
        for scene_id in ("7.0", 7.0, True, " 7", []):
            table["scene"][0]["sceneId"] = scene_id
            with self.subTest(scene_id=scene_id):
                self.assert_stage("unsafe_scene", table)
        table = sample_table()
        table["scene"][0]["sceneNo"] = 7
        self.assert_stage("unsafe_scene", table)

    def test_order_and_command_may_agree_but_must_not_conflict(self):
        table = sample_table()
        table["sceneBind"][0]["command"] = "tts play"
        self.assertEqual(
            speech.parse_speech_target(table, "HA播报", "早上好").binding["order"],
            "tts play",
        )
        table["sceneBind"][0]["command"] = "on"
        self.assert_stage("unsafe_scene", table)

    def test_non_text_and_blank_inputs_are_rejected_without_conversion(self):
        for value in (None, True, 123, ["hello"], {"message": "hello"}, "", " \t\n"):
            with self.subTest(value=value):
                self.assert_stage("invalid_message", sample_table(), message=value)
                self.assert_stage("invalid_scene", sample_table(), name=value)

    def test_evidence_based_length_limit_rejects_instead_of_truncating(self):
        with patch.object(speech, "MAX_SPEECH_LENGTH", 3):
            target = speech.parse_speech_target(sample_table(), "HA播报", "欢迎你")
            self.assertEqual(target.binding["actionName"], "欢迎你")
            self.assert_stage("invalid_message", sample_table(), message="欢迎你回家")

    def test_ordinary_editor_text_boundaries_and_character_set(self):
        text = "中" * 300
        self.assertEqual(speech.validate_message(text), text)
        self.assertEqual(speech.validate_message("Hello 123，欢迎。!?\n"), "Hello 123，欢迎。!?\n")
        for invalid in (text + "中", "欢迎😀", "测试\ud800", "<speak>欢迎</speak>", "门已开：请注意"):
            with self.subTest(message=ascii(invalid)):
                self.assert_stage("invalid_message", sample_table(), message=invalid)

    def test_scene_name_is_matched_exactly(self):
        self.assert_stage("scene_not_found", sample_table(), name=" HA播报")
        self.assert_stage("scene_not_found", sample_table(), name="HA播报 ")

    def test_both_tables_are_required_and_malformed_rows_are_not_discarded(self):
        for table in (None, [], {"data": sample_table()}, {"scene": []},
                      {"sceneBind": []}):
            with self.subTest(table=table):
                self.assert_stage("read_failed", table)
        for key in ("scene", "sceneBind"):
            for bad in (None, "not a table", [None], {"id": "not a row"}):
                table = sample_table()
                table[key] = bad
                with self.subTest(key=key, bad=bad):
                    self.assert_stage("read_failed", table)

    def test_duplicate_scene_name_or_identity_is_ambiguous(self):
        for field in ("sceneName", "sceneNo"):
            table = sample_table()
            duplicate = {
                "sceneNo": "another-scene", "sceneId": 8,
                "sceneName": "别的场景", "delFlag": 0,
            }
            duplicate[field] = table["scene"][0][field]
            table["scene"].append(duplicate)
            with self.subTest(field=field):
                self.assert_stage("ambiguous_scene", table)

    def test_incomplete_result_indicators_are_rejected(self):
        for key, value in (("hasMore", True), ("has_more", 1), ("truncated", "true"),
                           ("isTruncated", None), ("incomplete", {}),
                           ("nextPage", 2), ("nextCursor", "cursor"),
                           ("continuationToken", "token")):
            table = sample_table()
            table[key] = value
            with self.subTest(key=key):
                self.assert_stage("read_failed", table)
        table = sample_table()
        table.update(hasMore=False, truncated="false", nextPage=None)
        self.assertEqual(speech.parse_speech_target(table, "HA播报", "测试").scene_no, "scene-no")

    def test_deleted_scene_is_not_selected(self):
        table = sample_table()
        table["scene"][0]["delFlag"] = "1"
        self.assert_stage("scene_not_found", table)

    def test_missing_flags_cannot_hide_extra_actions(self):
        for key in ("scene", "sceneBind"):
            for flag in (None, True, -1, "", "false", 0.0):
                table = sample_table()
                table[key][0]["delFlag"] = flag
                with self.subTest(key=key, flag=flag):
                    self.assert_stage("unsafe_scene", table)
        table = sample_table()
        table["scene"][0].pop("delFlag")
        table["sceneBind"][0].pop("delFlag")
        self.assertEqual(speech.parse_speech_target(table, "HA播报", "测试").scene_no, "scene-no")
        extra = deepcopy(table["sceneBind"][0])
        extra["sceneBindId"] = "other-bind"
        table["sceneBind"].append(extra)
        self.assert_stage("unsafe_scene", table)

    def test_sole_active_binding_is_required(self):
        table = sample_table()
        table["sceneBind"] = []
        self.assert_stage("unsafe_scene", table)
        table = sample_table()
        extra = deepcopy(table["sceneBind"][0])
        extra.update(sceneBindId="extra-id", order="on")
        table["sceneBind"].append(extra)
        self.assert_stage("unsafe_scene", table)
        extra["delFlag"] = 1
        self.assertEqual(
            speech.parse_speech_target(table, "HA播报", "测试").binding["order"],
            "tts play",
        )

    def test_duplicate_binding_id_is_rejected_even_if_marked_deleted(self):
        table = sample_table()
        duplicate = deepcopy(table["sceneBind"][0])
        duplicate["delFlag"] = 1
        table["sceneBind"].append(duplicate)
        self.assert_stage("unsafe_scene", table)

    def test_target_requires_explicit_identifiers_and_normal_tts_action(self):
        for key in ("sceneNo",):
            table = sample_table()
            table["scene"][0].pop(key)
            with self.subTest(missing=key):
                self.assert_stage("unsafe_scene", table)
        for key in ("sceneNo", "sceneBindId", "uid", "deviceId", "order"):
            table = sample_table()
            table["sceneBind"][0].pop(key)
            with self.subTest(missing=key):
                self.assert_stage("unsafe_scene", table)
        for key, value in (("order", "song control"), ("order", ["tts play"]),
                           ("sceneBindTag", 1), ("sceneBindTag", False),
                           ("uid", 123), ("deviceId", " ")):
            table = sample_table()
            table["sceneBind"][0][key] = value
            with self.subTest(key=key, value=value):
                self.assert_stage("unsafe_scene", table)

    def test_malformed_preserved_fields_are_not_sent(self):
        for key, value in (("value1", {"nested": "action"}), ("delayTime", float("nan")),
                           ("value1", True), ("value2", 2 ** 31), ("authList", [{}]),
                           ("authList", [{"userId": "owner", "isAuthorized": []}])):
            table = sample_table()
            table["sceneBind"][0][key] = value
            with self.subTest(key=key, value=value):
                self.assert_stage("unsafe_scene", table)

    def test_gson_primitive_defaults_match_official_binding_serializer(self):
        table = sample_table()
        for key in ("sceneBindTag", "value1", "value2", "value3", "value4", "freq",
                    "pluseNum", "delayTime", "themeId", "pluseData"):
            table["sceneBind"][0].pop(key, None)
        table["sceneBind"][0]["authList"] = None
        target = speech.parse_speech_target(table, "HA播报", "测试")
        for key in ("sceneBindTag", "value1", "value2", "value3", "value4", "freq",
                    "pluseNum", "delayTime"):
            self.assertEqual(target.binding[key], 0)
        self.assertEqual(target.binding["themeId"], "")
        self.assertNotIn("pluseData", target.binding)
        self.assertNotIn("authList", target.binding)


class SpeechManagerTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, table: object = None):
        read = AsyncMock(return_value=sample_table() if table is None else table)
        update = AsyncMock(return_value=True)
        execute = AsyncMock(return_value=True)
        return speech.SpeechManager(read, update, execute), read, update, execute

    async def test_valid_request_updates_then_executes_without_text_in_result(self):
        manager, read, update, execute = self.make_manager()
        events = []

        async def update_action(scene_no, binding):
            events.append(("update", scene_no, binding["actionName"]))
            return True

        async def execute_action(scene_no, scene_id):
            events.append(("execute", scene_no, scene_id))
            return True

        update.side_effect = update_action
        execute.side_effect = execute_action
        result = await manager.speak("HA播报", "门已打开")
        self.assertEqual(events, [("update", "scene-no", "门已打开"),
                                  ("execute", "scene-no", 7)])
        self.assertEqual(result, {"stage": "speech_scene_executed", "scene_no": "scene-no"})
        self.assertNotIn("门已打开", repr(result))
        read.assert_awaited_once_with()
        update.assert_awaited_once()
        execute.assert_awaited_once_with("scene-no", 7)

    async def test_invalid_input_does_not_read_or_send(self):
        manager, read, update, execute = self.make_manager()
        for name, message, stage in (("HA播报", " ", "invalid_message"),
                                      (123, "测试", "invalid_scene")):
            with self.assertRaises(speech.SpeechError) as raised:
                await manager.speak(name, message)
            self.assertEqual(raised.exception.stage, stage)
        read.assert_not_awaited()
        update.assert_not_awaited()
        execute.assert_not_awaited()

    async def test_optional_scene_id_does_not_block_execution_by_scene_number(self):
        table = sample_table()
        table["scene"][0].pop("sceneId")
        manager, read, update, execute = self.make_manager(table)
        result = await manager.speak("HA播报", "测试")
        self.assertEqual(result["stage"], "speech_scene_executed")
        update.assert_awaited_once()
        execute.assert_awaited_once_with("scene-no", None)

    async def test_unsafe_scene_does_not_update_or_execute(self):
        table = sample_table()
        table["sceneBind"][0]["order"] = "on"
        manager, read, update, execute = self.make_manager(table)
        with self.assertRaises(speech.SpeechError) as raised:
            await manager.speak("HA播报", "测试")
        self.assertEqual(raised.exception.stage, "unsafe_scene")
        read.assert_awaited_once()
        update.assert_not_awaited()
        execute.assert_not_awaited()

    async def test_callback_failures_have_stage_and_are_never_retried(self):
        for callback_name in ("read", "update", "execute"):
            for error in (ConnectionError("private text"), TimeoutError("private text"),
                          RuntimeError("private text")):
                manager, read, update, execute = self.make_manager()
                callbacks = {"read": read, "update": update, "execute": execute}
                callbacks[callback_name].side_effect = error
                with self.subTest(callback=callback_name, error=type(error).__name__):
                    with self.assertRaises(speech.SpeechError) as raised:
                        await manager.speak("HA播报", "正文")
                    self.assertEqual(raised.exception.stage, f"{callback_name}_failed")
                    self.assertNotIn("private", str(raised.exception))
                    callbacks[callback_name].assert_awaited_once()
                    if callback_name in ("read", "update"):
                        execute.assert_not_awaited()
                    if callback_name == "read":
                        update.assert_not_awaited()

    async def test_both_writes_require_positive_acknowledgement(self):
        for callback_name in ("update", "execute"):
            for ack in (False, None, {}, 1):
                manager, read, update, execute = self.make_manager()
                callback = update if callback_name == "update" else execute
                callback.return_value = ack
                with self.subTest(callback=callback_name, ack=ack):
                    with self.assertRaises(speech.SpeechError) as raised:
                        await manager.speak("HA播报", "测试")
                    self.assertEqual(raised.exception.stage, f"{callback_name}_failed")
                    callback.assert_awaited_once()
                    if callback_name == "update":
                        execute.assert_not_awaited()

    async def test_each_transaction_reads_latest_configuration(self):
        manager, read, update, execute = self.make_manager()
        changed = sample_table()
        changed["sceneBind"][0]["order"] = "on"
        read.side_effect = [sample_table(), changed]
        await manager.speak("HA播报", "第一条")
        with self.assertRaises(speech.SpeechError) as raised:
            await manager.speak("HA播报", "第二条")
        self.assertEqual(raised.exception.stage, "unsafe_scene")
        self.assertEqual(read.await_count, 2)
        update.assert_awaited_once()
        execute.assert_awaited_once()

    async def test_concurrent_calls_cannot_replace_text_before_execution(self):
        table = sample_table()
        events = []
        first_update_entered = asyncio.Event()
        release_first_update = asyncio.Event()
        second_started = asyncio.Event()

        async def read_table():
            events.append("read")
            return deepcopy(table)

        async def update_scene(scene_no, binding):
            text = binding["actionName"]
            events.append(("update", text))
            table["sceneBind"][0]["actionName"] = text
            if text == "第一条":
                first_update_entered.set()
                await release_first_update.wait()
            return True

        async def execute_scene(scene_no, scene_id):
            events.append(("execute", table["sceneBind"][0]["actionName"]))
            return True

        manager = speech.SpeechManager(read_table, update_scene, execute_scene)
        first = asyncio.create_task(manager.speak("HA播报", "第一条"))
        await first_update_entered.wait()

        async def second_call():
            second_started.set()
            return await manager.speak("HA播报", "第二条")

        second = asyncio.create_task(second_call())
        await second_started.wait()
        self.assertEqual(events, ["read", ("update", "第一条")])
        release_first_update.set()
        await asyncio.gather(first, second)
        self.assertEqual(events, ["read", ("update", "第一条"), ("execute", "第一条"),
                                  "read", ("update", "第二条"), ("execute", "第二条")])
        self.assertEqual(table["sceneBind"][0]["actionName"], "第二条")

    async def test_cancellation_is_propagated_without_execute_or_retry(self):
        manager, read, update, execute = self.make_manager()
        update.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await manager.speak("HA播报", "取消这次")
        update.assert_awaited_once()
        execute.assert_not_awaited()
        update.side_effect = None
        await manager.speak("HA播报", "下次仍可调用")
        self.assertEqual(read.await_count, 2)
        execute.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
