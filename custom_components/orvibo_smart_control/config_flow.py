import logging
import re
import shutil
from pathlib import Path
from typing import Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .capabilities import TransportMode
from .cloud import CHINA_CLOUD, CloudEndpoint, cloud_for_region
from .const import (
    CONF_AVAILABILITY_NOTIFICATIONS,
    CONF_CLOUD_REGION,
    CONF_FAMILY_ID,
    CONF_LAN_PASSWORD,
    CONF_LAN_PASSWORD_HASH,
    CONF_LAN_USERNAME,
    CONF_LOCK_USER_NAMES,
    CONF_NOTIFY_OFFLINE,
    CONF_NOTIFY_ONLINE,
    CONF_NOTIFY_SERVICE,
    CONF_PASSWORD,
    CONF_PASSWORD_HASH,
    CONF_POLL_INTERVAL_MINUTES,
    CONF_TRANSPORT_MODE,
    CONF_UPDATE_CHECK_ENABLED,
    CONF_UPDATE_CHECK_INTERVAL_HOURS,
    CONF_USE_INDEPENDENT_LAN_CREDENTIALS,
    CONF_USERNAME,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DEFAULT_UPDATE_CHECK_INTERVAL_HOURS,
    DOMAIN,
    MAX_POLL_INTERVAL_MINUTES,
    MAX_UPDATE_CHECK_INTERVAL_HOURS,
    MIN_POLL_INTERVAL_MINUTES,
    MIN_UPDATE_CHECK_INTERVAL_HOURS,
)
from .device_selection import (
    device_selection_groups,
    merge_grouped_selection,
)
from .device_types import (
    DeviceCategory,
    classify_device,
    get_device_profile,
    is_hidden_category,
)
from .https_client import HttpsClient
from .lock_status import format_lock_user_names, parse_lock_user_names
from .protocol import password_hash
from .selection import CONF_SELECTED_DEVICE_IDS, selected_device_ids

_LOGGER = logging.getLogger(__name__)

CONF_CONFIRM = "confirm"
CONF_CLEAR_MEDIA = "clear_media"
CONF_RESET_GENERAL_OPTIONS = "reset_general_options"

_GENERAL_OPTION_KEYS = frozenset(
    {
        CONF_TRANSPORT_MODE,
        CONF_USE_INDEPENDENT_LAN_CREDENTIALS,
        CONF_LAN_USERNAME,
        CONF_LAN_PASSWORD_HASH,
        CONF_POLL_INTERVAL_MINUTES,
        CONF_AVAILABILITY_NOTIFICATIONS,
        CONF_NOTIFY_ONLINE,
        CONF_NOTIFY_OFFLINE,
        CONF_NOTIFY_SERVICE,
        CONF_UPDATE_CHECK_ENABLED,
        CONF_UPDATE_CHECK_INTERVAL_HOURS,
    }
)


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _clear_integration_media(media_root: str | Path) -> int:
    """Remove only this integration's media directory and return file count."""

    root = Path(media_root).resolve()
    target = (root / DOMAIN).resolve()
    if target.parent != root or target.name != DOMAIN:
        raise ValueError("unsafe integration media path")
    if not target.is_dir():
        return 0
    removed = sum(1 for path in target.rglob("*") if path.is_file())
    shutil.rmtree(target)
    return removed


def _sync_device_registry_names(hass: HomeAssistant, devices: list[dict]) -> int:
    """Apply current cloud names without replacing user-defined HA names."""

    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    updated = 0
    for device in devices:
        device_id = str(device.get("device_id") or "")
        cloud_name = str(device.get("device_name") or "").strip()
        if not device_id or not cloud_name:
            continue
        entry = registry.async_get_device(identifiers={(DOMAIN, device_id)})
        if entry is None or entry.name == cloud_name:
            continue
        registry.async_update_device(entry.id, name=cloud_name)
        updated += 1
    return updated


def _device_label(device_id: str, name: str, room: str) -> str:
    """设备标签：名称 + 房间。"""
    if room and room != name:
        return f"{name} [{room}]"
    return name or device_id[-8:]


