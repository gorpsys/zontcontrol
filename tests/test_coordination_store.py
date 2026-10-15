"""Offline R1/R2 wire contracts and non-atomic metadata store behavior."""

from dataclasses import FrozenInstanceError, replace
import json
import unittest

from zontcontrol.controller.contracts import CommandOutcomeUnknown, DeviceUnavailable
from zontcontrol.coordination.models import (
    GrantRecord, LeaderRecord, RecordError, RequestRecord, R1Record,
    decode_r1, decode_r2, encode_r1, encode_r2,
)
from zontcontrol.coordination.store import ExecutorStore, LeaderStore, RecordCapacityError
from tests.fakes.zont import FakeMetadataAdapter, FakeZontServer


OWNER = "c935f016-9ea1-4909-8f10-96fd98c792cb"
REQUEST = "48aace3b-258f-456a-b166-26bc05a56432"
NONCE = "9b7a32f0-a9e7-4b54-8fc4-6e55530371c8"
TOKEN = "0123456789abcdef0123456789abcdef"
PREFIX = "  Настоящий Дом 🏠  "


def leader(**changes):
    return replace(LeaderRecord(OWNER, TOKEN, 10**80, 60 * 1.1 / .9, .1), **changes)


def request(**changes):
    return replace(RequestRecord(OWNER, REQUEST, NONCE, 60, "request"), **changes)


def grant(**changes):
    return replace(GrantRecord(OWNER, REQUEST, NONCE, TOKEN, 10**80,
                               60 * 1.1 / .9, .1, NONCE), **changes)


