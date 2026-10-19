"""Offline strict v2 codecs, role capabilities and bounded metadata writes."""

from dataclasses import FrozenInstanceError, replace
import json
import unittest

from tests.fakes.clock import FakeClock
from tests.fakes.protocol import PROFILE
from tests.fakes.uuid import uuid_generator
from tests.fakes.zont import FakeMetadataAdapter, FakeZontServer
from zontcontrol.controller.contracts import CommandOutcomeUnknown, DeviceUnavailable
from zontcontrol.coordination.models import (
    GrantRecord, HandledRequest, LeaderRecord, ProtocolProfile, R1Record, R2Record,
    RecordError, RequestRecord, decode_r1, decode_r2, encode_r1, encode_r2, original_name,
)
from zontcontrol.coordination.clock import clock_factor, clock_rho
from zontcontrol.coordination.store import ExecutorStore, LeaderStore, RecordCapacityError
from zontcontrol.coordination.timing import OperationWindow, TimingViolation


OWNER = 'c935f016-9ea1-4909-8f10-96fd98c792cb'
REQUEST = '48aace3b-258f-456a-b166-26bc05a56432'
NONCE = '9b7a32f0-a9e7-4b54-8fc4-6e55530371c8'
TOKEN = '82f6c16d-2aca-45ce-8891-1072eb0a7c04'
PREFIX = '  Настоящий Дом 🏠  '


def leader(**changes):
    return replace(LeaderRecord(OWNER, TOKEN, NONCE, 300, .05, 1000, 1300, 1000), **changes)


def request(**changes):
    return replace(RequestRecord(OWNER, REQUEST, NONCE, TOKEN, 'acquire', 60), **changes)


def grant(**changes):
    return replace(GrantRecord(OWNER, REQUEST, NONCE, 60, .1, TOKEN, NONCE,
                              10**80, NONCE, 1000, 1000, 1000 + 60 * clock_factor(60)), **changes)


def r1(**changes):
    return replace(R1Record(PROFILE, REQUEST, leader(), (grant(),)), **changes)


