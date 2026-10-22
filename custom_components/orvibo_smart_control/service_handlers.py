"""Home Assistant service registration for lock, media, and history features."""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from .const import DOMAIN
from .device_types import DeviceCategory, classify_device
from .video_archive import normalize_event_object_key
from .meari_session import async_inspect_video_session
from .meari_probe import async_probe_video_stream, async_cancel_video_probes
from .meari_hdd import playback_entity

_LOGGER = logging.getLogger(__name__)

SERVICE_REFRESH = "refresh_devices"
SERVICE_INSPECT_VIDEO_SESSION = "inspect_video_session"
SERVICE_INSPECT_VIDEO_DEVICES = "inspect_video_devices"
SERVICE_INSPECT_VIDEO_CHANNELS = "inspect_video_channels"
SERVICE_PROBE_VIDEO_STREAM = "probe_video_stream"
SERVICE_SET_LOCK_USER_NAME = "set_lock_user_name"
SERVICE_FETCH_VIDEO = "fetch_video"
SERVICE_LIST_EVENTS = "list_events"
SERVICE_LIST_OPENINGS = "list_openings"
SERVICE_CLEANUP_HISTORY = "cleanup_history"
SERVICE_GRANT_TEMP_PASSWORD = "grant_temp_password"
SERVICE_REVOKE_TEMP_PASSWORD = "revoke_temp_password"
SERVICE_LIST_TEMP_PASSWORDS = "list_temp_passwords"


def _targets(hass: HomeAssistant, entry_id: object) -> list[Any]:
    coordinators = hass.data.get(DOMAIN, {})
    if entry_id:
        coordinator = coordinators.get(str(entry_id))
        return [coordinator] if coordinator is not None else []
    return list(coordinators.values())