def _device_option_label(device: dict) -> str:
    """Add catalogue identity without implying unsupported control capability."""
    label = _device_label(
        str(device["device_id"]),
        str(device.get("device_name") or ""),
        str(device.get("room_name") or ""),
    )
    profile = get_device_profile(device)
    if profile.category == DeviceCategory.OTHER or (
        profile.registration_only
        and profile.category != DeviceCategory.UNKNOWN
    ):
        return f"{label}（已识别：{profile.info.label}，暂未支持）"
    if profile.category == DeviceCategory.UNKNOWN:
        return f"{label}（未识别，暂未支持）"
    return label


def _device_selection_schema(
    devices: list[dict], selected_ids: set[str],
) -> vol.Schema:
    """Build grouped device selectors for both config-flow device screens.

    Each populated group has an all-checkbox and an expandable multi-select.
    The checkbox defaults to true when every device in that group is selected;
    turning it off exposes the saved individual choices.
    """

    fields: dict[object, object] = {}
    for group in device_selection_groups(devices):
        ids = list(group.device_ids)
        all_selected = bool(ids) and set(ids).issubset(selected_ids)
        options = [
            selector.SelectOptionDict(
                value=str(device["device_id"]),
                label=f"{group.label} · {_device_option_label(device)}",
            )
            for device in group.devices
        ]
        fields[vol.Required(group.all_field, default=all_selected)] = (
            selector.BooleanSelector()
        )
        selected_in_group = [
            device_id for device_id in ids if device_id in selected_ids
        ]
        fields[vol.Optional(
            group.device_field,
            default=[] if all_selected else selected_in_group,
        )] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=options,
                multiple=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
    return vol.Schema(fields)


async def _fetch_devices(
    hass: HomeAssistant,
    username: str,
    password_digest: str,
    family_id: str,
    cloud: CloudEndpoint = CHINA_CLOUD,
) -> list[dict]:
    """拉取设备列表（含房间信息），过滤隐藏类别。"""
    client = None
    try:
        client = HttpsClient(
            username=username,
            password_hash=password_digest,
            session=async_get_clientsession(hass),
            cloud=cloud,
        )
        if family_id:
            client.family_id = family_id
        if not await client.ensure_login():
            return []
        data = await client.fetch_device_status()
        devices = client.parse_device_status_list(data) if data else []
    except Exception as e:
        _LOGGER.debug("获取设备列表失败: %s", e)
        return []
    finally:
        if client:
            await client.close()
    return [
        d for d in devices
        if not is_hidden_category(classify_device(d))
    ]


async def _probe_ssl_credentials(
    hass: HomeAssistant,
    cloud: CloudEndpoint,
    username: str,
    password_digest: str,
    family_id: str,
) -> bool:
    """Validate credentials against the binary SSL login endpoint."""
    from .const import SSL_PORT
    from .ssl_client import SSLClient

    client = SSLClient(
        hass=hass,
        ssl_host=cloud.ssl_host,
        ssl_port=SSL_PORT,
        username=username,
        password_hash=password_digest,
        family_id=family_id,
        on_session_id_obtained=lambda sid: None,
        on_status_update=lambda did, raw: None,
        retry_interval=0,
    )
    try:
        ok = await client.connect_and_login(max_attempts=1, hello_wait=1.0)
        if ok:
            return True
        status = getattr(client, "_login_status", None)
        # Keep the existing behaviour: an explicit non-zero login response is
        # an authentication failure; a network timeout must not lock users out.
        return status is None or status == 0
    finally:
        await client._disconnect()


async def _validate_updated_credentials(
    hass: HomeAssistant,
    username: str,
    password_digest: str,
    family_id: str,
    cloud: CloudEndpoint,
) -> Optional[CloudEndpoint]:
    """Detect the account region and validate a replacement password."""
    client = None
    try:
        client = HttpsClient(
            username=username,
            password_hash=password_digest,
            session=async_get_clientsession(hass),
            cloud=cloud,
        )
        if not await client.async_detect_cloud(family_id or None):
            return None
        detected_cloud = client.cloud
    except Exception:
        _LOGGER.debug("重新登录验证失败", exc_info=True)
        return None
    finally:
        if client:
            await client.close()

    try:
        valid = await _probe_ssl_credentials(
            hass,
            detected_cloud,
            username,
            password_digest,
            family_id,
        )
    except Exception:
        _LOGGER.debug("SSL 重新登录验证失败", exc_info=True)
        return None
    if not valid:
        return None
    return detected_cloud


class OrviboSmartControlConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 3

    def __init__(self) -> None:
        self._devices: list[dict] = []
        self._pending_selected_ids: list[str] = []
        self._cloud = CHINA_CLOUD

    async def async_step_user(
        self, user_input: Optional[dict] = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            if not username or not password:
                errors["base"] = "empty_username_or_password"
            elif not re.match(r'^1[3-9]\d{9}$', username) and not re.match(r'^[^@]+@[^@]+\.[^@]+$', username):
                errors[CONF_USERNAME] = "invalid_username"

            if not errors:
                # 临时 client 用于验证登录并获取家庭列表
                temp_client = None
                try:
                    self._password_hash = password_hash(password)
                    temp_client = HttpsClient(
                        username=username,
                        password_hash=self._password_hash,
                        session=async_get_clientsession(self.hass),
                    )
                    success = await temp_client.async_detect_cloud()

                    if success:
                        # 保存数据到 self，后续步骤使用
                        self._username = username
                        self._family_list = temp_client.family_list
                        self._family_id = temp_client.family_id
                        self._family_name = temp_client.family_name
                        self._cloud = temp_client.cloud

                        # 前置认证校验：拿到第一个家庭 ID 后立即做 SSL 探针，
                        # 密码错误时在凭据表单直接提示，不再展示家庭列表
                        probe_family_id = (
                            str(self._family_list[0]["familyId"])
                            if self._family_list
                            else ""
                        )
                        if not await self._probe_ssl_login(probe_family_id):
                            errors["base"] = "auth_failed"
                        elif len(self._family_list) <= 1:
                            return await self.async_step_devices()
                        else:
                            return await self.async_step_select_family()
                    else:
                        errors["base"] = "auth_failed"
                except Exception as e:
                    _LOGGER.error(f"登录验证失败: {e}")
                    errors["base"] = "auth_failed"
                finally:
                    if temp_client:
                        await temp_client.close()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    async def async_step_select_family(self, user_input: Optional[dict] = None) -> FlowResult:
        """选择家庭步骤"""
        errors: dict[str, str] = {}
        
        if user_input is not None:
            family_id = user_input.get(CONF_FAMILY_ID)
            if family_id:
                self._family_id = family_id
                for f in self._family_list:
                    if f["familyId"] == family_id:
                        self._family_name = f["familyName"]
                        break
                return await self.async_step_devices()

        # 构建家庭选择列表
        family_choices = {
            f["familyId"]: f"{f['familyName']} ({f['familyId'][:8]}...)"
            for f in self._family_list
        }
        
        if len(family_choices) == 1:
            # 只有一个家庭，直接使用
            self._family_id = list(family_choices.keys())[0]
            return await self.async_step_devices()

        return self.async_show_form(
            step_id="select_family",
            data_schema=vol.Schema({
                vol.Required(CONF_FAMILY_ID): vol.In(family_choices),
            }),
            errors=errors,
            description_placeholders={
                "family_count": str(len(family_choices)),
            }
        )

    async def async_step_devices(
        self, user_input: Optional[dict] = None
    ) -> FlowResult:
        """选择要接入 Home Assistant 的设备。"""
        errors: dict[str, str] = {}
        if not self._devices:
            self._devices = await _fetch_devices(
                self.hass,
                self._username,
                self._password_hash,
                self._family_id or "",
                self._cloud,
            )
            if not self._devices:
                errors["base"] = "no_devices"

        if user_input is not None:
            available = {str(d["device_id"]) for d in self._devices}
            self._pending_selected_ids = [
                device_id
                for device_id in merge_grouped_selection(user_input, self._devices)
                if device_id in available
            ]
            if not self._pending_selected_ids:
                errors["base"] = "no_devices_selected"
            else:
                return await self._create_entry()

        default_ids = set(
            self._pending_selected_ids
            if user_input is not None
            else (str(d["device_id"]) for d in self._devices)
        )
        return self.async_show_form(
            step_id="devices",
            data_schema=_device_selection_schema(self._devices, default_ids),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Optional[dict] = None
    ) -> FlowResult:
        """开始重新认证（凭据失效时由 Home Assistant 自动触发）。"""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: Optional[dict] = None
    ) -> FlowResult:
        """输入新密码并更新配置项。"""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="reauth_entry_missing")
        username = str(entry.data.get(CONF_USERNAME, ""))

        if user_input is not None:
            password = str(user_input.get(CONF_PASSWORD) or "")
            if not password:
                errors["base"] = "empty_username_or_password"
            else:
                password_digest = password_hash(password)
                detected_cloud = await _validate_updated_credentials(
                    self.hass,
                    username,
                    password_digest,
                    str(entry.data.get(CONF_FAMILY_ID, "")),
                    cloud_for_region(entry.data.get(CONF_CLOUD_REGION)),
                )
                if detected_cloud is not None:
                    data_updates = {
                        CONF_PASSWORD_HASH: password_digest,
                        CONF_CLOUD_REGION: detected_cloud.region.value,
                    }
                    # HA 2026.6+ delegates reload to the entry update listener;
                    # retain the older helper for the declared HA 2024.1 floor.
                    update_and_abort = getattr(
                        self, "async_update_and_abort", None
                    )
                    if update_and_abort is not None:
                        return update_and_abort(
                            entry,
                            data_updates=data_updates,
                        )
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates=data_updates,
                    )
                errors["base"] = "auth_failed"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"username": username},
        )

    async def _probe_ssl_login(self, family_id: str) -> bool:
        """用 SSL 二进制登录校验密码真实有效。

        REST OAuth 不校验密码（任意密码都返回 token），真正的校验点在
        10002 端口 SSL 登录。仅当服务器明确拒绝（status 非空且非 0）时
        判定为认证失败；网络/超时类失败不阻塞配置流程。
        """
        return await _probe_ssl_credentials(
            self.hass,
            self._cloud,
            self._username,
            self._password_hash,
            family_id,
        )

    async def _create_entry(self) -> FlowResult:
        """创建配置条目"""
        # 找到家庭列表中的用户ID（临时 client 已关闭，使用暂存数据）
        await self.async_set_unique_id(self._username)
        self._abort_if_unique_id_configured()
        
        return self.async_create_entry(
            title=f"{self._username} - {self._family_name}",
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD_HASH: self._password_hash,
                CONF_FAMILY_ID: self._family_id,
                CONF_CLOUD_REGION: self._cloud.region.value,
            },
            options={
                CONF_SELECTED_DEVICE_IDS: self._pending_selected_ids,
            },
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return OrviboSmartControlOptionsFlow(config_entry)


