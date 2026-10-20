"""纯协议层：Orvibo REST API 数据模型与签名/解析函数。

零外部依赖（仅 Python 标准库），可在无 Home Assistant 环境下独立测试。

包含：
- OrviboDevice dataclass（标准化设备描述）
- OrviboFamily dataclass（标准化家庭描述）
- 签名函数（password_hash / sign_request）
- 请求构建（build_family_request / build_readtable_request）
- 响应解析（parse_families / parse_readtable_devices）
- 二进制协议设备提取（extract_devices）
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import copy
import hashlib
import json
import hmac
import re
from typing import Any, Final, Mapping, Sequence

_HMAC_SECRET: Final = "nQ45RjPtOws96jmH"
_PASSWORD_HASH_PATTERN: Final = re.compile(r"^[A-F0-9]{32}$")


@dataclass(frozen=True, slots=True)
class OrviboFamily:
    """一个 Orvibo 家庭。"""

    family_id: str
    name: str


@dataclass(frozen=True, slots=True)
class OrviboDevice:
    """标准化设备描述（从 readtable 或 binary 协议提取）。"""

    uid: str                     # deviceId
    name: str                    # deviceName
    model: str                   # model / modelName
    device_type: str             # deviceType (字符串)
    sub_device_type: str         # subDeviceType (字符串)
    room: str                    # 房间名（从 roomId 映射）
    parent_uid: str              # 网关 parentId
    online: bool | None          # 在线状态
    cloud_uid: str = ""          # uid（硬件 UID，与 deviceId 不同）
    value1: int | None = None
    value2: int | None = None
    value3: int | None = None
    value4: int | None = None
    class_id: int | None = None  # classId（部分设备通过 classId 识别）
    ui_model: str = ""           # ui.model
    room_name: str = ""          # 直接从设备条目读取的 roomName（备选）
    ext_addr: str = ""           # extAddr（Zigbee 地址；旧摄像机中作为 DID）
    status_id: str = ""
    app_device_id: str = ""
    gateway_id: str = ""
    class_name: str = ""
    device_flag: int | None = None
    is_rf_curtain: bool = False
    combined_code: int | None = None
    endpoint: int = 0
    properties: Mapping[str, Any] = field(default_factory=dict)
    camera_type: int | None = None  # CameraInfo.type，与 Device.deviceType 不同
    room_id: str = ""
    status: Mapping[str, Any] = field(default_factory=dict)


def password_hash(password: str) -> str:
    """返回 Orvibo 要求的大写 MD5 密码哈希。"""
    return hashlib.md5(password.encode("utf-8")).hexdigest().upper()  # noqa: S324


def normalize_password_hash(value: object) -> str:
    """Validate and normalize a replayable ORVIBO password digest."""
    digest = str(value or "").strip().upper()
    if not _PASSWORD_HASH_PATTERN.fullmatch(digest):
        raise ValueError("password hash must be 32 hexadecimal characters")
    return digest


def migrate_password_credentials(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return config-entry data containing only the protocol password digest."""
    migrated = dict(data)
    digest = migrated.get("password_hash")
    if digest:
        migrated["password_hash"] = normalize_password_hash(digest)
    else:
        plaintext = migrated.get("password")
        if not isinstance(plaintext, str) or not plaintext:
            raise ValueError("config entry has no password credential")
        migrated["password_hash"] = password_hash(plaintext)
    migrated.pop("password", None)
    return migrated


