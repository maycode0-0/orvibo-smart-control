"""Normalized door-lock state and event parsing (pure stdlib, no HA deps).

门锁状态存在两种已观察到的协议形态：

- 形态 A（type=522 / classId=463，V5 Eyes 门锁）::

    properties.doorLock = {
        "lockState": "on" | "off",
        "doorState": "on" | "off",
        "insideLockState": "on" | "off",
    }

- 形态 B（属性型 ThingModel，type=300/522 子类型）::

    properties = {
        "door_status":  "open" | "closed",
        "reverse_lock": "locked" | "unlocked",
        "handle":       "locked" | "unlocked",
        "clild_lock":   "on" | "off",
    }

电池（形态 A）: ``properties.batteryManager``（干电池）与
``properties.batteryManager1``（锂电池），值为 ``{"level": int, "isSetupBattery": "on"|"off"}``。

事件（cmd=352）::

    {"event": {"server": "doorLock" | "doorbell",
               "name": "unlockEvent" | "ring" | "answered" | "bye",
               "value": {...}}}
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, Optional


def _normalize_state(value: Any) -> Optional[bool]:
    """Map protocol state strings to booleans; return None for unknown shapes."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"on", "open", "locked", "up", "1", "true"}:
        return True
    if normalized in {"off", "closed", "unlocked", "down", "0", "false"}:
        return False
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _property_group(properties: Mapping[str, Any], group: str) -> dict[str, Any]:
    """Read nested pushes and App DevicePropertyStatus server_name leaf keys."""
    prefix = group + "_"
    fields = dict(_mapping(properties.get(group)))
    # APK BasePropertyMap.getObjectKey(): server + "_" + name. Independent
    # readtable property rows follow inline properties in protocol.py.
    fields.update({
        key[len(prefix):]: value
        for key, value in properties.items()
        if isinstance(key, str) and key.startswith(prefix)
    })
    return fields


