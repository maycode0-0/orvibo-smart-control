"""Update and execute an explicitly selected, dedicated announcement scene.

This integration explicitly persists TTS text in sceneBind.actionName (cmd195)
and then executes the saved scene (cmd197). This module never creates a scene,
removes an action, or retries a write whose result may be uncertain.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import math
import re
from typing import Any


# Match the ordinary announcement editor (not the personalized wake-word editor).
# Java String.length() counts UTF-16 units; util.m2.g supplies the character set.
MAX_SPEECH_LENGTH = 300
_SPEECH_TEXT = re.compile(r"[0-9A-Za-z\u4e00-\u9fa5 \t\n\x0b\f\r，。！？,.!?]+")

# com.orvibo.homemate.util.d3.g(SceneBind, false), used by core.c.l2/cmd195.
# Database metadata and the readtable alias `command` are not wire fields.
SCENE_BIND_FIELDS = (
    "deviceId", "sceneBindId", "uid", "actionName", "order",
    "value1", "value2", "value3", "value4", "freq", "pluseNum",
    "pluseData", "delayTime", "themeId", "sceneBindTag", "sceneBindTagId",
    "authList",
)
_INTEGER_FIELDS = {
    "value1", "value2", "value3", "value4", "freq", "pluseNum",
    "delayTime", "sceneBindTag",
}


class SpeechError(ValueError):
    """A speech failure with a stable stage and no announcement text."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(stage)


@dataclass(frozen=True, slots=True)
class SceneSpeechTarget:
    """One existing scene and its sole, updated TTS binding."""

    scene_no: str
    scene_id: int | None
    binding: dict[str, Any] = field(repr=False)


