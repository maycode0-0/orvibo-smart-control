"""Protocol and state-sequence regressions for opening direction and identity."""
import copy
import unittest
import importlib
import sys
import types
from unittest.mock import MagicMock, patch

from test_lock_manager import _load_module


class LockOpeningTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_module()
        self.status = __import__(self.module.__package__ + '.lock_status', fromlist=['lock_status'])
        self.now = 100.0
        self.manager = self.module.LockEventManager(
            lambda device, user: {('a', '0'): '管理员', ('a', '7'): '张三', ('b', '7'): '李四'}.get((device, str(user))),
            clock=lambda: self.now,
        )

    def unlock(self, method='fingerprint', user=7, timestamp=1, **extra):
        return {'cmd': 352, 'time': timestamp, 'event': {
            'server': 'doorLock', 'name': 'unlockEvent',
            'value': {'type': method, 'userId': user, **extra}}}

    def door(self, opened):
        return {'cmd': 42, 'properties': {'doorLock': {'doorState': 'on' if opened else 'off'}}}

    def test_credential_methods_always_outside_even_with_inside_hint(self):
        for method in ('password', 'fingerprint', 'key', 'card', 'temppassword', 'temp password', 'emergency fingerprint', 'emergency password', 'emergency card'):
            with self.subTest(method=method):
                event = self.status.normalize_lock_event(self.unlock(method, direction='inside'))
                self.assertEqual(event['open_direction'], 'outside')

    def test_other_unlock_methods_follow_inside_policy_without_a_previous_person(self):
        for method in ('face', 'remote', 'bluetooth', 'manual', '', None, 20):
            with self.subTest(method=method):
                self.manager.build_event('a', self.unlock(timestamp=1))
                event = self.manager.build_event('a', self.unlock(method=method, user=7, timestamp=2))
                self.assertEqual(event['open_direction'], 'inside')
                self.assertIsNone(event['unlock_user_id'])
                self.assertNotIn('unlock_user_name', event)
                opened = self.manager.build_event('a', self.door(True))
                self.assertEqual(opened['open_direction'], 'inside')
                self.assertIsNone(opened['unlock_user_id'])
                self.assertNotIn('opened_by_name', opened)
                self.manager.remove('a')

    def test_door_only_opening_follows_inside_policy(self):
        self.manager.build_event('a', self.door(False))
        opened = self.manager.build_event('a', self.door(True))
        self.assertEqual(opened['open_direction'], 'inside')
        self.assertIsNone(opened['unlock_user_id'])
        self.assertNotIn('opened_by_name', opened)

    def test_new_user_id_zero_is_a_valid_administrator(self):
        event = self.manager.build_event('a', self.unlock(user=0))
        self.assertEqual(event['unlock_user_id'], 0)
        self.assertEqual(event['unlock_user_name'], '管理员')

    def test_old_flat_event_has_authorized_id_and_snapshot(self):
        packet = {'cmd': 352, 'event': {'name': 'unlock', 'value': {
            'type': 'temp password', 'userID': 7, 'picture_url': '/a/picture.jpg'}}}
        before = copy.deepcopy(packet)
        event = self.manager.build_event('a', packet)
        self.assertEqual(event['unlock_user_name'], '张三')
        self.assertEqual(event['pic_url'], '/a/picture.jpg')
        self.assertEqual(event['unlock_method'], 'temporary_password')
        self.assertEqual(packet, before)

    def test_legacy_inside_has_no_outside_person(self):
        self.manager.build_event('a', self.unlock())
        inner = self.manager.build_event('a', {'cmd': 42, 'value2': 65535, 'value4': 20})
        self.assertEqual(inner['open_direction'], 'inside')
        self.assertIsNone(inner['unlock_user_id'])
        opened = self.manager.build_event('a', self.door(True))
        self.assertEqual(opened['open_direction'], 'inside')
        self.assertNotIn('opened_by_name', opened)
        self.assertIsNone(opened['unlock_user_id'])

    def test_legacy_credential_codes_are_outside(self):
        for code, method in ((1, 'fingerprint'), (2, 'password'), (3, 'card'), (4, 'temporary_password'), (5, 'key')):
            event = self.status.normalize_lock_event({'cmd': 42, 'value2': 65535, 'value4': code})
            self.assertEqual(event['open_direction'], 'outside')
            self.assertEqual(event['unlock_method'], method)

    def test_no_cross_protocol_numeric_enum(self):
        event = self.status.normalize_lock_event(self.unlock(method=20))
        self.assertEqual(event['open_direction'], 'inside')
        self.assertIsNone(self.status.normalize_lock_event({'cmd': 42, 'value2': 7, 'value4': 20}))
        self.assertIsNone(self.status.normalize_lock_event({'event': {'name': 'manual_unlock', 'value': {}}}))

    def test_credential_event_wins_over_conflicting_legacy_alarm(self):
        event = self.status.normalize_lock_event({**self.unlock('password'), 'cmd': 42, 'value2': 65535, 'value4': 20})
        self.assertEqual(event['open_direction'], 'outside')

    def test_protocol_user_id_field_takes_precedence(self):
        event = self.status.normalize_lock_event(self.unlock(user=0, userID=8))
        self.assertEqual(event['unlock_user_id'], 0)

    def test_inside_lock_state_does_not_mean_inside_opening(self):
        event = self.manager.build_event('a', {'properties': {'doorLock': {'insideLockState': 'off'}}})
        self.assertNotIn('open_direction', event)

    def test_recent_unlock_consumed_by_one_opening(self):
        unlocked = self.manager.build_event('a', self.unlock())
        first = self.manager.build_event('a', self.door(True))
        self.assertEqual(first['opening_id'], unlocked['opening_id'])
        self.assertEqual(first['time'], unlocked['time'])
        self.assertEqual(first['open_direction'], 'outside')
        self.assertEqual(first['opened_by_name'], '张三')
        self.manager.build_event('a', self.door(False))
        second = self.manager.build_event('a', self.door(True))
        self.assertNotEqual(second['opening_id'], first['opening_id'])
        self.assertEqual(second['open_direction'], 'inside')
        self.assertIsNone(second['unlock_user_id'])

    def test_expired_unlock_does_not_reuse_person_or_direction(self):
        self.manager.build_event('a', self.unlock())
        self.now += 31
        event = self.manager.build_event('a', self.door(True))
        self.assertEqual(event['open_direction'], 'inside')
        self.assertIsNone(event['unlock_user_id'])

    def test_replayed_unlock_after_state_change_stays_deduplicated(self):
        packet = self.unlock()
        self.manager.build_event('a', packet)
        self.manager.build_event('a', self.door(True))
        self.manager.build_event('a', self.door(False))
        self.assertIsNone(self.manager.build_event('a', packet))
        event = self.manager.build_event('a', self.door(True))
        self.assertEqual(event['open_direction'], 'inside')

    def test_two_images_at_same_second_are_distinct_events(self):
        self.assertIsNotNone(self.manager.build_event('a', self.unlock(url='/1.jpg')))
        self.assertIsNotNone(self.manager.build_event('a', self.unlock(url='/2.jpg')))

    def test_two_locks_do_not_share_identity_or_correlation(self):
        self.manager.build_event('a', self.unlock())
        event = self.manager.build_event('b', self.door(True))
        self.assertIsNone(event['unlock_user_id'])
        event = self.manager.build_event('b', self.unlock())
        self.assertEqual(event['unlock_user_name'], '李四')

    def test_late_producer_timestamp_does_not_match_new_opening(self):
        self.manager.build_event('a', self.unlock(timestamp=10))
        event = self.manager.build_event('a', {**self.door(True), 'time': 100})
        self.assertEqual(event['open_direction'], 'inside')

    def test_future_unlock_cannot_attribute_an_earlier_opening(self):
        self.manager.build_event('a', self.unlock(timestamp=100))
        event = self.manager.build_event('a', {**self.door(True), 'time': 90})
        self.assertEqual(event['open_direction'], 'inside')

    def test_label_cannot_call_an_inside_opening_fingerprint(self):
        self.assertEqual(self.status.format_unlock_label(7, '张三', 'inside', None), '内开')
        self.assertEqual(self.status.format_unlock_label(7, '张三', 'outside', 'fingerprint'), '张三外开（指纹）')
        self.assertEqual(self.status.format_unlock_label(None, None, 'outside', 'key'), '外开（钥匙，人员未知）')
        self.assertEqual(self.status.format_unlock_label(7, '张三', 'inside', 'fingerprint'), '张三外开（指纹）')
        self.assertEqual(self.status.format_unlock_label(7, '张三', 'outside', 'face'), '内开')
        self.assertEqual(self.status.format_unlock_label(7, '张三', 'unknown', None), '内开')

    def test_opening_identity_is_stable_when_timestamped_unlock_is_replayed_after_reload(self):
        packet = self.unlock(timestamp=20)
        first = self.manager.build_event('a', packet)
        self.manager.remove('a')
        second = self.manager.build_event('a', packet)
        self.assertEqual(first['opening_id'], second['opening_id'])

    def test_openings_always_have_a_valid_time(self):
        for timestamp in (None, 'bad', -1, float('nan'), float('inf')):
            with self.subTest(timestamp=timestamp), patch.object(self.module.time, 'time', return_value=1234):
                self.manager.remove('a')
                unlocked = self.manager.build_event('a', self.unlock(timestamp=timestamp))
                self.assertEqual(unlocked['time'], 1234)
                self.manager.remove('a')
                opened = self.manager.build_event('a', {**self.door(True), 'time': timestamp})
                self.assertEqual(opened['time'], 1234)

    def test_nonfinite_correlation_is_rejected(self):
        self.assertIsNone(self.status.resolve_opened_by({'user_id': 7}, float('nan')))

    def test_old_event_can_archive_picture_without_changing_latest_person(self):
        self.manager.build_event('a', self.unlock(user=0, timestamp=20))
        old = self.manager.build_event('a', self.unlock(user=7, timestamp=10, url='/old.jpg'))
        self.assertTrue(old['historical'])
        self.assertEqual(old['pic_url'], '/old.jpg')
        opened = self.manager.build_event('a', self.door(True))
        self.assertEqual(opened['opened_by_name'], '管理员')


