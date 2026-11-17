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
import logging
import math
import re
import time
from typing import Any
from uuid import uuid4

_LOGGER = logging.getLogger(__name__)
FRESHNESS_REVISION = 2
UPDATE_ACK_REVISION = 1


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
class SpeechUpdateResult:
    """A correlated update ACK with a bounded, text-free public summary."""

    accepted: bool
    details: dict[str, Any] = field(repr=False)

    def __bool__(self) -> bool:
        return self.accepted


def summarize_update_ack(response: object, binding: Mapping[str, Any]) -> SpeechUpdateResult:
    """Check cmd195 action results without treating an ACK as playback proof.

    The reference APK parses successList as SceneBind objects and failList as
    action failures (core.g.T0 / core.f.F1 / core.f.D1). Missing action lists
    remain explicitly unconfirmed, with the existing cloud readback required.
    Do not copy arbitrary response values or speech into the public result.
    """

    if not isinstance(response, Mapping):
        return SpeechUpdateResult(False, {"response_received": False})
    status = response.get("status")
    status_zero = type(status) is int and status == 0 or (
        isinstance(status, str) and status == "0"
    )
    details: dict[str, Any] = {
        "response_received": True,
        "status_zero": status_zero,
        "success_list_present": "successList" in response,
        "fail_list_present": "failList" in response,
        "binding_confirmation": "not_reported",
    }
    accepted = status_zero
    for field_name, label in (("failList", "fail_count"), ("successList", "success_count")):
        if field_name not in response:
            details[label] = None
            continue
        rows = response[field_name]
        if not isinstance(rows, list):
            details[label] = None
            details["list_format_valid"] = False
            accepted = False
            continue
        details[label] = len(rows)
        if field_name == "failList" and rows:
            accepted = False
    if "successList" in response:
        rows = response["successList"]
        # This request updates exactly one existing action. Reject a different
        # action or conflicting echoed identifiers rather than guessing.
        matches = (
            isinstance(rows, list) and len(rows) == 1
            and isinstance(rows[0], Mapping)
            and rows[0].get("sceneBindId") == binding.get("sceneBindId")
            and all(
                key not in rows[0] or rows[0][key] == binding.get(key)
                for key in ("uid", "deviceId")
            )
        )
        details["binding_confirmation"] = "matched" if matches else "not_matched"
        accepted = accepted and matches
        if matches and "actionName" in rows[0]:
            text_matches = rows[0]["actionName"] == binding.get("actionName")
            details["returned_text_matches"] = text_matches
            accepted = accepted and text_matches
    details["accepted"] = bool(accepted)
    return SpeechUpdateResult(bool(accepted), details)


@dataclass(frozen=True, slots=True)
class SceneSpeechTarget:
    """One existing scene and its sole, updated TTS binding."""

    scene_no: str
    scene_id: int | None
    binding: dict[str, Any] = field(repr=False)
    stored_text: object = field(default=None, repr=False)


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
        stored_text=deepcopy(active_bindings[0].get("actionName")),
    )


class SpeechManager:
    """Serialize read/update/execute transactions within one coordinator."""

    def __init__(
        self,
        read_table: Callable[[], Awaitable[object]],
        update_scene: Callable[[str, dict[str, Any]], Awaitable[bool | SpeechUpdateResult]],
        execute_scene: Callable[[str, int | None], Awaitable[bool]],
    ) -> None:
        self._read_table = read_table
        self._update_scene = update_scene
        self._execute_scene = execute_scene
        self._lock = asyncio.Lock()

    async def speak(self, scene_name: object, message: object) -> dict[str, Any]:
        """Require a matching cloud readback before executing exactly once.

        Readback verifies cloud configuration, not the panel's playback cache.
        A stale, changed or unreadable scene is never executed by this call.
        """

        name = _validate_scene_name(scene_name)
        text = validate_message(message)
        async with self._lock:
            request_id = uuid4().hex[:12]
            started = time.monotonic()
            try:
                table = await self._read_table()
            except (OSError, RuntimeError, ValueError):
                raise SpeechError("read_failed") from None
            target = parse_speech_target(table, name, text)
            read_done = time.monotonic()
            try:
                updated = await self._update_scene(target.scene_no, target.binding)
            except (OSError, RuntimeError, ValueError):
                raise SpeechError("update_failed") from None
            if isinstance(updated, SpeechUpdateResult):
                update_ack = deepcopy(updated.details)
                update_accepted = updated.accepted is True
                ack_revision = UPDATE_ACK_REVISION
            else:
                # A speech.py-only upload can still run with the older SSL
                # client; report this explicitly instead of claiming new checks.
                update_ack = {"binding_confirmation": "client_not_upgraded"}
                update_accepted = updated is True
                ack_revision = 0
            if not update_accepted:
                _LOGGER.warning(
                    "MixPad speech update rejected request_id=%s ack=%s; scene not executed",
                    request_id, update_ack,
                )
                raise SpeechError("update_failed")
            update_done = time.monotonic()
            try:
                verified = parse_speech_target(await self._read_table(), name, text)
            except (OSError, RuntimeError, ValueError):
                _LOGGER.warning("MixPad speech freshness revision=%s stage=readback_failed; scene not executed", FRESHNESS_REVISION)
                raise SpeechError("update_failed") from None
            # Preserve exactly the originally approved scene, device and action.
            # parse_speech_target normalizes wire fields; stored_text separately
            # retains the actual readback instead of substituting requested text.
            if (verified.scene_no != target.scene_no
                    or verified.binding != target.binding
                    or verified.stored_text != text):
                _LOGGER.warning(
                    "MixPad speech freshness revision=%s stage=readback_mismatch text_matches=%s binding_matches=%s; scene not executed",
                    FRESHNESS_REVISION, verified.stored_text == text,
                    verified.scene_no == target.scene_no and verified.binding == target.binding,
                )
                raise SpeechError("update_failed")
            verify_done = time.monotonic()
            scene_id_changed = verified.scene_id != target.scene_id
            _LOGGER.info(
                "MixPad speech freshness revision=%s stage=cloud_text_verified scene_id_changed=%s read_ms=%d update_ms=%d verify_ms=%d",
                FRESHNESS_REVISION, scene_id_changed, round((read_done - started) * 1000),
                round((update_done - read_done) * 1000), round((verify_done - update_done) * 1000),
            )
            try:
                executed = await self._execute_scene(verified.scene_no, verified.scene_id)
            except (OSError, RuntimeError, ValueError):
                raise SpeechError("execute_failed") from None
            if executed is not True:
                raise SpeechError("execute_failed")
            execute_done = time.monotonic()
            _LOGGER.info(
                "MixPad speech freshness revision=%s stage=scene_executed execute_ms=%d; panel sound not verified",
                FRESHNESS_REVISION, round((execute_done - verify_done) * 1000),
            )
            return {"stage": "speech_scene_executed", "scene_no": target.scene_no,
                    "freshness_revision": FRESHNESS_REVISION, "cloud_text_verified": True,
                    "scene_id_refreshed": scene_id_changed, "panel_sound_verified": False,
                    "request_id": request_id, "update_ack_revision": ack_revision,
                    "update_ack": update_ack,
                    "timing_ms": {
                        "read": round((read_done - started) * 1000),
                        "update": round((update_done - read_done) * 1000),
                        "verify": round((verify_done - update_done) * 1000),
                        "execute": round((execute_done - verify_done) * 1000),
                        "total": round((execute_done - started) * 1000),
                    }}