class CodecTests(unittest.TestCase):
    def test_r1_round_trip_empty_and_large_sequence(self):
        record = r1(leader=leader(revision=10**80))
        self.assertEqual(decode_r1(encode_r1(PREFIX, record), PREFIX), record)
        self.assertIsNone(decode_r1(PREFIX, PREFIX))
        with self.assertRaises(FrozenInstanceError):
            record.leader.revision = 1

    def test_r2_round_trip_preserves_real_prefix_and_visible_grant(self):
        raw = encode_r1(PREFIX, r1())
        self.assertTrue(raw.startswith(PREFIX + ' [zc:'))
        self.assertIn('управление занято', raw)
        self.assertEqual(original_name(raw), PREFIX)
        self.assertEqual(decode_r2(encode_r2(R2Record(PROFILE.profile_id, request()))).request, request())
        self.assertIsNone(decode_r2(None))
        self.assertIsNone(decode_r2(''))

    def test_bad_r1_is_never_free(self):
        raw = encode_r1(PREFIX, r1())
        for value in (' ', 'FREE', '{}', 'null', '[]', '{', False, None,
                      raw.replace('"v":2', '"v":1'), raw.replace('"v":2', '"v":true'),
                      raw.replace('"v":2', '"v":2,"v":2')):
            with self.subTest(raw=value), self.assertRaises(RecordError):
                decode_r1(value, PREFIX)

    def test_bad_or_foreign_suffix_and_human_rename_never_free(self):
        raw = encode_r1(PREFIX, r1())
        for value in ('Другой дом', PREFIX + ' [foreign:busy]', raw[:-1],
                      PREFIX + ' [zc:unknown]', raw + ' ', 'Новый дом' + raw[len(PREFIX):]):
            with self.subTest(raw=value), self.assertRaises(RecordError):
                decode_r1(value, PREFIX)
        with self.assertRaises(RecordError):
            original_name(PREFIX + ' [zc:v1 busy]')

    def test_invalid_identifiers_types_and_durations(self):
        cases = [(leader, {'owner_id': 'bad'}), (leader, {'term_id': 'a' * 32}),
                 (leader, {'revision': True}), (leader, {'revision': -1}),
                 (leader, {'leader_until': 999}), (leader, {'period_s': float('inf')}),
                 (leader, {'clock_rho': .005}), (leader, {'clock_rho': True}),
                 (request, {'nonce': ''}), (request, {'request_id': 'no'}),
                 (request, {'lease_seconds': float('nan')}),
                 (request, {'operation': 'finish'}),
                 (request, {'operation': 'heartbeat', 'grant_id': NONCE}),
                 (request, {'grant_id': NONCE, 'grant_revision': 0}),
                 (request, {'operation': 'finish', 'grant_id': NONCE, 'grant_revision': True}),
                 (request, {'operation': 'request'}), (grant, {'grant_id': 'bad'}),
                 (grant, {'revision': -1}), (grant, {'issuer_term_id': 'z' * 32}),
                 (grant, {'valid_until': 1000}), (grant, {'clock_rho': .01}),
                 (grant, {'valid_from': 999}), (grant, {'valid_until': float('inf')})]
        for factory, changes in cases:
            with self.subTest(changes=changes), self.assertRaises(RecordError):
                factory(**changes)

    def test_clock_rho_uses_period_before_scaling_at_all_boundaries(self):
        for period in (.1, 60, 60.01, 300, 300.01, 600, 600.01):
            item = grant(lease_seconds=period, clock_rho=clock_rho(period),
                         valid_until=1000 + period * clock_factor(period))
            self.assertEqual(item.clock_rho, clock_rho(period))
            self.assertEqual(decode_r2(encode_r2(R2Record(PROFILE.profile_id,
                request(lease_seconds=period)))).request.lease_seconds, period)
        for op in ('acquire', 'heartbeat', 'finish', 'cancel'):
            changes = {} if op == 'acquire' else {'grant_id': NONCE, 'grant_revision': 0}
            req = request(operation=op, **changes)
            self.assertEqual(decode_r2(encode_r2(R2Record(PROFILE.profile_id, req))).request, req)

    def test_nested_wire_fields_are_strict_and_absolute_deadlines_checked(self):
        raw = encode_r1(PREFIX, r1())
        for old, new in (('"revision":', '"extra":0,"revision":'),
                         ('"sampled_at":', '"until":'), ('"v":2', '"v":NaN'),
                         ('"grants":[', '"grants":[')):
            if old == new:
                continue
            with self.subTest(new=new), self.assertRaises(RecordError):
                decode_r1(raw.replace(old, new), PREFIX)
        for raw in ('{"v":1,"profile_id":"x","request":null}',
                    '{"v":2,"profile_id":"' + PROFILE.profile_id + '","request":[]}'):
            with self.assertRaises(RecordError):
                decode_r2(raw)

    def test_two_ordered_slots_and_leader_horizon(self):
        a = grant()
        b = grant(grant_id=OWNER, valid_from=a.valid_until + clock_factor(1),
                  sampled_at=1050, valid_until=1120)
        self.assertEqual(r1(grants=(a, b)).grants, (a, b))
        for grants in ((a, b, b), (b, a), (a, replace(b, valid_from=a.valid_until))):
            with self.assertRaises(RecordError):
                r1(grants=grants)
        with self.assertRaises(RecordError):
            r1(leader=leader(leader_until=a.valid_until))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.uid = uuid_generator()
        self.server = FakeZontServer({42: {'name': PREFIX, 'notes': None,
                                          'heating': {'mode': 'unchanged'}}}, clock=self.clock)
        self.adapter = FakeMetadataAdapter(self.server)
        self.options = dict(original_name=PREFIX, notes_capacity=10000,
                            name_capacity=10000, profile=PROFILE)
        self.store = LeaderStore(self.adapter, 42, **self.options)
        self.executor = ExecutorStore(self.adapter, 42, **self.options)

    def write(self, record=None, store=None):
        (store or self.store).write_r1(record or r1(record_id=str(self.uid())),
            window=OperationWindow(self.clock, 1), role_deadline=self.clock.now() + 100)

    def publish(self, req=None, store=None):
        (store or self.executor).publish_request(req, window=OperationWindow(self.clock, 1))

    def test_single_field_writes_and_explicit_free(self):
        self.write()
        self.publish(request())
        self.assertEqual([field for _, field, _ in self.adapter.writes], ['name', 'notes'])
        self.assertEqual(self.store.read_r1().grants, (grant(),))
        self.assertEqual(self.store.read_r2().request, request())
        self.write(r1(record_id=str(self.uid()), leader=None, grants=()))
        self.publish()
        self.assertEqual(self.server.read_device(42)['heating'], {'mode': 'unchanged'})
        self.assertEqual(original_name(self.server.read_device(42)['name']), PREFIX)
        self.assertIsNone(self.store.read_r2().request)

    def test_executor_can_write_only_r2_and_leader_only_r1(self):
        self.assertFalse(hasattr(self.executor, 'write_r1'))
        self.assertFalse(hasattr(self.store, 'publish_request'))
        self.assertFalse(issubclass(LeaderStore, ExecutorStore))
        self.publish(request())
        self.assertIsNone(self.store.read_r1())
        self.assertEqual(self.server.read_device(42)['name'], PREFIX)

    def test_complete_utf8_capacity_includes_prefix_suffix_and_sequence_growth(self):
        record = r1(record_id=str(self.uid()))
        for field, raw, cls, op in (
            ('name', encode_r1(PREFIX, record), LeaderStore, lambda s: self.write(record, s)),
            ('notes', encode_r2(R2Record(PROFILE.profile_id, request())), ExecutorStore,
             lambda s: self.publish(request(), s))):
            size = len(raw.encode('utf-8'))
            options = {**self.options, field + '_capacity': size - 1}
            with self.subTest(field=field), self.assertRaises(RecordCapacityError):
                op(cls(self.adapter, 42, **options))
            self.assertEqual(self.adapter.writes, [])
            options[field + '_capacity'] = size
            op(cls(self.adapter, 42, **options))
            self.adapter.writes.clear()

    def test_unknown_data_and_rename_fail_without_write(self):
        for field, raw, operation in (('name', 'renamed', self.write),
                                      ('notes', 'unknown', self.publish)):
            self.setUp()
            self.server.apply(self.server.send_update(42, {field: raw}))
            with self.assertRaises(RecordError):
                operation()
            self.assertEqual(self.adapter.writes, [])

    def test_confirmation_read_failure_is_unknown_but_presend_failure_is_known(self):
        from unittest.mock import patch
        read = self.adapter.read_metadata_field
        count = 0
        def fail_after_write(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 3:
                raise DeviceUnavailable()
            return read(*args, **kwargs)
        with patch.object(self.adapter, 'read_metadata_field', side_effect=fail_after_write):
            with self.assertRaises(CommandOutcomeUnknown):
                self.write()
        self.assertEqual(len(self.adapter.writes), 1)
        self.setUp()
        with patch.object(self.adapter, 'read_metadata_field', side_effect=DeviceUnavailable):
            with self.assertRaises(DeviceUnavailable):
                self.write()
        self.assertEqual(self.adapter.writes, [])

    def test_size_types_and_invalid_store_arguments_never_send(self):
        for changes in ({'device_id': True}, {'device_id': 0}, {'original_name': None},
                        {'notes_capacity': 0}, {'name_capacity': False}):
            args = {'device_id': 42, **self.options, **changes}
            with self.subTest(changes=changes), self.assertRaises(RecordError):
                LeaderStore(self.adapter, **args)
        self.assertEqual(self.adapter.writes, [])

    def test_original_window_guard_and_readback_share_budget(self):
        window = OperationWindow(self.clock, 1)
        self.clock.advance(1.01)
        with self.assertRaises(TimingViolation):
            self.store.write_r1(r1(), window=window, role_deadline=100)
        self.assertEqual(self.adapter.writes, [])
        self.setUp()
        self.adapter.read_delay = .3
        self.adapter.write_delay = .5
        with self.assertRaises(CommandOutcomeUnknown):
            self.write()
        self.assertEqual(len(self.adapter.writes), 1)

    def test_profile_mismatch_and_record_id_replay_are_refused(self):
        self.write()
        previous = self.store.read_r1()
        with self.assertRaises(RecordError):
            self.write(previous)
        foreign = ProtocolProfile(OWNER, 1, 1)
        store = LeaderStore(self.adapter, 42, **{**self.options, 'profile': foreign})
        before = len(self.adapter.writes)
        with self.assertRaises(RecordError):
            store.read_r1()
        self.assertEqual(len(self.adapter.writes), before)

    def test_bootstrap_rejects_either_unknown_register_before_any_role_write(self):
        foreign = ProtocolProfile(OWNER, 1, 1)
        mismatched_parameters = replace(PROFILE, delta_cmd_seconds=2)
        cases = (
            ('name', PREFIX + ' [zc:legacy]'),
            ('notes', '{"v":1}'),
            ('name', encode_r1(PREFIX, r1(profile=foreign))),
            ('notes', encode_r2(R2Record(foreign.profile_id))),
            ('name', encode_r1(PREFIX, r1(profile=mismatched_parameters))),
        )
        for field, raw in cases:
            for role in ('leader', 'executor'):
                with self.subTest(field=field, raw=raw, role=role):
                    self.setUp()
                    self.server.apply(self.server.send_update(42, {field: raw}))
                    before = self.server.read_device(42)
                    requests = len(self.server.requests)
                    with self.assertRaises(RecordError):
                        self.write() if role == 'leader' else self.publish(request())
                    self.assertEqual(self.adapter.writes, [])
                    self.assertEqual(len(self.server.requests), requests)
                    self.assertEqual(self.server.read_device(42), before)

    def test_missing_other_register_never_gives_a_writer_the_other_capability(self):
        for role in ('leader', 'executor'):
            with self.subTest(role=role):
                self.setUp()
                if role == 'leader':
                    self.write(r1(record_id=str(self.uid()), leader=None, grants=()))
                    self.assertIsNone(self.server.read_device(42)['notes'])
                    self.assertFalse(hasattr(self.store, 'publish_request'))
                    field = 'name'
                else:
                    self.publish(request(leader_term_id=None))
                    self.assertEqual(self.server.read_device(42)['name'], PREFIX)
                    self.assertFalse(hasattr(self.executor, 'write_r1'))
                    field = 'notes'
                self.assertEqual([item[1] for item in self.adapter.writes], [field])
                self.assertEqual(original_name(self.server.read_device(42)['name']), PREFIX)
                self.assertEqual(self.server.read_device(42)['heating'], {'mode': 'unchanged'})
