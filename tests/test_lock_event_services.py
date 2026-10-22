"""History services preserve the image filter and scoped lock ownership."""
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

from test_service_handlers import _load_module


class LockHistoryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_module()
        self.handlers = {}
        def register(domain, name, handler, **kwargs):
            self.handlers[name] = handler
        self.coordinator = types.SimpleNamespace(
            devices={'a': {'device_type_raw': 522}},
            async_list_events=AsyncMock(return_value=[{
                'device_id': 'a', 'kind': 'ring', 'time': '1',
                'type': 'image', 'media_id': 'media-source://image', 'file': 'C:/private.jpg'}]),
            async_list_openings=AsyncMock(return_value=[{
                'device_id': 'a', 'id': 'one', 'time': 100, 'open_direction': 'inside',
                'unlock_user_id': 7, 'unlock_user_name': '旧用户', 'unlock_method': 'password',
                'file': 'C:/private.json',
            }]),
            set_lock_user_name=MagicMock(return_value=True),
        )
        self.hass = types.SimpleNamespace(
            data={'orvibo_smart_control': {'entry': self.coordinator}},
            bus=types.SimpleNamespace(async_listen_once=lambda *args: None),
            services=types.SimpleNamespace(async_register=register),
        )
        await self.module.async_register_services(self.hass)

    async def test_images_filtered_before_limit_and_no_filesystem_paths(self):
        response = await self.handlers['list_events'](types.SimpleNamespace(data={
            'device_id': 'a', 'limit': 30, 'media_type': 'image'}))
        self.coordinator.async_list_events.assert_awaited_once_with('a', 30, 'image')
        self.assertEqual(len(response['events']), 1)
        self.assertNotIn('file', response['events'][0])

    async def test_invalid_filter_and_unknown_lock_never_read_files(self):
        response = await self.handlers['list_events'](types.SimpleNamespace(data={'media_type': 'other'}))
        self.assertIn('error', response)
        response = await self.handlers['list_events'](types.SimpleNamespace(data={'device_id': 'missing', 'media_type': 'image'}))
        self.assertEqual(response, {'events': []})
        self.coordinator.async_list_events.assert_not_awaited()

    async def test_user_zero_can_be_named_by_service(self):
        await self.handlers['set_lock_user_name'](types.SimpleNamespace(data={
            'device_id': 'a', 'user_id': 0, 'name': '管理员'}))
        self.coordinator.set_lock_user_name.assert_called_once_with('a', '0', '管理员')

    async def test_openings_are_scoped_and_inside_does_not_expose_person(self):
        response = await self.handlers['list_openings'](types.SimpleNamespace(data={
            'device_id': 'a', 'entry_id': 'entry', 'limit': 900}))
        self.coordinator.async_list_openings.assert_awaited_once_with('a', 500)
        self.assertEqual(response, {'openings': [
            {'device_id': 'a', 'id': 'one', 'time': 100, 'open_direction': 'inside'},
        ]})

    async def test_opening_query_rejects_missing_device_and_does_not_read_unknown_lock(self):
        handler = self.handlers['list_openings']
        self.assertIn('error', await handler(types.SimpleNamespace(data={})))
        for data in ({'device_id': 'missing'}, {'device_id': 'a', 'entry_id': 'other'}):
            self.assertEqual(await handler(types.SimpleNamespace(data=data)), {'openings': []})
        self.coordinator.async_list_openings.assert_not_awaited()

    async def test_openings_sort_limit_dedupe_and_preserve_outside_user_zero(self):
        self.coordinator.async_list_openings.return_value = [
            {'device_id': 'a', 'id': 'one', 'time': 100, 'open_direction': 'inside'},
            {'device_id': 'a', 'id': 'two', 'time': 200, 'open_direction': 'outside',
             'unlock_user_id': 0, 'unlock_user_name': '管理员', 'unlock_method': 'fingerprint',
             'file': 'C:/private.json'},
        ]
        self.hass.data['orvibo_smart_control']['duplicate-entry'] = self.coordinator
        response = await self.handlers['list_openings'](types.SimpleNamespace(data={
            'device_id': 'a', 'limit': 1}))
        self.assertEqual(len(response['openings']), 1)
        self.assertEqual(response['openings'][0]['unlock_user_id'], 0)
        self.assertNotIn('file', response['openings'][0])