class CodecTests(unittest.TestCase):
    def test_r1_round_trip_empty_and_large_sequence(self):
        record = R1Record(leader(), request())
        self.assertEqual(decode_r1(encode_r1(record)), record)
        self.assertEqual(decode_r1(None), R1Record())
        self.assertEqual(decode_r1(""), R1Record())
        self.assertEqual(encode_r1(R1Record()), "")
        self.assertEqual(decode_r1(encode_r1(R1Record(request=request()))).leader, None)
        with self.assertRaises(FrozenInstanceError):
            record.leader.seq = 1

    def test_r2_round_trip_preserves_real_prefix_and_visible_grant(self):
        raw = encode_r2(PREFIX, grant())
        self.assertTrue(raw.startswith(PREFIX + " [zc:"))
        self.assertIn("управление занято", raw)
        self.assertEqual(decode_r2(raw, PREFIX), grant())
        self.assertEqual(encode_r2(PREFIX, None), PREFIX)
        self.assertIsNone(decode_r2(PREFIX, PREFIX))

    def test_bad_r1_is_never_free(self):
        good = json.loads(encode_r1(R1Record(leader(), request())))
        values = [" ", "FREE", "{}", "null", "[]", "{", False,
                  json.dumps({**good, "v": 2}), json.dumps({**good, "v": True}),
                  json.dumps({**good, "leader_until": 100}),
                  '{"v":1,"v":1,"leader":null,"request":null}']
        for raw in values:
            with self.subTest(raw=raw), self.assertRaises(RecordError):
                decode_r1(raw)

    def test_bad_or_foreign_suffix_and_human_rename_never_free(self):
        raw = encode_r2(PREFIX, grant())
        for value in (None, "Другой дом", PREFIX + " [foreign:busy]", raw[:-1],
                      raw.replace('"v":1', '"v":2'), PREFIX + " [zc:unknown]",
                      raw + " ", "Новый дом" + raw[len(PREFIX):]):
            with self.subTest(value=value), self.assertRaises(RecordError):
                decode_r2(value, PREFIX)

    def test_invalid_identifiers_types_and_relative_durations(self):
        cases = [(leader, {"owner": "not-uuid"}), (leader, {"token": "a" * 31}),
                 (leader, {"seq": True}), (leader, {"seq": -1}),
                 (leader, {"lease_s": 1201}), (leader, {"lease_s": float("inf")}),
                 (leader, {"clock_rho": .005}), (leader, {"clock_rho": True}),
                 (request, {"nonce": ""}), (request, {"request_id": "no"}),
                 (request, {"lease_seconds": float("nan")}),
                 (request, {"state": "cancelled"}), (grant, {"grant_id": "bad"}),
                 (grant, {"revision": -1}), (grant, {"leader_token": "z" * 32}),
                 (grant, {"lease_s": 0}), (grant, {"clock_rho": .01})]
        for factory, changes in cases:
            with self.subTest(changes=changes), self.assertRaises(RecordError):
                factory(**changes)

    def test_clock_rho_uses_period_before_scaling_at_all_boundaries(self):
        for period, rho in ((.1, .1), (60, .1), (60.01, .05), (300, .05),
                            (300.01, .01), (600, .01), (600.01, .005)):
            with self.subTest(period=period):
                item = grant(lease_s=period * (1 + rho) / (1 - rho), clock_rho=rho)
                self.assertEqual(decode_r2(encode_r2(PREFIX, item), PREFIX), item)
        for state in ("request", "heartbeat", "finished"):
            self.assertEqual(decode_r1(encode_r1(R1Record(request=request(state=state)))).request.state,
                             state)

    def test_nested_wire_fields_are_strict_and_do_not_accept_absolute_deadlines(self):
        record = json.loads(encode_r1(R1Record(leader(), request())))
        for section, key, value in (("leader", "owner", "bad"), ("leader", "seq", True),
                                     ("leader", "lease_s", -1), ("request", "state", "unknown"),
                                     ("request", "nonce", None), ("leader", "until", 100)):
            changed = json.loads(json.dumps(record))
            changed[section][key] = value
            with self.subTest(key=key), self.assertRaises(RecordError):
                decode_r1(json.dumps(changed))
        for raw in (encode_r1(R1Record(leader())).replace('"lease_s":', '"lease_until":'),
                    '{"v":1,"leader":null,"request":NaN}',
                    '{"v":1,"leader":[],"request":null}'):
            with self.subTest(raw=raw), self.assertRaises(RecordError):
                decode_r1(raw)
        raw = encode_r2(PREFIX, grant())
        for changed in (raw.replace(TOKEN, "bad"), raw.replace('"nonce":', '"lease_until":'),
                        raw.replace('"revision":', '"extra":0,"revision":')):
            with self.subTest(raw=changed), self.assertRaises(RecordError):
                decode_r2(changed, PREFIX)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeZontServer({42: {"name": PREFIX, "notes": None,
                                          "heating": {"mode": "unchanged"}}})
        self.adapter = FakeMetadataAdapter(self.server)
        self.store = LeaderStore(self.adapter, 42, original_name=PREFIX,
                                 notes_capacity=10000, name_capacity=10000)

    def test_single_field_writes_and_explicit_free(self):
        record = R1Record(leader(), request())
        self.store.write_r1(record)
        self.store.write_r2(grant())
        self.assertEqual(self.store.read_r1(), record)
        self.assertEqual(self.store.read_r2(), grant())
        self.assertEqual(self.adapter.writes, [(42, "notes", encode_r1(record)),
                                               (42, "name", encode_r2(PREFIX, grant()))])
        self.store.write_r1(R1Record())
        self.store.write_r2(None)
        state = self.server.read_device(42)
        self.assertEqual(state, {"name": PREFIX, "notes": "", "heating": {"mode": "unchanged"}})

    def test_executor_can_write_r1_and_only_read_r2(self):
        executor = ExecutorStore(self.adapter, 42, original_name=PREFIX,
                                 notes_capacity=10000, name_capacity=10000)
        self.assertFalse(hasattr(executor, "write_r2"))
        executor.write_r1(R1Record(request=request()))
        self.assertIsNone(executor.read_r2())

    def test_complete_utf8_capacity_includes_prefix_suffix_and_sequence_growth(self):
        for field, raw, operation in (
            ("name", encode_r2(PREFIX, grant()), lambda store: store.write_r2(grant())),
            ("notes", encode_r1(R1Record(leader())),
             lambda store: store.write_r1(R1Record(leader()))),
        ):
            size = len(raw.encode("utf-8"))
            store = LeaderStore(self.adapter, 42, original_name=PREFIX,
                                notes_capacity=size if field == "notes" else 10000,
                                name_capacity=size if field == "name" else 10000)
            operation(store)
            store = LeaderStore(self.adapter, 42, original_name=PREFIX,
                                notes_capacity=size - 1 if field == "notes" else 10000,
                                name_capacity=size - 1 if field == "name" else 10000)
            count = len(self.server.requests)
            with self.assertRaises(RecordCapacityError):
                operation(store)
            self.assertEqual(len(self.server.requests), count)
        raw = encode_r1(R1Record(leader(seq=9)))
        store = LeaderStore(self.adapter, 42, original_name=PREFIX,
                            notes_capacity=len(raw.encode()), name_capacity=10000)
        store.write_r1(R1Record(leader(seq=9)))
        with self.assertRaises(RecordCapacityError):
            store.write_r1(R1Record(leader(seq=10)))

    def test_unknown_r1_and_renamed_or_foreign_name_are_not_overwritten(self):
        for field, value, write in (("notes", "private text", lambda: self.store.write_r1(R1Record())),
                                    ("name", "Новое имя", lambda: self.store.write_r2(None)),
                                    ("name", PREFIX + " [zc:other]", lambda: self.store.write_r2(None))):
            change = self.server.send_update(42, {field: value})
            self.server.apply(change)
            count = len(self.server.requests)
            with self.subTest(field=field), self.assertRaises(RecordError):
                write()
            self.assertEqual(len(self.server.requests), count)

    def test_truncation_and_normalization_are_unconfirmed_without_retry(self):
        for normalize in (lambda value: value[:-1], lambda value: value.replace('\":', '\": ')):
            for field in ("notes", "name"):
                self.setUp()
                self.adapter.normalize = {field: normalize}
                with self.subTest(field=field), self.assertRaises(CommandOutcomeUnknown):
                    if field == "notes":
                        self.store.write_r1(R1Record(leader()))
                    else:
                        self.store.write_r2(grant())
                self.assertEqual(len(self.server.requests), 1)

    def test_unknown_outcome_before_or_after_application_is_not_retried(self):
        for mode in ("defer", "lose_response"):
            for field in ("notes", "name"):
                self.setUp()
                self.adapter.write_mode = mode
                with self.subTest(mode=mode, field=field), self.assertRaises(CommandOutcomeUnknown):
                    if field == "notes":
                        self.store.write_r1(R1Record(leader()))
                    else:
                        self.store.write_r2(grant())
                self.assertEqual(len(self.server.requests), 1)
                if mode == "defer":
                    self.assertEqual(self.server.read_device(42)[field], None if field == "notes" else PREFIX)
                    self.server.apply(self.server.requests[0])
                self.assertIsNotNone(self.store.read_r1().leader if field == "notes" else self.store.read_r2())

    def test_confirmation_read_failure_is_unknown_but_presend_failure_is_known(self):
        self.adapter.fail_confirmation = True
        with self.assertRaises(CommandOutcomeUnknown):
            self.store.write_r1(R1Record(leader()))
        self.assertEqual(len(self.server.requests), 1)
        self.setUp()
        self.server.fail_before_send = True
        with self.assertRaises(DeviceUnavailable):
            self.store.write_r1(R1Record(leader()))
        self.assertEqual(len(self.server.requests), 0)

    def test_read_modify_write_is_not_compare_and_swap(self):
        self.store.write_r1(R1Record(leader()))
        other = LeaderStore(FakeMetadataAdapter(self.server), 42, original_name=PREFIX,
                            notes_capacity=10000, name_capacity=10000)
        stale = self.store.read_r1()
        other.write_r1(R1Record(leader(seq=1)))
        self.store.write_r1(replace(stale, request=request()))
        self.assertEqual(other.read_r1().leader, stale.leader)

    def test_invalid_store_configuration_fails_before_io(self):
        for changes in ({"device_id": True}, {"device_id": 0}, {"original_name": None},
                        {"notes_capacity": 0}, {"name_capacity": False}):
            args = {"device_id": 42, "original_name": PREFIX,
                    "notes_capacity": 10000, "name_capacity": 10000, **changes}
            with self.subTest(changes=changes), self.assertRaises(RecordError):
                LeaderStore(self.adapter, **args)
        self.assertEqual(self.adapter.writes, [])