def _sign_request(body: Mapping[str, Any]) -> str:
    """对 Orvibo REST 请求进行 HMAC-SHA256 签名。"""
    canonical = "&".join(f"{key}={body[key]}" for key in sorted(body))
    canonical = f"{canonical}&key={_HMAC_SECRET}"
    return hmac.new(
        _HMAC_SECRET.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().upper()


def build_family_request(
    access_token: str,
    user_id: str,
    timestamp_ms: int,
    nonce: int,
) -> dict[str, str]:
    """构建带签名的家庭列表请求体。"""
    body = {
        "accessToken": access_token,
        "userId": user_id,
        "timestamp": str(timestamp_ms),
        "random": str(nonce),
    }
    body["sign"] = _sign_request(body)
    return body


def build_readtable_request(
    access_token: str,
    user_id: str,
    family_id: str,
    session_id: str,
    timestamp_ms: int,
    serial: int,
    nonce: str,
    version: str = "5.2.6.302",
) -> dict[str, Any]:
    """构建 /v2/cmd/app/readtable 请求体。"""
    body: dict[str, Any] = {
        "accessToken": access_token,
        "dataType": "all",
        "deviceFlag": 0,
        "familyId": family_id,
        "lastUpdateTime": 0,
        "pageIndex": 0,
        "random": nonce,
        "serial": serial,
        "sessionId": session_id,
        "timestamp": timestamp_ms,
        "userId": user_id,
        "userName": user_id,
        "ver": version,
    }
    body["sign"] = _sign_request(body)
    return body


def _first_text(item: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    """按优先级返回第一个非空字符串字段值。"""
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_online(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "online", "connected"}:
            return True
        if normalized in {"0", "false", "offline", "disconnected"}:
            return False
    return None


def _parse_bool(value: Any) -> bool:
    """Normalize optional APK flags without treating arbitrary text as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _parse_room(
    item: Mapping[str, Any],
    room_names: Mapping[str, str],
) -> str:
    """从设备条目中提取房间名（优先 roomName 字段，再按 roomId 映射）。"""
    room_name = _first_text(item, ("roomName", "room"))
    if room_name:
        return room_name
    room_id = _first_text(item, ("roomId", "roomID"))
    if room_id and room_id in room_names:
        return room_names[room_id]
    return ""


def parse_families(payload: Mapping[str, Any]) -> tuple[OrviboFamily, ...]:
    """归一化家庭响应解析（兼容多种字段名）。"""
    raw: Any = payload.get("data", [])
    if isinstance(raw, Mapping):
        raw = raw.get("families") or raw.get("familyList") or raw.get("list") or []
    if not isinstance(raw, list):
        return ()

    families: list[OrviboFamily] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        family_id = str(
            item.get("familyId") or item.get("family_id") or item.get("id") or ""
        ).strip()
        if not family_id or family_id in seen:
            continue
        seen.add(family_id)
        name = str(
            item.get("familyName")
            or item.get("family_name")
            or item.get("name")
            or family_id
        ).strip()
        families.append(OrviboFamily(family_id=family_id, name=name or family_id))
    return tuple(families)


def parse_camera_types(raw: Any) -> dict[str, int | None]:
    """Read only CameraInfo.type, keyed by hardware UID; discard SDK secrets.

    Accept a row list or a UID-keyed table. Never join on CameraInfo.deviceId:
    the Android application queries this table with Device.uid.
    """
    if isinstance(raw, list):
        rows = ((None, row) for row in raw)
    elif isinstance(raw, Mapping):
        rows = raw.items()
    else:
        return {}

    result: dict[str, int | None] = {}
    for key, row in rows:
        if not isinstance(row, Mapping):
            continue
        uid = row.get("uid", key)
        if not isinstance(uid, str) or not uid.strip():
            continue
        uid = uid.strip()
        if row.get("delFlag") in (1, "1"):
            result[uid] = None
            continue
        value = row.get("type")
        # int(True) and int(8.5) would silently select the wrong SDK.
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            result[uid] = None
        elif isinstance(value, str) and not re.fullmatch(r"[+-]?[0-9]{1,10}", value.strip()):
            result[uid] = None
        else:
            result[uid] = int(value)
    return result


def _table_rows(raw: Any, key_field: str | None = None) -> tuple[dict[str, Any], ...]:
    """Normalize array/keyed tables without mutating rows or inventing identifiers."""
    if isinstance(raw, list):
        entries = ((None, row) for row in raw)
    elif isinstance(raw, Mapping):
        entries = raw.items()
    else:
        return ()
    rows = []
    for key, row in entries:
        if not isinstance(row, Mapping):
            continue
        row = dict(row)
        if key_field and key is not None and not row.get(key_field):
            # Legacy aliases take precedence over an outer table key.
            alias = row.get(key_field[:-2] + "ID") if key_field.endswith("Id") else None
            row[key_field] = alias or key
        rows.append(row)
    return tuple(rows)


def _row_update_time(row: Mapping[str, Any]) -> int:
    """Use numeric BaseBo millisecond/second timestamps when supplied."""
    seconds = _safe_int(row.get("updateTimeSec"))
    if seconds:
        return seconds * 1000
    return _safe_int(row.get("updateTime")) or 0


def _merge_properties(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_properties(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _readtable_properties(
    device: Mapping[str, Any], status: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Join inline and DevicePropertyStatus values on (deviceId, property).

    Untimed/equal-time values follow arrival order; independent rows follow inline
    properties. Deleted properties suppress older values in this snapshot. This
    is a snapshot policy, not an incremental cache or an inferred control model.
    """
    properties: dict[str, Any] = {}
    times: dict[str, int] = {}
    for owner in (device, status):
        raw = owner.get("properties")
        if isinstance(raw, Mapping):
            timestamp = _row_update_time(owner)
            for key, value in raw.items():
                if timestamp and timestamp < times.get(key, 0):
                    continue
                properties = _merge_properties(properties, {key: value})
                times[key] = timestamp
    for row in rows:
        key = row.get("property")
        if not isinstance(key, str) or not key.strip():
            continue
        deleted = row.get("delFlag") in (1, "1")
        if not deleted and "value" not in row:
            continue
        timestamp = _row_update_time(row)
        if timestamp and timestamp < times.get(key, 0):
            continue
        times[key] = timestamp
        if deleted:
            properties.pop(key, None)
            continue
        value = row["value"]
        if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                pass
        properties = _merge_properties(properties, {key: value})
    return properties


def parse_readtable_devices(
    payload: Mapping[str, Any],
) -> tuple[OrviboDevice, ...]:
    """解析 /v2/cmd/app/readtable 响应，返回归一化的设备列表。

    支持设备、状态、房间表联合查询；cameraInfo[] 只按设备 uid 补充 SDK 类型。
    """
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return ()

    camera_types = parse_camera_types(data.get("cameraInfo"))

    # Keyed and array tables share the same joins and deletion rules.
    room_names: dict[str, str] = {}
    for row in _table_rows(data.get("room"), "roomId"):
        if row.get("delFlag") not in (1, "1"):
            room_id = _first_text(row, ("roomId", "roomID"))
            if room_id:
                room_names[room_id] = _first_text(row, ("roomName",))

    statuses: dict[str, dict[str, Any]] = {}
    for row in _table_rows(data.get("deviceStatus"), "deviceId"):
        device_id = _first_text(row, ("deviceId", "deviceID"))
        if not device_id:
            continue
        previous = statuses.get(device_id, {})
        timestamp = _row_update_time(row)
        if not timestamp or timestamp >= _row_update_time(previous):
            statuses[device_id] = row

    property_rows: dict[str, list[dict[str, Any]]] = {}
    for row in _table_rows(data.get("devicePropertyStatus")):
        device_id = _first_text(row, ("deviceId", "deviceID"))
        if device_id:
            property_rows.setdefault(device_id, []).append(row)

    devices: dict[str, OrviboDevice] = {}
    for item in _table_rows(data.get("device"), "deviceId"):
        if item.get("delFlag") in (1, "1"):
            continue
        device_id = _first_text(
            item, ("deviceId", "deviceID", "deviceUid", "deviceUUID", "uid")
        )
        if not device_id:
            continue
        embedded = item.get("status")
        status = dict(embedded) if isinstance(embedded, Mapping) else {}
        table_status = statuses.get(device_id)
        if table_status is not None:
            if table_status.get("delFlag") in (1, "1"):
                status = {}
            else:
                status.update(table_status)
        if status.get("delFlag") in (1, "1"):
            status = {}
        room_id = _first_text(item, ("roomId", "roomID"))
        online = _parse_online(status.get("online", item.get("online")))
        values = tuple(_safe_int(status.get(key, item.get(key)))
                       for key in ("value1", "value2", "value3", "value4"))
        properties = _readtable_properties(item, status, property_rows.get(device_id, ()))
        if "properties" in status:
            status["properties"] = copy.deepcopy(properties)

        # 提取 ui.model
        ui = item.get("ui", {})
        ui_model = ui.get("model", "") if isinstance(ui, dict) else ""

        # 提取 classId（可能在 properties.Descriptor 里）
        class_id: int | None = _safe_int(item.get("classId"))
        if class_id is None:
            if isinstance(properties, dict):
                descriptor = properties.get("Descriptor", {})
                if isinstance(descriptor, dict):
                    class_id = _safe_int(descriptor.get("classId"))

        devices[device_id] = OrviboDevice(
            uid=device_id,
            name=_first_text(
                item,
                ("deviceName", "devName", "name", "nickName", "nickname"),
            ),
            model=_first_text(
                item,
                (
                    "model",
                    "modelName",
                    "modelId",
                    "modelID",
                    "productName",
                    "productId",
                    "productID",
                ),
            ),
            device_type=_first_text(
                item,
                ("deviceType", "devType", "type", "category", "deviceCategory"),
            ),
            sub_device_type=_first_text(
                item,
                ("subDeviceType", "subDevType"),
            ),
            room=_parse_room(item, room_names),
            parent_uid=_first_text(
                item,
                ("parentUid", "parentId", "parentID", "gatewayUid", "hubUid"),
            ),
            online=online,
            cloud_uid=_first_text(item, ("uid",)),
            camera_type=camera_types.get(_first_text(item, ("uid",))),
            value1=values[0],
            value2=values[1],
            value3=values[2],
            value4=values[3],
            class_id=class_id,
            ui_model=ui_model,
            ext_addr=_first_text(item, ("extAddr", "extAddress")),
            status_id=_first_text(item, ("statusId", "statusID")),
            app_device_id=_first_text(item, ("appDeviceId", "appDeviceID")),
            gateway_id=_first_text(item, ("gatewayId", "gatewayID")),
            class_name=_first_text(item, ("className",)),
            device_flag=_safe_int(item.get("deviceFlag")),
            is_rf_curtain=_parse_bool(item.get("isRFCurtain", item.get("is_rf_curtain"))),
            combined_code=_safe_int(item.get("combinedCode", item.get("combineCode"))),
            endpoint=_safe_int(item.get("endpoint")) or 0,
            properties=properties,
            room_id=room_id,
            status=copy.deepcopy(status),
        )

    return tuple(sorted(devices.values(), key=lambda d: d.uid))


def extract_devices(payloads: Any) -> tuple[OrviboDevice, ...]:
    """从二进制协议（cmd=147/230）递归提取设备列表。

    处理多种 payload 结构：
    - {cmd: 147, tableNameList: [{tableName: "device", dataList: [...]}]}
    - {cmd: 230, deviceList: [...]}
    - 嵌套根数据
    """
    devices: dict[str, OrviboDevice] = {}
    room_names: dict[str, str] = {}
    camera_types: dict[str, int | None] = {}

    def collect_camera_types(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                collect_camera_types(child)
        elif isinstance(value, Mapping):
            if str(value.get("tableName", "")).strip().lower().replace("_", "") == "camerainfo":
                camera_types.update(parse_camera_types(value.get("dataList")))
            for key, child in value.items():
                if str(key).lower().replace("_", "") == "camerainfo":
                    camera_types.update(parse_camera_types(child))
                elif isinstance(child, (Mapping, list)):
                    collect_camera_types(child)

    collect_camera_types(payloads)
    id_keys = (
        "deviceId", "deviceID", "deviceUid", "deviceUUID",
        "deviceUuid", "uuid", "uid",
    )
    device_markers = (
        "uid", "extAddr", "deviceName", "devName",
        "deviceType", "devType", "model", "modelName",
        "productId", "productID",
    )

    def collect_room_names(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                collect_room_names(child)
            return
        if not isinstance(value, Mapping):
            return
        room_id = _first_text(value, ("roomId", "roomID"))
        room_name = _first_text(value, ("roomName",))
        if room_id and room_name:
            room_names[room_id] = room_name
        for child in value.values():
            if isinstance(child, (Mapping, list)):
                collect_room_names(child)

    collect_room_names(payloads)

    def visit(value: Any, table_name: str | None = None) -> None:
        if isinstance(value, list):
            for child in value:
                visit(child, table_name)
            return
        if not isinstance(value, Mapping):
            return

        raw_table_name = value.get("tableName")
        if isinstance(raw_table_name, str) and raw_table_name.strip():
            table_name = raw_table_name.strip().lower().replace("_", "")
        is_device_table = table_name in (
            None, "device", "devices", "devicelist",
            "privacydevice", "privacydevices",
        )

        uid = _first_text(value, id_keys)
        is_device_row = any(
            value.get(key) not in (None, "") for key in device_markers
        )
        if len(uid) >= 6 and is_device_row and is_device_table:
            room_id = _first_text(value, ("roomId", "roomID"))
            candidate = OrviboDevice(
                uid=uid,
                name=_first_text(
                    value,
                    ("deviceName", "devName", "name", "nickName", "nickname"),
                ),
                model=_first_text(
                    value,
                    (
                        "model", "modelName", "modelId", "modelID",
                        "productName", "productId", "productID",
                    ),
                ),
                device_type=_first_text(
                    value,
                    ("deviceType", "devType", "type", "category", "deviceCategory"),
                ),
                sub_device_type=_first_text(
                    value, ("subDeviceType", "subDevType"),
                ),
                room=_parse_room(value, room_names),
                parent_uid=_first_text(
                    value,
                    ("parentUid", "parentId", "parentID", "gatewayUid", "hubUid"),
                ),
                online=_parse_online(
                    next(
                        (
                            value[key]
                            for key in ("online", "isOnline", "connected")
                            if key in value
                        ),
                        None,
                    )
                ),
                cloud_uid=_first_text(value, ("uid",)),
                value1=_safe_int(value.get("value1")),
                value2=_safe_int(value.get("value2")),
                value3=_safe_int(value.get("value3")),
                value4=_safe_int(value.get("value4")),
                class_id=_safe_int(value.get("classId")),
                ui_model=(
                    str(value.get("ui", {}).get("model", ""))
                    if isinstance(value.get("ui"), Mapping)
                    else ""
                ),
                ext_addr=_first_text(value, ("extAddr", "extAddress")),
                status_id=_first_text(value, ("statusId", "statusID")),
                app_device_id=_first_text(value, ("appDeviceId", "appDeviceID")),
                gateway_id=_first_text(value, ("gatewayId", "gatewayID")),
                class_name=_first_text(value, ("className",)),
                device_flag=_safe_int(value.get("deviceFlag")),
                is_rf_curtain=_parse_bool(value.get("isRFCurtain", value.get("is_rf_curtain"))),
                combined_code=_safe_int(value.get("combinedCode", value.get("combineCode"))),
                endpoint=_safe_int(value.get("endpoint")) or 0,
                properties=(
                    dict(value.get("properties", {}))
                    if isinstance(value.get("properties"), Mapping)
                    else {}
                ),
            )
            previous = devices.get(uid)
            if previous is None:
                devices[uid] = candidate
            else:
                devices[uid] = OrviboDevice(
                    uid=uid,
                    name=candidate.name or previous.name,
                    model=candidate.model or previous.model,
                    device_type=candidate.device_type or previous.device_type,
                    sub_device_type=candidate.sub_device_type or previous.sub_device_type,
                    room=candidate.room or previous.room,
                    parent_uid=candidate.parent_uid or previous.parent_uid,
                    online=(
                        candidate.online
                        if candidate.online is not None
                        else previous.online
                    ),
                    cloud_uid=candidate.cloud_uid or previous.cloud_uid,
                    value1=candidate.value1 if candidate.value1 is not None else previous.value1,
                    value2=candidate.value2 if candidate.value2 is not None else previous.value2,
                    value3=candidate.value3 if candidate.value3 is not None else previous.value3,
                    value4=candidate.value4 if candidate.value4 is not None else previous.value4,
                    class_id=candidate.class_id or previous.class_id,
                    ui_model=candidate.ui_model or previous.ui_model,
                    ext_addr=candidate.ext_addr or previous.ext_addr,
                    status_id=candidate.status_id or previous.status_id,
                    app_device_id=candidate.app_device_id or previous.app_device_id,
                    gateway_id=candidate.gateway_id or previous.gateway_id,
                    class_name=candidate.class_name or previous.class_name,
                    device_flag=candidate.device_flag if candidate.device_flag is not None else previous.device_flag,
                    is_rf_curtain=candidate.is_rf_curtain or previous.is_rf_curtain,
                    combined_code=candidate.combined_code if candidate.combined_code is not None else previous.combined_code,
                    endpoint=candidate.endpoint or previous.endpoint,
                    properties=candidate.properties or previous.properties,
                )

        for key, child in value.items():
            if isinstance(child, (Mapping, list)):
                child_table = (
                    "camerainfo"
                    if str(key).lower().replace("_", "") == "camerainfo"
                    else table_name
                )
                visit(child, child_table)

    visit(payloads)
    return tuple(
        replace(device, camera_type=camera_types.get(device.cloud_uid))
        for device in sorted(devices.values(), key=lambda d: d.uid)
    )


# --- 兼容函数：将 OrviboDevice 转换为现有 https_client 的 dict 格式 ---

def device_to_dict(device: OrviboDevice) -> dict[str, Any]:
    """将 OrviboDevice 转为 https_client.parse_device_status_list 兼容的 dict。

    这样 coordinator 和数据平台层无需改动。
    """
    _initial_state = _infer_initial_state(device)
    return {
        "device_id": device.uid,
        "device_name": device.name,
        "device_type": _infer_ha_device_type(device),
        "device_type_raw": _safe_int(device.device_type),
        "camera_type": device.camera_type,
        "sub_device_type": _safe_int(device.sub_device_type),
        "class_id": device.class_id,
        "uid": device.cloud_uid,
        "status_id": device.status_id,
        "app_device_id": device.app_device_id,
        "gateway_id": device.gateway_id,
        "ext_addr": device.ext_addr,
        "model": device.model,
        "ui_model": device.ui_model,
        "room_id": device.room_id,
        "room_name": device.room,
        "online": device.online or False,
        "state": _initial_state,
        "position": device.value1 if device.value1 is not None else 0,
        "brightness": device.value2,
        "color_temp": device.value3,
        "fan_speed": "停",
        "temperature": None,
        "properties": dict(device.properties),
        "endpoint": device.endpoint,
        "class_name": device.class_name,
        "device_flag": device.device_flag,
        "is_rf_curtain": device.is_rf_curtain,
        "combined_code": device.combined_code,
        "status": copy.deepcopy(dict(device.status)),
        "value1": device.value1,
        "value2": device.value2,
        "value3": device.value3,
        "value4": device.value4,
    }


def _infer_ha_device_type(device: OrviboDevice) -> str:
    """根据 device_type 推断 HA 平台类型（兼容 const.DEVICE_TYPE_MAP 逻辑）。

    注：这是一个常量映射的内联版，与 const.DEVICE_TYPE_MAP 保持一致。
    需要同步更新 const.py 的 DEVICE_TYPE_MAP 时也更新这里。
    """
    _DEVICE_TYPE_MAP_INLINE = {
        1: "light", 34: "cover", 35: "cover", 36: "climate", 38: "light",
        43: "switch",  # COCO智能插线板
        14: "sensor", 162: "sensor",  # 摄像机/NVR：仅 SDK 诊断
        46: "sensor", 52: "clothes_horse",
        25: "sensor", 26: "sensor", 27: "sensor", 56: "sensor",
        54: "sensor", 501: "light", 502: "light",
        503: "light", 516: "fan", 522: "sensor", 116: "light",
        506: "cover",
        22: "sensor", 23: "sensor",
        81: "climate",
    }
    _CLASS_ID_MAP_INLINE = {
        426: "light", 429: "light", 436: "light", 1114: "fan",
    }

    dt = _safe_int(device.device_type)
    sub = _safe_int(device.sub_device_type)
    if dt == 0:
        return "light"
    if dt == 102:
        return "light"
    if dt == 112:
        return (
            "climate"
            if sub == -2 and (
                device.ui_model == "orb_floorheat"
                or device.model == "2ac836760da10748856a7e4eafb91efa"
            )
            else "unknown"
        )
    if dt == 10086:
        # 虚拟灯组尚未完成控制协议真机验证，只保留登记信息。
        return "unknown"
    if dt == 300:
        return "climate" if sub == 481 else "sensor" if sub == 491 else "unknown"
    if dt == 506:
        return "cover" if sub == 408 else "unknown"
    if dt == 501:
        return "light" if sub in (426, 429) else "unknown"
    if dt == 502:
        return "light" if sub == 431 else "unknown"
    if dt == 503:
        return "light" if sub in (436, 461) else "unknown"
    if dt == 522:
        return "sensor" if sub == 463 else "unknown"
    if dt and dt in _DEVICE_TYPE_MAP_INLINE:
        return _DEVICE_TYPE_MAP_INLINE[dt]
    if device.class_id and device.class_id in _CLASS_ID_MAP_INLINE:
        return _CLASS_ID_MAP_INLINE[device.class_id]
    return "unknown"


def _infer_initial_state(device: OrviboDevice) -> bool:
    """根据 device_type 和 value1 推断初始开关状态。"""
    dt = _safe_int(device.device_type)
    v1 = device.value1
    if v1 is None:
        return False
    if dt in (1, 102, 38, 503, 0, 43):  # 43=COCO插线板 active-low
        return int(v1) == 0  # active-low
    if dt in (135, 136, 137, 143, 518):
        return int(v1) == 1  # active-high
    if dt == 112:
        return bool(int(v1) & 0x08)
    if dt in (34, 52):
        return int(v1) > 0  # 窗帘位置 >0 为开
    return False


def parse_readtable_to_device_dicts(
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """解析 readtable 返回，直接生成兼容 https_client 的 dict 列表。

    这是 https_client.parse_device_status_list() 的纯函数替代。
    """
    devices = parse_readtable_devices(payload)
    return [device_to_dict(d) for d in devices]


def parse_families_to_dicts(
    payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    """解析家庭列表，返回兼容 https_client.family_list 的 dict 列表。"""
    families = parse_families(payload)
    return [
        {"familyId": f.family_id, "familyName": f.name} for f in families
    ]


def parse_readtable_rooms(
    payload: Mapping[str, Any],
) -> dict[str, str]:
    """从 readtable 响应中提取 roomId → roomName 映射。"""
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return {}
    raw_rooms = data.get("room", [])
    rooms: dict[str, str] = {}
    if isinstance(raw_rooms, list):
        for item in raw_rooms:
            if not isinstance(item, Mapping) or item.get("delFlag") in (1, "1"):
                continue
            room_id = _first_text(item, ("roomId", "roomID"))
            room_name = _first_text(item, ("roomName",))
            if room_id and room_name:
                rooms[room_id] = room_name
    return rooms