class OrviboSmartControlOptionsFlow(config_entries.OptionsFlow):
    """重新选择接入的设备。"""

    def __init__(self, config_entry):
        # 新版 HA 将 OptionsFlow.config_entry 暴露为只读属性；
        # 用私有字段保存工厂参数，兼容新旧版本。
        self._config_entry = config_entry
        self._devices: list[dict] = []

    async def async_step_init(self, user_input=None):
        """Show all account, transport, device, and maintenance options."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "transport_mode",
                "lan_credentials",
                "devices",
                "reauth",
                "sync_device_names",
                "clear_local_data",
                "polling",
                "availability_notifications",
                "update_check",
                "lock_users",
            ],
        )

    async def async_step_transport_mode(self, user_input=None):
        """Select LAN-only, cloud-only, or combined LAN-first operation."""
        if user_input is not None:
            options = dict(self._config_entry.options)
            options[CONF_TRANSPORT_MODE] = str(
                user_input.get(CONF_TRANSPORT_MODE, TransportMode.AUTO.value)
            )
            return self.async_create_entry(title="", data=options)

        current = self._config_entry.options.get(
            CONF_TRANSPORT_MODE, TransportMode.AUTO.value
        )
        return self.async_show_form(
            step_id="transport_mode",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TRANSPORT_MODE, default=current
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                TransportMode.AUTO.value,
                                TransportMode.LAN_ONLY.value,
                                TransportMode.CLOUD_ONLY.value,
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                            translation_key="transport_mode",
                        )
                    )
                }
            ),
        )

    async def async_step_lan_credentials(self, user_input=None):
        """Configure optional credentials used only for MixPad LAN login."""

        errors: dict[str, str] = {}
        options = dict(self._config_entry.options)
        if user_input is not None:
            enabled = bool(
                user_input.get(CONF_USE_INDEPENDENT_LAN_CREDENTIALS, False)
            )
            username = str(user_input.get(CONF_LAN_USERNAME) or "").strip()
            password = str(user_input.get(CONF_LAN_PASSWORD) or "")
            password_digest = (
                password_hash(password)
                if password
                else str(options.get(CONF_LAN_PASSWORD_HASH) or "")
            )
            if enabled and (not username or not password_digest):
                errors["base"] = "lan_credentials_required"
            else:
                options[CONF_USE_INDEPENDENT_LAN_CREDENTIALS] = enabled
                if enabled:
                    options[CONF_LAN_USERNAME] = username
                    options[CONF_LAN_PASSWORD_HASH] = password_digest
                else:
                    options.pop(CONF_LAN_USERNAME, None)
                    options.pop(CONF_LAN_PASSWORD_HASH, None)
                return self.async_create_entry(title="", data=options)

        current_enabled = bool(
            options.get(CONF_USE_INDEPENDENT_LAN_CREDENTIALS, False)
        )
        current_username = str(
            options.get(CONF_LAN_USERNAME)
            or self._config_entry.data.get(CONF_USERNAME, "")
        )
        return self.async_show_form(
            step_id="lan_credentials",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USE_INDEPENDENT_LAN_CREDENTIALS,
                        default=current_enabled,
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_LAN_USERNAME,
                        default=current_username,
                    ): selector.TextSelector(),
                    vol.Optional(CONF_LAN_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_polling(self, user_input=None):
        """Configure cloud snapshot polling used outside LAN-only mode."""

        options = dict(self._config_entry.options)
        if user_input is not None:
            options[CONF_POLL_INTERVAL_MINUTES] = _bounded_int(
                user_input.get(CONF_POLL_INTERVAL_MINUTES),
                DEFAULT_POLL_INTERVAL_MINUTES,
                MIN_POLL_INTERVAL_MINUTES,
                MAX_POLL_INTERVAL_MINUTES,
            )
            return self.async_create_entry(title="", data=options)

        current = _bounded_int(
            options.get(CONF_POLL_INTERVAL_MINUTES),
            DEFAULT_POLL_INTERVAL_MINUTES,
            MIN_POLL_INTERVAL_MINUTES,
            MAX_POLL_INTERVAL_MINUTES,
        )
        return self.async_show_form(
            step_id="polling",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL_MINUTES,
                        default=current,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_POLL_INTERVAL_MINUTES,
                            max=MAX_POLL_INTERVAL_MINUTES,
                            step=5,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    )
                }
            ),
        )

    async def async_step_availability_notifications(self, user_input=None):
        """Configure device online/offline notifications."""

        errors: dict[str, str] = {}
        options = dict(self._config_entry.options)
        if user_input is not None:
            service = str(user_input.get(CONF_NOTIFY_SERVICE) or "").strip()
            if service and not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", service):
                errors[CONF_NOTIFY_SERVICE] = "invalid_service"
            else:
                options[CONF_AVAILABILITY_NOTIFICATIONS] = bool(
                    user_input.get(CONF_AVAILABILITY_NOTIFICATIONS, False)
                )
                options[CONF_NOTIFY_ONLINE] = bool(
                    user_input.get(CONF_NOTIFY_ONLINE, True)
                )
                options[CONF_NOTIFY_OFFLINE] = bool(
                    user_input.get(CONF_NOTIFY_OFFLINE, True)
                )
                options[CONF_NOTIFY_SERVICE] = service
                return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="availability_notifications",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AVAILABILITY_NOTIFICATIONS,
                        default=bool(
                            options.get(CONF_AVAILABILITY_NOTIFICATIONS, False)
                        ),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_NOTIFY_ONLINE,
                        default=bool(options.get(CONF_NOTIFY_ONLINE, True)),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_NOTIFY_OFFLINE,
                        default=bool(options.get(CONF_NOTIFY_OFFLINE, True)),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        CONF_NOTIFY_SERVICE,
                        default=str(options.get(CONF_NOTIFY_SERVICE) or ""),
                    ): selector.TextSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_update_check(self, user_input=None):
        """Configure the lightweight GitHub release check."""

        options = dict(self._config_entry.options)
        if user_input is not None:
            options[CONF_UPDATE_CHECK_ENABLED] = bool(
                user_input.get(CONF_UPDATE_CHECK_ENABLED, False)
            )
            options[CONF_UPDATE_CHECK_INTERVAL_HOURS] = _bounded_int(
                user_input.get(CONF_UPDATE_CHECK_INTERVAL_HOURS),
                DEFAULT_UPDATE_CHECK_INTERVAL_HOURS,
                MIN_UPDATE_CHECK_INTERVAL_HOURS,
                MAX_UPDATE_CHECK_INTERVAL_HOURS,
            )
            return self.async_create_entry(title="", data=options)

        current = _bounded_int(
            options.get(CONF_UPDATE_CHECK_INTERVAL_HOURS),
            DEFAULT_UPDATE_CHECK_INTERVAL_HOURS,
            MIN_UPDATE_CHECK_INTERVAL_HOURS,
            MAX_UPDATE_CHECK_INTERVAL_HOURS,
        )
        return self.async_show_form(
            step_id="update_check",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_UPDATE_CHECK_ENABLED,
                        default=bool(options.get(CONF_UPDATE_CHECK_ENABLED, False)),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_UPDATE_CHECK_INTERVAL_HOURS,
                        default=current,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_UPDATE_CHECK_INTERVAL_HOURS,
                            max=MAX_UPDATE_CHECK_INTERVAL_HOURS,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="h",
                        )
                    ),
                }
            ),
        )

    async def async_step_sync_device_names(self, user_input=None):
        """Fetch current cloud names and apply them to HA device entries."""

        errors: dict[str, str] = {}
        if user_input is not None and user_input.get(CONF_CONFIRM):
            devices = await _fetch_devices(
                self.hass,
                str(self._config_entry.data.get(CONF_USERNAME, "")),
                str(self._config_entry.data.get(CONF_PASSWORD_HASH, "")),
                str(self._config_entry.data.get(CONF_FAMILY_ID, "")),
                cloud_for_region(
                    self._config_entry.data.get(CONF_CLOUD_REGION)
                ),
            )
            if not devices:
                errors["base"] = "no_devices"
            else:
                updated = _sync_device_registry_names(self.hass, devices)
                coordinator = self.hass.data.get(DOMAIN, {}).get(
                    getattr(self._config_entry, "entry_id", "")
                )
                if coordinator is not None:
                    for cloud_device in devices:
                        device_id = str(cloud_device.get("device_id") or "")
                        current = coordinator.devices.get(device_id)
                        if current is not None:
                            current["device_name"] = cloud_device.get("device_name")
                            current["room_name"] = cloud_device.get("room_name")
                    coordinator.async_set_updated_data(coordinator.device_states)
                return self.async_abort(
                    reason="device_names_synced",
                    description_placeholders={"count": str(updated)},
                )

        return self.async_show_form(
            step_id="sync_device_names",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CONFIRM, default=False):
                        selector.BooleanSelector()
                }
            ),
            errors=errors,
        )

    async def async_step_clear_local_data(self, user_input=None):
        """Clear integration-owned media and reset only general options."""

        if user_input is not None and user_input.get(CONF_CONFIRM):
            removed = 0
            if user_input.get(CONF_CLEAR_MEDIA, True):
                removed = await self.hass.async_add_executor_job(
                    _clear_integration_media,
                    self.hass.config.path("media"),
                )
            if user_input.get(CONF_RESET_GENERAL_OPTIONS, True):
                options = {
                    key: value
                    for key, value in self._config_entry.options.items()
                    if key not in _GENERAL_OPTION_KEYS
                }
                return self.async_create_entry(title="", data=options)
            return self.async_abort(
                reason="local_data_cleared",
                description_placeholders={"count": str(removed)},
            )

        return self.async_show_form(
            step_id="clear_local_data",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CLEAR_MEDIA, default=True):
                        selector.BooleanSelector(),
                    vol.Required(CONF_RESET_GENERAL_OPTIONS, default=True):
                        selector.BooleanSelector(),
                    vol.Required(CONF_CONFIRM, default=False):
                        selector.BooleanSelector(),
                }
            ),
        )

    async def async_step_reauth(self, user_input=None):
        """主动更新密码，同时保留当前配置项及全部实体设置。"""
        errors: dict[str, str] = {}
        username = str(self._config_entry.data.get(CONF_USERNAME, ""))

        if user_input is not None:
            password = str(user_input.get(CONF_PASSWORD) or "")
            if not password:
                errors["base"] = "empty_username_or_password"
            else:
                password_digest = password_hash(password)
                detected_cloud = await _validate_updated_credentials(
                    self.hass,
                    username,
                    password_digest,
                    str(self._config_entry.data.get(CONF_FAMILY_ID, "")),
                    cloud_for_region(
                        self._config_entry.data.get(CONF_CLOUD_REGION)
                    ),
                )
                if detected_cloud is not None:
                    updated_data = dict(self._config_entry.data)
                    updated_data[CONF_PASSWORD_HASH] = password_digest
                    updated_data[CONF_CLOUD_REGION] = detected_cloud.region.value
                    self.hass.config_entries.async_update_entry(
                        self._config_entry,
                        data=updated_data,
                    )
                    # Do not create or replace an entry. The existing options,
                    # selected devices, areas and entity registry stay intact.
                    return self.async_abort(reason="reauth_successful")
                errors["base"] = "auth_failed"

        return self.async_show_form(
            step_id="reauth",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"username": username},
        )

    async def async_step_lock_users(self, user_input=None):
        """编辑门锁 userId → 名称 映射（每行 用户ID=名称）。"""
        if user_input is not None:
            options = dict(self._config_entry.options)
            options[CONF_LOCK_USER_NAMES] = parse_lock_user_names(
                user_input.get(CONF_LOCK_USER_NAMES, "")
            )
            return self.async_create_entry(title="", data=options)

        current = format_lock_user_names(
            self._config_entry.options.get(CONF_LOCK_USER_NAMES, {})
        )
        return self.async_show_form(
            step_id="lock_users",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_LOCK_USER_NAMES,
                        default=current,
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            multiline=True,
                            type=selector.TextSelectorType.TEXT,
                        )
                    )
                }
            ),
        )

    async def async_step_devices(self, user_input=None):
        """重新选择要接入的设备。"""
        errors: dict[str, str] = {}
        if not self._devices:
            self._devices = await _fetch_devices(
                self.hass,
                str(self._config_entry.data.get(CONF_USERNAME, "")),
                str(self._config_entry.data.get(CONF_PASSWORD_HASH, "")),
                str(self._config_entry.data.get(CONF_FAMILY_ID, "")),
                cloud_for_region(
                    self._config_entry.data.get(CONF_CLOUD_REGION)
                ),
            )
            if not self._devices:
                errors["base"] = "no_devices"

        if user_input is not None:
            selected = merge_grouped_selection(user_input, self._devices)
            if not selected:
                errors["base"] = "no_devices_selected"
            else:
                options = dict(self._config_entry.options)
                options[CONF_SELECTED_DEVICE_IDS] = selected
                return self.async_create_entry(
                    title="",
                    data=options,
                )

        current = selected_device_ids(
            self._config_entry.options,
            [str(d["device_id"]) for d in self._devices],
        )
        if user_input is not None:
            current = set(merge_grouped_selection(user_input, self._devices))
        return self.async_show_form(
            step_id="devices",
            data_schema=_device_selection_schema(self._devices, current),
            errors=errors,
        )