def validate_message(message: object) -> str:
    """Require actual text; preserve the user's text without truncation."""

    if not isinstance(message, str) or not message.strip():
        raise SpeechError("invalid_message")
    try:
        length = len(message.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        raise SpeechError("invalid_message") from None
    if length > MAX_SPEECH_LENGTH or not _SPEECH_TEXT.fullmatch(message):
        raise SpeechError("invalid_message")
    return message


def _validate_scene_name(scene_name: object) -> str:
    if not isinstance(scene_name, str) or not scene_name.strip():
        raise SpeechError("invalid_scene")
    # Match the saved name exactly, including intentional surrounding spaces.
    return scene_name


def _scene_id(value: object) -> int | None:
    # core.c.R0 still executes by sceneNo when the optional sceneId is <= 0.
    if value is None:
        return None
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and (
        value.isdecimal() or value.startswith("-") and value[1:].isdecimal()
    ):
        try:
            parsed = int(value)
        except ValueError:
            raise SpeechError("unsafe_scene") from None
        return parsed if parsed > 0 else None
    raise SpeechError("unsafe_scene")


def _text_identifier(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpeechError("unsafe_scene")
    return value


def _active(row: Mapping[str, Any]) -> bool:
    # Omitted default flags count as active, so they cannot hide extra actions.
    if "delFlag" not in row:
        return True
    flag = row.get("delFlag")
    if type(flag) is int and flag in (0, 1):
        return flag == 0
    if isinstance(flag, str) and flag in ("0", "1"):
        return flag == "0"
    raise SpeechError("unsafe_scene")


def validate_complete_readtable(data: Mapping[str, Any]) -> None:
    """Reject visible partial-result markers without guessing wrapper schemas."""

    for key in ("hasMore", "has_more", "more", "truncated", "isTruncated", "incomplete"):
        if key in data and not (
            data[key] is False or type(data[key]) is int and data[key] == 0
            or isinstance(data[key], str) and data[key] in ("0", "false")
        ):
            raise SpeechError("read_failed")
    for key in ("next", "nextPage", "nextCursor", "nextToken", "continuationToken"):
        if key in data and data[key] not in (None, "", False, 0):
            raise SpeechError("read_failed")


def _rows(data: Mapping[str, Any], table: str, key_field: str) -> list[Mapping[str, Any]]:
    if table not in data:
        raise SpeechError("read_failed")
    raw = data[table]
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, Mapping):
        rows = []
        for key, row in raw.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(row, Mapping):
                raise SpeechError("read_failed")
            if key_field in row and row[key_field] != key:
                raise SpeechError("read_failed")
            rows.append({key_field: key, **row})
    else:
        raise SpeechError("read_failed")
    if any(not isinstance(row, Mapping) for row in rows):
        raise SpeechError("read_failed")
    return rows


def _scalar(value: object) -> bool:
    return (
        value is None
        or isinstance(value, (str, bool, int))
        or isinstance(value, float) and math.isfinite(value)
    )


def _wire_integer(value: object) -> int:
    # Gson's primitive int fields default to zero for absent/null JSON values.
    if value is None:
        return 0
    if type(value) is int:
        number = value
    elif isinstance(value, str) and value.isascii() and (
        value.isdecimal() or value.startswith("-") and value[1:].isdecimal()
    ):
        try:
            number = int(value)
        except ValueError:
            raise SpeechError("unsafe_scene") from None
    else:
        raise SpeechError("unsafe_scene")
    if not -(2 ** 31) <= number < 2 ** 31:
        raise SpeechError("unsafe_scene")
    return number


def _binding_for_write(row: Mapping[str, Any], message: str) -> dict[str, Any]:
    """Keep the official serializer's fields and change only the spoken text."""

    for key in ("deviceId", "uid", "sceneBindId"):
        _text_identifier(row.get(key))
    if _wire_integer(row.get("sceneBindTag")) != 0:
        raise SpeechError("unsafe_scene")

    orders = [row[key] for key in ("order", "command") if key in row]
    if not orders or any(order != "tts play" for order in orders):
        raise SpeechError("unsafe_scene")

    binding: dict[str, Any] = {}
    for key in SCENE_BIND_FIELDS:
        if key in _INTEGER_FIELDS:
            binding[key] = _wire_integer(row.get(key))
            continue
        if key == "themeId" and key not in row:
            # Action's no-argument constructor initializes this string to empty.
            binding[key] = ""
            continue
        if key not in row or key in ("order", "actionName"):
            continue
        value = row[key]
        # JSONObject.put removes null strings; d3.g omits an empty authList.
        if value is None:
            continue
        if key == "authList":
            if not isinstance(value, list) or any(
                not isinstance(item, Mapping)
                or "userId" not in item or "isAuthorized" not in item
                or not _scalar(item["userId"]) or not _scalar(item["isAuthorized"])
                for item in value
            ):
                raise SpeechError("unsafe_scene")
            if not value:
                continue
            binding[key] = [
                {field: deepcopy(item[field]) for field in ("userId", "isAuthorized")}
                for item in value
            ]
        else:
            if not isinstance(value, str):
                raise SpeechError("unsafe_scene")
            binding[key] = deepcopy(value)
    binding["order"] = "tts play"
    binding["actionName"] = message
    return binding


def parse_speech_target(
    readtable: object, scene_name: object, message: object,
) -> SceneSpeechTarget:
    """Fail closed unless both complete tables describe exactly one TTS action.

    Input is the inner readtable dictionary, with array or ID-keyed tables.
    Invalid rows are rejected rather than discarded, since discarding a row
    could make a scene with additional actions appear safe to execute.
    """

    name = _validate_scene_name(scene_name)
    text = validate_message(message)
    if not isinstance(readtable, Mapping):
        raise SpeechError("read_failed")
    validate_complete_readtable(readtable)
    scenes = _rows(readtable, "scene", "sceneNo")
    bindings = _rows(readtable, "sceneBind", "sceneBindId")

    matched: list[Mapping[str, Any]] = []
    scene_numbers: set[str] = set()
    for scene in scenes:
        scene_no = _text_identifier(scene.get("sceneNo"))
        if scene_no in scene_numbers:
            raise SpeechError("ambiguous_scene")
        scene_numbers.add(scene_no)
        active = _active(scene)
        if active and scene.get("sceneName") == name:
            matched.append(scene)
    if not matched:
        raise SpeechError("scene_not_found")
    if len(matched) != 1:
        raise SpeechError("ambiguous_scene")
    scene = matched[0]

    active_bindings: list[Mapping[str, Any]] = []
    binding_ids: set[str] = set()
    for row in bindings:
        scene_no = _text_identifier(row.get("sceneNo"))
        binding_id = _text_identifier(row.get("sceneBindId"))
        if binding_id in binding_ids:
            raise SpeechError("unsafe_scene")
        binding_ids.add(binding_id)
        active = _active(row)
        if active and scene_no == scene["sceneNo"]:
            active_bindings.append(row)
    if len(active_bindings) != 1:
        raise SpeechError("unsafe_scene")

    return SceneSpeechTarget(
        scene_no=scene["sceneNo"],
        scene_id=_scene_id(scene.get("sceneId")),
        binding=_binding_for_write(active_bindings[0], text),
    )


class SpeechManager:
    """Serialize read/update/execute transactions within one coordinator."""

    def __init__(
        self,
        read_table: Callable[[], Awaitable[object]],
        update_scene: Callable[[str, dict[str, Any]], Awaitable[bool]],
        execute_scene: Callable[[str, int | None], Awaitable[bool]],
    ) -> None:
        self._read_table = read_table
        self._update_scene = update_scene
        self._execute_scene = execute_scene
        self._lock = asyncio.Lock()

    async def speak(self, scene_name: object, message: object) -> dict[str, Any]:
        """Persist the last text and execute once, only after update succeeds."""

        name = _validate_scene_name(scene_name)
        text = validate_message(message)
        async with self._lock:
            try:
                table = await self._read_table()
            except (OSError, RuntimeError, ValueError):
                raise SpeechError("read_failed") from None
            target = parse_speech_target(table, name, text)
            try:
                updated = await self._update_scene(target.scene_no, target.binding)
            except (OSError, RuntimeError, ValueError):
                raise SpeechError("update_failed") from None
            if updated is not True:
                raise SpeechError("update_failed")
            try:
                executed = await self._execute_scene(target.scene_no, target.scene_id)
            except (OSError, RuntimeError, ValueError):
                raise SpeechError("execute_failed") from None
            if executed is not True:
                raise SpeechError("execute_failed")
            return {"stage": "speech_scene_executed", "scene_no": target.scene_no}
