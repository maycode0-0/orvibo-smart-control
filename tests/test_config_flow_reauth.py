"""Focused tests for account and common options flows."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibo_smart_control"
    / "config_flow.py"
)


class _ConfigFlow:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        return None


class _OptionsFlow:
    def async_show_menu(self, **kwargs):
        return {"type": "menu", **kwargs}

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}


def _module(name: str, **values) -> ModuleType:
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    return module


def _load_config_flow():
    package_name = "orvibo_smart_control_reauth_test"
    package = _module(package_name)
    package.__path__ = [str(MODULE_PATH.parent)]

    cloud = SimpleNamespace(region=SimpleNamespace(value="china"), ssl_host="ssl")
    config_entries = _module(
        "homeassistant.config_entries",
        ConfigFlow=_ConfigFlow,
        OptionsFlow=_OptionsFlow,
    )
    selector = MagicMock()
    modules = {
        package_name: package,
        "voluptuous": _module(
            "voluptuous",
            Schema=lambda value: value,
            Required=lambda key, **kwargs: key,
            Optional=lambda key, **kwargs: key,
        ),
        "homeassistant": _module("homeassistant", config_entries=config_entries),
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": _module("homeassistant.core", HomeAssistant=object),
        "homeassistant.data_entry_flow": _module(
            "homeassistant.data_entry_flow", FlowResult=dict
        ),
        "homeassistant.helpers": _module(
            "homeassistant.helpers", selector=selector
        ),
        "homeassistant.helpers.selector": selector,
        "homeassistant.helpers.aiohttp_client": _module(
            "homeassistant.helpers.aiohttp_client",
            async_get_clientsession=lambda hass: object(),
        ),
        f"{package_name}.https_client": _module(
            f"{package_name}.https_client", HttpsClient=object
        ),
        f"{package_name}.cloud": _module(
            f"{package_name}.cloud",
            CHINA_CLOUD=cloud,
            CloudEndpoint=object,
            cloud_for_region=lambda region: cloud,
        ),
        f"{package_name}.const": _module(
            f"{package_name}.const",
            DOMAIN="orvibo_smart_control",
            CONF_USERNAME="username",
            CONF_PASSWORD="password",
            CONF_PASSWORD_HASH="password_hash",
            CONF_CLOUD_REGION="cloud_region",
            CONF_FAMILY_ID="family_id",
            CONF_LOCK_USER_NAMES="lock_user_names",
            CONF_TRANSPORT_MODE="transport_mode",
            CONF_USE_INDEPENDENT_LAN_CREDENTIALS="use_independent_lan_credentials",
            CONF_LAN_USERNAME="lan_username",
            CONF_LAN_PASSWORD="lan_password",
            CONF_LAN_PASSWORD_HASH="lan_password_hash",
            CONF_POLL_INTERVAL_MINUTES="poll_interval_minutes",
            CONF_AVAILABILITY_NOTIFICATIONS="availability_notifications",
            CONF_NOTIFY_ONLINE="notify_online",
            CONF_NOTIFY_OFFLINE="notify_offline",
            CONF_NOTIFY_SERVICE="notify_service",
            CONF_UPDATE_CHECK_ENABLED="update_check_enabled",
            CONF_UPDATE_CHECK_INTERVAL_HOURS="update_check_interval_hours",
            DEFAULT_POLL_INTERVAL_MINUTES=30,
            MIN_POLL_INTERVAL_MINUTES=5,
            MAX_POLL_INTERVAL_MINUTES=1440,
            DEFAULT_UPDATE_CHECK_INTERVAL_HOURS=24,
            MIN_UPDATE_CHECK_INTERVAL_HOURS=6,
            MAX_UPDATE_CHECK_INTERVAL_HOURS=168,
        ),
        f"{package_name}.capabilities": _module(
            f"{package_name}.capabilities",
            TransportMode=SimpleNamespace(
                AUTO=SimpleNamespace(value="auto"),
                LAN_ONLY=SimpleNamespace(value="lan_only"),
                CLOUD_ONLY=SimpleNamespace(value="cloud_only"),
            ),
        ),
        f"{package_name}.device_types": _module(
            f"{package_name}.device_types",
            DeviceCategory=SimpleNamespace(OTHER="other", UNKNOWN="unknown"),
            classify_device=lambda device: "other",
            get_device_profile=lambda device: None,
            is_hidden_category=lambda category: False,
        ),
        f"{package_name}.lock_status": _module(
            f"{package_name}.lock_status",
            format_lock_user_names=lambda value: "",
            parse_lock_user_names=lambda value: {},
        ),
        f"{package_name}.selection": _module(
            f"{package_name}.selection",
            CONF_HIDDEN_DEVICE_NAME_PATTERNS="hidden_device_name_patterns",
            CONF_SELECTED_DEVICE_IDS="selected_device_ids",
            parse_hidden_device_name_patterns=lambda value: list(value)
            if isinstance(value, (list, tuple, set))
            else [
                line.strip()
                for line in str(value or "").splitlines()
                if line.strip()
            ],
            selected_device_ids=lambda options, devices: set(devices),
            visible_devices_by_name=lambda devices, patterns: list(devices),
        ),
        f"{package_name}.protocol": _module(
            f"{package_name}.protocol",
            password_hash=lambda value: f"hash:{value}",
        ),
    }

    module_name = f"{package_name}.config_flow"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    config_flow = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        sys.modules[module_name] = config_flow
        spec.loader.exec_module(config_flow)
    return config_flow, cloud


class TestOptionsReauth(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module, cls.cloud = _load_config_flow()

    def _flow(self):
        entry = SimpleNamespace(
            data={
                "username": "account@example.com",
                "password_hash": "old-hash",
                "family_id": "family-1",
                "cloud_region": "china",
                "unrelated": "preserve-me",
            },
            options={"selected_device_ids": ["device-1"]},
        )
        flow = self.module.OrviboSmartControlOptionsFlow(entry)
        flow.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=MagicMock()),
            data={},
        )
        return flow, entry

    async def test_menu_exposes_all_common_options(self):
        flow, _ = self._flow()
        result = await flow.async_step_init()
        self.assertEqual(
            result["menu_options"],
            [
                "transport_mode",
                "lan_credentials",
                "devices",
                "device_name_filter",
                "reauth",
                "sync_device_names",
                "clear_local_data",
                "polling",
                "availability_notifications",
                "update_check",
                "lock_users",
            ],
        )

    def test_device_selector_uses_checkbox_list_mode(self):
        select_config = self.module.selector.SelectSelectorConfig
        select_config.reset_mock()
        devices = [
            {
                "device_id": "light-1",
                "device_name": "客厅灯",
                "device_type_raw": 38,
            },
            {
                "device_id": "light-2",
                "device_name": "卧室灯",
                "device_type_raw": 38,
            },
        ]

        with patch.object(
            self.module,
            "_device_option_label",
            side_effect=lambda device: device["device_name"],
        ):
            self.module._device_selection_schema(devices, set())

        config = select_config.call_args.kwargs
        self.assertTrue(config["multiple"])
        self.assertIs(
            config["mode"],
            self.module.selector.SelectSelectorMode.LIST,
        )

    def test_p20_model_is_supported_without_type_metadata(self):
        device = {
            "device_id": "lock-1",
            "device_name": "入户门锁",
            "device_type_raw": 999999,
            "model": "dec7d494f0454110805c0d5f7e7cba73",
        }
        supported_profile = SimpleNamespace(
            category="door_lock",
            registration_only=False,
        )

        with patch.object(
            self.module,
            "get_device_profile",
            return_value=supported_profile,
        ) as get_profile:
            label = self.module._device_option_label(device)

        self.assertEqual(label, "入户门锁")
        self.assertNotIn("暂未支持", label)
        get_profile.assert_called_once_with(device)

    async def test_device_name_filter_saves_rules_and_preserves_options(self):
        flow, _ = self._flow()

        result = await flow.async_step_device_name_filter(
            {"hidden_device_name_patterns": "测试\n*控制器*"}
        )

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            result["data"]["hidden_device_name_patterns"],
            ["测试", "*控制器*"],
        )
        self.assertEqual(result["data"]["selected_device_ids"], ["device-1"])

    async def test_device_name_filter_can_be_cleared(self):
        flow, entry = self._flow()
        entry.options["hidden_device_name_patterns"] = ["测试"]

        result = await flow.async_step_device_name_filter(
            {"hidden_device_name_patterns": ""}
        )

        self.assertEqual(result["type"], "create_entry")
        self.assertNotIn("hidden_device_name_patterns", result["data"])
        self.assertEqual(result["data"]["selected_device_ids"], ["device-1"])

    async def test_transport_mode_accepts_lan_only(self):
        flow, _ = self._flow()
        result = await flow.async_step_transport_mode(
            {"transport_mode": "lan_only"}
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["transport_mode"], "lan_only")

    async def test_independent_lan_password_is_hashed(self):
        flow, _ = self._flow()
        result = await flow.async_step_lan_credentials(
            {
                "use_independent_lan_credentials": True,
                "lan_username": "mixpad@example.com",
                "lan_password": "secret",
            }
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["lan_password_hash"], "hash:secret")
        self.assertNotIn("lan_password", result["data"])

    async def test_polling_interval_is_bounded(self):
        flow, _ = self._flow()
        result = await flow.async_step_polling(
            {"poll_interval_minutes": 99999}
        )
        self.assertEqual(result["data"]["poll_interval_minutes"], 1440)

    def test_local_cleanup_never_escapes_integration_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_root = Path(temp_dir)
            target = media_root / "orvibo_smart_control"
            target.mkdir()
            (target / "event.jpg").write_bytes(b"image")
            unrelated = media_root / "keep.txt"
            unrelated.write_text("keep", encoding="utf-8")

            removed = self.module._clear_integration_media(media_root)

            self.assertEqual(removed, 1)
            self.assertFalse(target.exists())
            self.assertTrue(unrelated.exists())

    async def test_success_updates_same_entry_and_preserves_other_settings(self):
        flow, entry = self._flow()
        original_options = dict(entry.options)
        validator = AsyncMock(return_value=self.cloud)

        with patch.object(self.module, "_validate_updated_credentials", validator):
            result = await flow.async_step_reauth({"password": "new-password"})

        self.assertEqual(result, {"type": "abort", "reason": "reauth_successful"})
        flow.hass.config_entries.async_update_entry.assert_called_once()
        updated_entry = flow.hass.config_entries.async_update_entry.call_args.args[0]
        updated_data = flow.hass.config_entries.async_update_entry.call_args.kwargs[
            "data"
        ]
        self.assertIs(updated_entry, entry)
        self.assertEqual(updated_data["password_hash"], "hash:new-password")
        self.assertEqual(updated_data["family_id"], "family-1")
        self.assertEqual(updated_data["unrelated"], "preserve-me")
        self.assertEqual(entry.options, original_options)

    async def test_failed_login_does_not_modify_entry(self):
        flow, _ = self._flow()
        with patch.object(
            self.module,
            "_validate_updated_credentials",
            AsyncMock(return_value=None),
        ):
            result = await flow.async_step_reauth({"password": "wrong"})

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"]["base"], "auth_failed")
        flow.hass.config_entries.async_update_entry.assert_not_called()


class TestInitialSetupOptions(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module, cls.cloud = _load_config_flow()

    async def test_user_form_exposes_name_and_mixpad_settings(self):
        flow = self.module.OrviboSmartControlConfigFlow()

        result = await flow.async_step_user()

        self.assertEqual(result["type"], "form")
        self.assertEqual(
            set(result["data_schema"]),
            {
                "username",
                "password",
                "hidden_device_name_patterns",
                "use_independent_lan_credentials",
                "lan_username",
                "lan_password",
            },
        )

    async def test_independent_mixpad_requires_both_credentials(self):
        flow = self.module.OrviboSmartControlConfigFlow()

        result = await flow.async_step_user(
            {
                "username": "account@example.com",
                "password": "cloud-secret",
                "use_independent_lan_credentials": True,
                "lan_username": "",
                "lan_password": "",
            }
        )

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"]["base"], "lan_credentials_required")

    async def test_login_submission_prepares_initial_options(self):
        flow = self.module.OrviboSmartControlConfigFlow()
        flow.hass = object()
        cloud_client = SimpleNamespace(
            async_detect_cloud=AsyncMock(return_value=True),
            family_list=[{"familyId": "family-1", "familyName": "Home"}],
            family_id="family-1",
            family_name="Home",
            cloud=self.cloud,
            close=AsyncMock(),
        )
        visible = {"device_id": "visible", "device_name": "客厅灯"}

        with (
            patch.object(self.module, "HttpsClient", return_value=cloud_client),
            patch.object(flow, "_probe_ssl_login", AsyncMock(return_value=True)),
            patch.object(
                self.module,
                "_fetch_devices",
                AsyncMock(
                    return_value=[
                        visible,
                        {"device_id": "hidden", "device_name": "测试控制器"},
                    ]
                ),
            ),
            patch.object(
                self.module,
                "visible_devices_by_name",
                return_value=[visible],
            ),
            patch.object(
                self.module,
                "_device_selection_schema",
                return_value="device-schema",
            ),
        ):
            result = await flow.async_step_user(
                {
                    "username": "account@example.com",
                    "password": "cloud-secret",
                    "hidden_device_name_patterns": "测试\n*控制器*",
                    "use_independent_lan_credentials": True,
                    "lan_username": "mixpad@example.com",
                    "lan_password": "lan-secret",
                }
            )

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "devices")
        self.assertEqual(
            flow._pending_options,
            {
                "hidden_device_name_patterns": ["测试", "*控制器*"],
                "use_independent_lan_credentials": True,
                "lan_username": "mixpad@example.com",
                "lan_password_hash": "hash:lan-secret",
            },
        )
        self.assertEqual(result["data_schema"], "device-schema")

    async def test_initial_settings_are_saved_without_plaintext_password(self):
        flow = self.module.OrviboSmartControlConfigFlow()
        flow._username = "account@example.com"
        flow._password_hash = "hash:cloud-secret"
        flow._family_id = "family-1"
        flow._family_name = "Home"
        flow._pending_selected_ids = ["visible-device"]
        flow._pending_options = {
            "hidden_device_name_patterns": ["测试", "*控制器*"],
            "use_independent_lan_credentials": True,
            "lan_username": "mixpad@example.com",
            "lan_password_hash": "hash:lan-secret",
        }

        result = await flow._create_entry()

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            result["options"]["hidden_device_name_patterns"],
            ["测试", "*控制器*"],
        )
        self.assertEqual(
            result["options"]["lan_password_hash"], "hash:lan-secret"
        )
        self.assertNotIn("lan_password", result["options"])
        self.assertEqual(
            result["options"]["selected_device_ids"], ["visible-device"]
        )

    async def test_initial_device_step_hides_matching_names(self):
        flow = self.module.OrviboSmartControlConfigFlow()
        flow._devices = [
            {"device_id": "visible", "device_name": "客厅灯"},
            {"device_id": "hidden", "device_name": "测试控制器"},
        ]
        flow._pending_options = {
            "hidden_device_name_patterns": ["测试"],
        }

        with (
            patch.object(
                self.module,
                "visible_devices_by_name",
                return_value=[flow._devices[0]],
            ),
            patch.object(
                self.module,
                "_device_selection_schema",
                return_value="device-schema",
            ) as schema,
        ):
            result = await flow.async_step_devices()

        self.assertEqual(result["type"], "form")
        self.assertEqual(result["data_schema"], "device-schema")
        self.assertEqual(schema.call_args.args[0], [flow._devices[0]])
        self.assertEqual(schema.call_args.args[1], {"visible"})


if __name__ == "__main__":
    unittest.main()