class CoordinatorOpeningTests(unittest.TestCase):
    def setUp(self):
        from test_coordinator_transport import _load_coordinator_module
        self.module = _load_coordinator_module('orvibo_opening_coordinator_test')
        logic = _load_module()
        store = importlib.import_module(logic.__package__ + '.state_store')
        dispatcher = importlib.import_module(logic.__package__ + '.status_dispatcher')
        self.module.StateSource = store.StateSource
        sys.modules[self.module.__package__ + '.const'].LOCK_EVENT = 'orvibo_smart_control_lock_event'
        coordinator = self.module.OrviboSmartControlCoordinator.__new__(self.module.OrviboSmartControlCoordinator)
        coordinator.device_states = {'a': {}}
        coordinator.state_store = store.StateStore(coordinator.device_states)
        coordinator._lock_user_names = {}
        coordinator._lock_user_names_shared = {'a/7': '张三', 'b/7': '李四'}
        coordinator.lock_events = logic.LockEventManager(coordinator.lock_user_name)
        coordinator.lock_media = types.SimpleNamespace(attach_urls=lambda *args: {}, record_opening=MagicMock())
        coordinator.hass = types.SimpleNamespace(bus=types.SimpleNamespace(async_fire=MagicMock()))
        coordinator._redaction_salt = b'salt'
        coordinator._schedule_lock_reset = MagicMock()
        self.coordinator = coordinator
        self.dispatcher = dispatcher.StatusUpdateDispatcher(
            {'a': {'device_type_raw': 522}}, coordinator.device_states, coordinator.state_store, {}, [],
            on_motion=MagicMock(), on_emergency=MagicMock(),
            on_lock_transient=lambda state, raw, device: coordinator._apply_lock_transient_event(device, state, raw),
            on_lock_message=MagicMock(), on_lock_event=coordinator._publish_lock_event,
            on_updated=MagicMock(),
        )

    def test_entity_fields_follow_normalized_event_and_ignore_replay(self):
        packet = {'cmd': 352, 'time': 1, 'event': {'server': 'doorLock', 'name': 'unlockEvent', 'value': {'type': 'fingerprint', 'userId': 7}}}
        self.dispatcher.dispatch('a', packet)
        state = self.coordinator.device_states['a']
        self.assertEqual(state['open_direction'], 'outside')
        self.assertEqual(state['unlock_user_id'], 7)
        self.dispatcher.dispatch('a', {'cmd': 42, 'value2': 65535, 'value4': 20, 'time': 2})
        self.assertEqual(state['open_direction'], 'inside')
        self.assertIsNone(state['unlock_user_id'])
        self.dispatcher.dispatch('a', packet)
        self.assertEqual(state['open_direction'], 'inside')
        self.assertIsNone(state['unlock_user_id'])

    def test_only_current_opening_events_are_sent_to_history(self):
        self.coordinator._publish_lock_event('a', {'properties': {'doorLock': {'doorState': 'off'}}})
        self.coordinator.lock_media.record_opening.assert_not_called()
        for timestamp in (20, 10):
            self.coordinator._publish_lock_event('a', {'cmd': 352, 'time': timestamp, 'event': {
                'server': 'doorLock', 'name': 'unlockEvent', 'value': {'type': 'fingerprint', 'userId': 7}}})
        self.assertEqual(self.coordinator.lock_media.record_opening.call_count, 1)
        self.assertFalse(self.coordinator.lock_media.record_opening.call_args.args[1].get('historical', False))

    def test_number_name_mapping_resolves_each_outside_opening(self):
        status = __import__(self.coordinator.lock_events.__module__.rsplit('.', 1)[0] + '.lock_status', fromlist=['lock_status'])
        self.coordinator._lock_user_names_shared = status.parse_lock_user_names('1=家人甲\n2=家人乙\n3=家人丙')
        for user_id, name in ((1, '家人甲'), (2, '家人乙'), (3, '家人丙')):
            with self.subTest(user_id=user_id):
                packet = {'cmd': 352, 'time': user_id, 'event': {
                    'server': 'doorLock', 'name': 'unlockEvent',
                    'value': {'type': 'fingerprint', 'userId': user_id}}}
                self.dispatcher.dispatch('a', packet)
                event = self.coordinator.hass.bus.async_fire.call_args.args[1]
                self.assertEqual(event['unlock_user_name'], name)
                self.assertEqual(event['open_direction'], 'outside')

    def test_service_names_are_scoped_per_lock_including_zero(self):
        self.assertTrue(self.coordinator.set_lock_user_name('a', 0, '管理员'))
        self.assertEqual(self.coordinator.lock_user_name('a', 0), '管理员')
        self.assertIsNone(self.coordinator.lock_user_name('b', 0))
        self.assertEqual(self.coordinator._lock_user_names_shared['a/0'], '管理员')
        self.coordinator.set_lock_user_name('a', 0, '')
        self.assertIsNone(self.coordinator.lock_user_name('a', 0))

    def test_credential_specific_name_prevents_same_id_from_sharing_person(self):
        self.coordinator._lock_user_names_shared.update({'a/fingerprint/7': '张三指纹', 'a/password/7': '访客密码'})
        self.assertEqual(self.coordinator.lock_user_name('a', 7, 'fingerprint'), '张三指纹')
        self.assertEqual(self.coordinator.lock_user_name('a', 7, 'password'), '访客密码')
        self.assertEqual(self.coordinator.lock_user_name('b', 7, 'password'), '李四')


if __name__ == '__main__':
    unittest.main()