def normalize_battery_level(value: Any) -> Optional[int]:
    """Accept only actual percentages, including a depleted battery's zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        level = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(level) or not level.is_integer() or not 0 <= level <= 100:
        return None
    return int(level)


def normalize_unlock_method(value: Any) -> Optional[str]:
    """Normalize named ThingModel methods; numeric legacy codes are separate."""
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return {
        "fingerprint": "fingerprint", "password": "password",
        "card": "card", "key": "key", "face": "face",
        "temporary_password": "temporary_password",
        "temp password": "temporary_password",
        "temp_password": "temporary_password",
        "temppassword": "temporary_password",
        "emergency fingerprint": "fingerprint",
        "emergency password": "password", "emergency card": "card",
    }.get(value)


def unlock_direction(method: Optional[str]) -> str:
    """Apply the configured policy: four credential families outside, all else inside."""
    if method in {"fingerprint", "password", "card", "key", "temporary_password"}:
        return "outside"
    return "inside"


def normalize_door_lock_properties(properties: Any) -> dict[str, Any]:
    """Normalize either lock property morphology to one boolean dict."""
    props = _mapping(properties)
    result: dict[str, Any] = {
        "locked": None,
        "door_open": None,
        "inside_locked": None,
        "child_locked": None,
        "leave_home_armed": None,
        "raw": {},
    }

    door_lock = _property_group(props, "doorLock")
    if door_lock:
        # V5 Eyes（type=522/classId=463）实机验证（反语义，与 App 显示一致）：
        # lockState="on"=已解锁、"off"=已锁定；
        # insideLockState="off"=门内反锁中、"on"=反锁解除（181826 反锁/解除抓包）。
        lock_raw = _normalize_state(door_lock.get("lockState"))
        result["locked"] = None if lock_raw is None else not lock_raw
        result["door_open"] = _normalize_state(door_lock.get("doorState"))
        inside_raw = _normalize_state(door_lock.get("insideLockState"))
        result["inside_locked"] = None if inside_raw is None else not inside_raw
        result["raw"]["doorLock"] = dict(door_lock)

    # App renders handle as the main lock and reverse_lock as the independent deadbolt.
    reverse = _normalize_state(props.get("reverse_lock"))
    if reverse is not None:
        result["inside_locked"] = reverse
    for key in ("handle", "lock_state", "lockStatus"):
        value = _normalize_state(props.get(key))
        if value is not None:
            result["locked"] = value
    for key in ("door_status",):
        value = _normalize_state(props.get(key))
        if value is not None:
            result["door_open"] = value
    child = _normalize_state(props.get("clild_lock"))
    if child is not None:
        result["child_locked"] = child
    armed = _normalize_state(door_lock.get("leaveHomeAlarmCfg"))
    if armed is None:
        armed = _normalize_state(props.get("leaveHomeAlarmCfg"))
    if armed is not None:
        result["leave_home_armed"] = armed
    result["raw"]["flat"] = {
        key: props[key]
        for key in (
            "door_status",
            "reverse_lock",
            "handle",
            "clild_lock",
            "lock_state",
            "lockStatus",
            "leaveHomeAlarmCfg",
        )
        if key in props
    }
    return result


def normalize_battery_properties(properties: Any) -> dict[str, Any]:
    """Normalize legacy AA percentages and dry/lithium battery managers."""
    props = _mapping(properties)
    result: dict[str, Any] = {}
    # APK IntProperty.BatteryPower and type 300/400 AA lock diagnostics use
    # battery_power (0..100). Managers below retain precedence, including removal.
    legacy_level = normalize_battery_level(props.get("battery_power"))
    if legacy_level is not None:
        result["dry_battery_level"] = legacy_level
    for source_key, out_prefix in (
        ("batteryManager", "dry"),
        ("batteryManager1", "lithium"),
    ):
        manager = _property_group(props, source_key)
        if not manager:
            continue
        level = normalize_battery_level(manager.get("level"))
        # Missing/invalid partial readings must not erase the last valid percentage.
        if level is not None:
            result[f"{out_prefix}_battery_level"] = level
        setup = _normalize_state(manager.get("isSetupBattery"))
        if setup is not None:
            result[f"{out_prefix}_battery_setup"] = setup
            # isSetupBattery=off 表示电池被取出/未安装（实测：扣电池触发推送），
            # level=0 无意义，置为未知
            if not setup:
                result[f"{out_prefix}_battery_level"] = None
    return result


def normalize_lock_event(payload: Any) -> Optional[dict[str, Any]]:
    """Normalize cmd=352 doorbell/unlock events for the HA event bus."""
    if not isinstance(payload, Mapping):
        return None
    # Legacy StatusRecord: value2=65535 marks a standalone alarm code.
    # Do not apply this numeric enum to ThingModel event.value.type.
    if (
        not isinstance(payload.get("event"), Mapping)
        and payload.get("cmd") == 42
        and str(payload.get("value2")) == "65535"
    ):
        code = str(payload.get("value4"))
        method = {
            "1": "fingerprint", "2": "password", "3": "card",
            "4": "temporary_password", "5": "key",
        }.get(code)
        if code == "20" or method is not None:
            return {
                "kind": "unlock", "unlock_type": method or "inside",
                "unlock_method": method,
                "open_direction": "inside" if code == "20" else "outside",
                "unlock_user_id": None, "time": payload.get("time"),
            }
    event = payload.get("event")
    if not isinstance(event, Mapping):
        return None
    server = event.get("server")
    name = event.get("name")
    value = _mapping(event.get("value"))
    event_time = payload.get("time")
    if event_time is None:
        event_time = _mapping(payload.get("raw_data")).get("time")

    if (server == "doorLock" and name == "unlockEvent") or (
        server in (None, "doorLock") and name == "unlock"
    ):
        method = normalize_unlock_method(value.get("type"))
        primary_id = "userId" if name == "unlockEvent" else "userID"
        alternate_id = "userID" if primary_id == "userId" else "userId"
        user_id = value.get(primary_id, value.get(alternate_id))
        if unlock_direction(method) == "inside":
            user_id = None
        if isinstance(user_id, bool) or not isinstance(user_id, (str, int)):
            user_id = None
        return {
            "kind": "unlock",
            "unlock_type": value.get("type"),
            "unlock_method": method,
            "open_direction": unlock_direction(method),
            "unlock_user_id": user_id,
            "pic_url": value.get("picture_url") or value.get("url"),
            "time": event_time,
        }
    if server in ("doorLock", "security") and name == "errorUnlockEvent":
        return {
            "kind": "error_unlock",
            "unlock_type": value.get("type"),
            "time": event_time,
        }
    if server in ("doorLock", "security") and name in ("doorUnclose", "doorUnclosed"):
        return {"kind": "door_unclose", "time": event_time}
    if server in ("doorLock", "security") and name == "picklockEvent":
        return {
            "kind": "picklock",
            "video_url": value.get("videoUrl"),
            "pic_url": value.get("url"),
            "time": event_time,
        }
    if server in ("doorLock", "security") and name == "leaveHomeEvent":
        return {
            "kind": "leave_home",
            "video_url": value.get("videoUrl"),
            "pic_url": value.get("url"),
            "time": event_time,
        }
    if server == "security" and name == "staying":
        return {
            "kind": "loiter", "pic_url": value.get("url") or value.get("picture_url"),
            "video_url": value.get("videoUrl"), "time": event_time,
        }
    if (server == "doorbell" and name == "ring") or (
        server in (None, "doorbell") and name == "doorbell_ring"
    ):
        return {
            "kind": "ring",
            "doorbell_url": value.get("url") or value.get("picture_url"),
            "doorbell_ip": value.get("doorbell_local_Ip"),
            "time": event_time,
        }
    if server in (None, "doorbell") and name == "answered":
        return {"kind": "answered", "time": event_time}
    if server in (None, "doorbell") and name == "bye":
        return {"kind": "bye", "time": event_time}
    return None


def normalize_message_event(payload: Any) -> Optional[dict[str, Any]]:
    """Normalize cmd=82 push-message packets (门锁等文本消息/告警)。"""
    if not isinstance(payload, Mapping):
        return None
    if payload.get("cmd") != 82:
        return None
    data = payload.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            data = {}
    if not isinstance(data, Mapping):
        data = {}

    info_type = payload.get("infoType")
    device_type = data.get("deviceType")
    if info_type != 12 and device_type not in (522, 107, 300):
        return None
    return {
        "kind": "message",
        "device_id": data.get("deviceId") or payload.get("deviceId") or "",
        "uid": data.get("uid") or payload.get("uid") or "",
        "is_alarm": data.get("isAlarm"),
        "message_type": payload.get("messageType"),
        "text": payload.get("text"),
        "pic_url": data.get("picUrl"),
        "time": data.get("time"),
    }


def resolve_opened_by(
    last_unlock: Any,
    elapsed: float,
    window: float = 30,
) -> Optional[dict[str, Any]]:
    """把一次开门事件归属到窗口内最近一次开锁（返回 user_id + 开锁方式）。"""
    if not isinstance(last_unlock, Mapping):
        return None
    if (
        not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0 or elapsed > window
    ):
        return None
    user_id = last_unlock.get("user_id")
    if user_id is None:
        return None
    return {
        "user_id": str(user_id),
        "unlock_type": last_unlock.get("unlock_type"),
    }


def derive_lock_status(
    locked: Optional[bool],
    door_open: Optional[bool],
    inside_locked: Optional[bool],
) -> Optional[str]:
    """从归一化字段推导面向用户的锁状态。

    规则（绑定门磁）：门磁开 + 锁上锁 = 异常（异常关门/测试状态）；
    门磁开 = 未上锁；门磁关 = 门内反锁/上锁/未上锁。
    """
    if door_open is True and locked is True:
        return "abnormal"
    if door_open is True:
        return "unlocked"
    if inside_locked is True:
        return "inside_locked"
    if locked is True:
        return "locked"
    if locked is False:
        return "unlocked"
    return None


def format_unlock_label(
    user_id: Any, name: Optional[str], direction: Optional[str] = None,
    method: Optional[str] = None,
) -> str:
    """生成"xxx开门"显示文本（未配置名称时回退为 用户{id}）。"""
    if direction is not None:
        direction = unlock_direction(method)
        if direction == "inside":
            return "内开"
        who = str(name).strip() if name else ""
        if not who and user_id not in (None, ""):
            who = f"用户{user_id}"
        label = {"outside": "外开", "unknown": "开门（方向未知）"}.get(direction, "开门（方向未知）")
        method_label = {
            "fingerprint": "指纹", "password": "密码", "card": "卡片",
            "key": "钥匙", "face": "人脸", "temporary_password": "临时密码",
        }.get(method)
        details = "，".join(item for item in (method_label, "人员未知" if not who else None) if item)
        return f"{who}{label}" + (f"（{details}）" if details else "")
    if name and str(name).strip():
        return f"{name}开门"
    if user_id not in (None, ""):
        return f"用户{user_id}开门"
    return "无"


def parse_lock_user_names(text: Any) -> dict[str, str]:
    """解析多行"用户ID=名称"文本为映射，兼容冒号分隔，忽略空行/坏行。"""
    result: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        separator = "=" if "=" in line else (":" if ":" in line else None)
        if separator is None:
            continue
        user_id, name = line.split(separator, 1)
        user_id = user_id.strip()
        name = name.strip()
        if user_id and name:
            result[user_id] = name
    return result


def format_lock_user_names(mapping: Any) -> str:
    """把映射格式化为多行"用户ID=名称"，数字 ID 优先排序。"""
    if not isinstance(mapping, Mapping):
        return ""
    def sort_key(item: tuple[str, Any]) -> tuple[int, str]:
        user_id = str(item[0])
        return (0 if user_id.isdigit() else 1, user_id)

    return "\n".join(
        f"{user_id}={name}"
        for user_id, name in sorted(mapping.items(), key=sort_key)
    )