def _bounded_int(
    value: object,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _is_lock(coordinator: Any, device_id: str) -> bool:
    device = coordinator.devices.get(device_id)
    return device is not None and classify_device(device) == DeviceCategory.DOOR_LOCK


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    """Do not expose Home Assistant filesystem paths in service responses."""
    return {
        key: event[key]
        for key in ("device_id", "kind", "time", "type", "media_id")
        if key in event
    }


def _public_authorizations(result: dict[str, Any]) -> dict[str, Any]:
    """Defense in depth: never expose stored passwords from list services."""
    public: dict[str, Any] = {}
    for device_id, records in result.items():
        if not isinstance(records, list):
            continue
        public[device_id] = [
            {key: value for key, value in record.items() if key != "password"}
            for record in records
            if isinstance(record, dict)
        ]
    return public


async def async_register_services(hass: HomeAssistant) -> None:
    """Register integration services once during domain setup."""

    async def handle_inspect_video_session(
        call: ServiceCall, *, inspect_devices: bool = False, inspect_channels: bool = False,
    ) -> dict[str, Any]:
        targets = _targets(hass, call.data.get("entry_id"))
        if len(targets) != 1:
            return {"stage": "select_one_entry", "video_playback_implemented": True,
                    "real_device_video_verified": False}
        country = str(call.data.get("country_code") or "CN").upper()
        phone = str(call.data.get("phone_code") or "86")
        language = str(call.data.get("language") or "zh")
        if (
            not re.fullmatch(r"[A-Z]{2}", country)
            or not re.fullmatch(r"[0-9]{1,4}", phone)
            or language not in ("zh", "en")
        ):
            return {"stage": "invalid_region", "video_playback_implemented": True,
                    "real_device_video_verified": False}
        kwargs: dict[str, Any] = {}
        if inspect_devices or inspect_channels:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            kwargs["http_session"] = async_get_clientsession(hass)
        if inspect_channels:
            kwargs["include_channels"] = True
        return await async_inspect_video_session(
            targets[0], country_code=country, phone_code=phone, language=language,
            **kwargs,
        )

    async def handle_inspect_video_devices(call: ServiceCall) -> dict[str, Any]:
        return await handle_inspect_video_session(call, inspect_devices=True)

    async def handle_inspect_video_channels(call: ServiceCall) -> dict[str, Any]:
        return await handle_inspect_video_session(call, inspect_channels=True)

    async def handle_probe_video_stream(call: ServiceCall) -> dict[str, Any]:
        base = {"video_playback_implemented": True, "real_device_video_verified": False}
        targets = _targets(hass, call.data.get("entry_id"))
        if len(targets) != 1:
            return {**base, "probe_stage": "select_one_entry"}
        values = {}
        for field, minimum, maximum, default in (
            ("device_index", 1, 512, None), ("channel_id", 0, 128, None),
            ("timeout_seconds", 10, 60, 35),
        ):
            value = call.data.get(field, default)
            if type(value) is not int or not minimum <= value <= maximum:
                return {**base, "probe_stage": "invalid_probe_parameters"}
            values[field] = value
        country = str(call.data.get("country_code") or "CN").upper()
        phone = str(call.data.get("phone_code") or "86")
        language = str(call.data.get("language") or "zh")
        if (not re.fullmatch(r"[A-Z]{2}", country)
                or not re.fullmatch(r"[0-9]{1,4}", phone)
                or language not in ("zh", "en")):
            return {**base, "probe_stage": "invalid_region"}
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        return await async_probe_video_stream(
            targets[0], http_session=async_get_clientsession(hass),
            country_code=country, phone_code=phone, language=language, **values,
        )

    async def handle_stop_video_probes(_event: Any) -> None:
        await async_cancel_video_probes()

    hass.bus.async_listen_once("homeassistant_stop", handle_stop_video_probes)

    async def handle_search_hdd(call: ServiceCall) -> dict[str, Any]:
        try:
            entity = playback_entity(hass, call.data.get("entity_id"))
            result = await entity.async_search_recordings(
                call.data.get("operation", "day"), call.data.get("date"),
            )
            return {**result, "entity_id": entity.entity_id}
        except (ValueError, TypeError, OSError, RuntimeError, TimeoutError):
            return {"stage": "hdd_query_failed",
                    "error": "查询失败，请选择硬盘或存储卡回放实体并检查日期；连接失败时稍后重试"}

    async def handle_start_hdd(call: ServiceCall) -> dict[str, Any]:
        try:
            entity = playback_entity(hass, call.data.get("entity_id"))
            await entity.async_select_recording(call.data.get("date"))
            return {"stage": "playback_selected", "entity_id": entity.entity_id,
                    "date": call.data["date"], "device_calendar_time": True}
        except (ValueError, TypeError, OSError, RuntimeError, TimeoutError):
            return {"stage": "hdd_playback_failed",
                    "error": "回放未启动，请选择硬盘或存储卡回放实体和有效的录像时间"}

    async def handle_stop_hdd(call: ServiceCall) -> dict[str, Any]:
        try:
            entity = playback_entity(hass, call.data.get("entity_id"))
            await entity.async_stop_recording()
            return {"stage": "playback_stopped", "entity_id": entity.entity_id}
        except (ValueError, TypeError, OSError, RuntimeError, TimeoutError):
            return {"stage": "hdd_playback_failed", "error": "未找到可用的硬盘或存储卡回放实体"}

    async def handle_refresh(call: ServiceCall) -> None:
        entry_id = str(call.data.get("entry_id") or "")
        targets = _targets(hass, entry_id)
        if not entry_id or not targets:
            _LOGGER.error("refresh_devices 需要有效的 entry_id")
            return
        await targets[0].async_request_refresh()

    async def handle_set_lock_user_name(call: ServiceCall) -> None:
        device_id = str(call.data.get("device_id") or "").strip()
        raw_user_id = call.data.get("user_id")
        user_id = str(raw_user_id).strip() if raw_user_id is not None else ""
        name = str(call.data.get("name") or "").strip()[:64]
        if not device_id or not user_id:
            _LOGGER.error("set_lock_user_name 需要 device_id 和 user_id")
            return
        updated = False
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if _is_lock(coordinator, device_id):
                updated |= coordinator.set_lock_user_name(device_id, user_id[:64], name)
        if not updated:
            _LOGGER.warning("未找到对应门锁或配置项")

    async def handle_fetch_video(call: ServiceCall) -> dict[str, Any]:
        device_id = str(call.data.get("device_id") or "").strip()
        object_key = normalize_event_object_key(
            str(call.data.get("object_key") or "")
        )
        if not device_id or object_key is None:
            return {"error": "需要有效的门锁 device_id 和事件录像对象键"}
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if not _is_lock(coordinator, device_id):
                continue
            result = await coordinator.async_fetch_video(device_id, object_key)
            if result and "error" not in result:
                return {
                    "ok": True,
                    "media_id": result.get("media_id", ""),
                }
        return {"error": "未找到门锁配置项或录像拉取失败"}

    async def handle_list_events(call: ServiceCall) -> dict[str, Any]:
        device_id = str(call.data.get("device_id") or "").strip()
        limit = _bounded_int(call.data.get("limit"), 100, 1, 500)
        media_type = call.data.get("media_type")
        if media_type not in (None, "image", "video"):
            return {"error": "media_type 必须为 image 或 video"}
        events: list[dict[str, Any]] = []
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if device_id and not _is_lock(coordinator, device_id):
                continue
            if media_type is None:
                events.extend(await coordinator.async_list_events(device_id, limit))
            else:
                events.extend(await coordinator.async_list_events(device_id, limit, media_type))
            if len(events) >= limit:
                break
        return {"events": [_public_event(item) for item in events[:limit]]}

    async def handle_cleanup_history(call: ServiceCall) -> dict[str, int]:
        device_id = str(call.data.get("device_id") or "").strip()
        keep_days = _bounded_int(call.data.get("keep_days"), 7, 0, 3650)
        raw_max_entries = call.data.get("max_entries")
        max_entries = (
            _bounded_int(raw_max_entries, 500, 1, 10000)
            if raw_max_entries not in (None, "")
            else None
        )
        total = 0
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if device_id and not _is_lock(coordinator, device_id):
                continue
            total += await coordinator.async_cleanup_history(
                keep_days=keep_days,
                device_id=device_id,
                max_entries=max_entries,
            )
        return {"removed": total}

    async def handle_list_openings(call: ServiceCall) -> dict[str, Any]:
        device_id = str(call.data.get("device_id") or "").strip()
        if not device_id:
            return {"error": "需要门锁 device_id"}
        limit = _bounded_int(call.data.get("limit"), 100, 1, 500)
        records: dict[str, dict[str, Any]] = {}
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if not _is_lock(coordinator, device_id):
                continue
            for record in await coordinator.async_list_openings(device_id, limit):
                public = {
                    key: record[key]
                    for key in ("id", "device_id", "time", "open_direction")
                    if key in record
                }
                if record.get("open_direction") == "outside":
                    public.update({
                        key: record[key]
                        for key in ("unlock_method", "unlock_user_id", "unlock_user_name")
                        if key in record
                    })
                records[str(record["id"])] = public
        openings = sorted(records.values(), key=lambda item: item["time"], reverse=True)
        return {"openings": openings[:limit]}

    async def handle_grant_temp_password(call: ServiceCall) -> dict[str, Any]:
        device_id = str(call.data.get("device_id") or "").strip()
        auth_type = _bounded_int(call.data.get("type"), 2, 1, 2)
        minutes = _bounded_int(call.data.get("minutes"), 1440, 1, 525600)
        number = _bounded_int(call.data.get("number"), 1, 0, 100)
        name = str(call.data.get("name") or "").strip()[:64]
        phone = str(call.data.get("phone") or "").strip()[:32]
        if phone and not re.fullmatch(r"\+?[0-9]{6,20}", phone):
            return {"error": "phone 必须为 6 到 20 位电话号码"}
        start_time = call.data.get("start_time")
        end_time = call.data.get("end_time")
        try:
            start = int(start_time) if start_time not in (None, "") else None
            end = int(end_time) if end_time not in (None, "") else None
        except (TypeError, ValueError):
            return {"error": "start_time/end_time 必须为 Unix 秒时间戳"}
        if start is not None and end is not None and end <= start:
            return {"error": "end_time 必须晚于 start_time"}
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if not device_id:
                device_id = next(
                    (
                        did
                        for did, device in coordinator.devices.items()
                        if classify_device(device) == DeviceCategory.DOOR_LOCK
                    ),
                    "",
                )
            if not device_id or not _is_lock(coordinator, device_id):
                continue
            return await coordinator.async_grant_temp_password(
                device_id=device_id,
                auth_type=auth_type,
                minutes=minutes,
                number=number,
                name=name,
                phone=phone,
                start_time=start,
                end_time=end,
            )
        return {"error": "未找到可用的配置项或门锁设备"}

    async def handle_revoke_temp_password(call: ServiceCall) -> dict[str, Any]:
        device_id = str(call.data.get("device_id") or "").strip()
        authorized_id = _bounded_int(call.data.get("authorized_id"), 0, 0, 2**31 - 1)
        if not device_id or authorized_id <= 0:
            return {"error": "需要门锁 device_id 和有效的 authorized_id"}
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if _is_lock(coordinator, device_id):
                return await coordinator.async_revoke_temp_password(
                    device_id, authorized_id
                )
        return {"error": "未找到门锁设备或配置项"}

    async def handle_list_temp_passwords(call: ServiceCall) -> dict[str, Any]:
        device_id = str(call.data.get("device_id") or "").strip()
        result: dict[str, Any] = {}
        for coordinator in _targets(hass, call.data.get("entry_id")):
            if device_id and not _is_lock(coordinator, device_id):
                continue
            result.update(await coordinator.async_list_temp_passwords(device_id))
        return _public_authorizations(result)

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, handle_refresh)
    hass.services.async_register(
        DOMAIN, SERVICE_SET_LOCK_USER_NAME, handle_set_lock_user_name
    )
    for service, handler in (
        ("search_hdd_recordings", handle_search_hdd),
        ("start_hdd_playback", handle_start_hdd),
        ("stop_hdd_playback", handle_stop_hdd),
        (SERVICE_INSPECT_VIDEO_SESSION, handle_inspect_video_session),
        (SERVICE_INSPECT_VIDEO_DEVICES, handle_inspect_video_devices),
        (SERVICE_INSPECT_VIDEO_CHANNELS, handle_inspect_video_channels),
        (SERVICE_PROBE_VIDEO_STREAM, handle_probe_video_stream),
        (SERVICE_FETCH_VIDEO, handle_fetch_video),
        (SERVICE_LIST_EVENTS, handle_list_events),
        (SERVICE_LIST_OPENINGS, handle_list_openings),
        (SERVICE_CLEANUP_HISTORY, handle_cleanup_history),
        (SERVICE_GRANT_TEMP_PASSWORD, handle_grant_temp_password),
        (SERVICE_REVOKE_TEMP_PASSWORD, handle_revoke_temp_password),
        (SERVICE_LIST_TEMP_PASSWORDS, handle_list_temp_passwords),
    ):
        hass.services.async_register(
            DOMAIN,
            service,
            handler,
            supports_response=SupportsResponse.OPTIONAL,
        )
