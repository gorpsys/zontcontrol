"""Strict wire v2: name/R1 contains leader replies; notes/R2 only requests."""

from dataclasses import asdict, dataclass
import json
import math
from uuid import UUID

from .clock import clock_factor, clock_rho


class RecordError(ValueError):
    def __init__(self):
        super().__init__('Invalid or incompatible coordination record/profile (wire v2 required)')


def _uuid(value):
    if not isinstance(value, str):
        raise RecordError()
    try:
        parsed = UUID(value)
        if not parsed.int or str(parsed) != value:
            raise RecordError()
    except ValueError:
        raise RecordError() from None


def _counter(value):
    if type(value) is not int or value < 0:
        raise RecordError()


def _number(value, *, positive=False):
    try:
        if (type(value) not in (int, float) or not math.isfinite(value)
                or positive and value <= 0):
            raise RecordError()
    except OverflowError:
        raise RecordError() from None


def _period(period, rho):
    _number(period, positive=True)
    if type(rho) not in (int, float) or rho != clock_rho(period):
        raise RecordError()


@dataclass(frozen=True)
class ProtocolProfile:
    profile_id: str
    delta_seconds: float
    delta_cmd_seconds: float
    rho_version: int = 1

    def __post_init__(self):
        _uuid(self.profile_id)
        _number(self.delta_seconds, positive=True)
        _number(self.delta_cmd_seconds, positive=True)
        if type(self.rho_version) is not int or self.rho_version != 1:
            raise RecordError()


@dataclass(frozen=True)
class LeaderRecord:
    owner_id: str
    term_id: str
    clock_id: str
    period_s: float
    clock_rho: float
    sampled_at: float
    leader_until: float
    term_started_at: float
    revision: int = 0

    def __post_init__(self):
        for value in (self.owner_id, self.term_id, self.clock_id):
            _uuid(value)
        _period(self.period_s, self.clock_rho)
        _counter(self.revision)
        for value in (self.sampled_at, self.leader_until, self.term_started_at):
            _number(value)
        if not (self.term_started_at <= self.sampled_at < self.leader_until
                <= self.term_started_at + 1200
                and self.leader_until <= self.sampled_at + self.period_s):
            raise RecordError()


@dataclass(frozen=True)
class RequestRecord:
    owner_id: str
    request_id: str
    nonce: str
    leader_term_id: str | None
    operation: str
    lease_seconds: float
    grant_id: str | None = None
    grant_revision: int | None = None

    def __post_init__(self):
        for value in (self.owner_id, self.request_id, self.nonce):
            _uuid(value)
        if self.leader_term_id is not None:
            _uuid(self.leader_term_id)
        _number(self.lease_seconds, positive=True)
        if self.operation not in ('acquire', 'heartbeat', 'finish', 'cancel'):
            raise RecordError()
        if self.operation == 'acquire':
            if self.grant_id is not None or self.grant_revision is not None:
                raise RecordError()
        else:
            _uuid(self.leader_term_id)
            _uuid(self.grant_id)
            _counter(self.grant_revision)


@dataclass(frozen=True)
class GrantRecord:
    owner_id: str
    request_id: str
    grant_id: str
    lease_seconds: float
    clock_rho: float
    issuer_term_id: str
    clock_id: str
    revision: int
    nonce: str
    sampled_at: float
    valid_from: float
    valid_until: float

    def __post_init__(self):
        for value in (self.owner_id, self.request_id, self.grant_id,
                      self.issuer_term_id, self.clock_id, self.nonce):
            _uuid(value)
        _period(self.lease_seconds, self.clock_rho)
        _counter(self.revision)
        for value in (self.sampled_at, self.valid_from, self.valid_until):
            _number(value)
        if not (self.sampled_at <= self.valid_from < self.valid_until
                <= self.sampled_at + clock_factor(self.lease_seconds) * self.lease_seconds):
            raise RecordError()


@dataclass(frozen=True)
class ReplyRecord:
    owner_id: str
    request_id: str
    nonce: str
    reason: str
    status: str = 'declined'

    def __post_init__(self):
        for value in (self.owner_id, self.request_id, self.nonce):
            _uuid(value)
        if self.status != 'declined' or self.reason not in ('busy', 'expired', 'horizon', 'old_term'):
            raise RecordError()


@dataclass(frozen=True)
class HandledRequest:
    owner_id: str
    request_id: str
    nonce: str
    term_id: str
    operation: str
    grant_id: str | None
    outcome: str

    def __post_init__(self):
        for value in (self.owner_id, self.request_id, self.nonce, self.term_id):
            _uuid(value)
        if self.grant_id is not None:
            _uuid(self.grant_id)
        if (self.operation not in ('acquire', 'heartbeat', 'finish', 'cancel')
                or self.outcome not in ('issued', 'expired', 'finished', 'cancelled')):
            raise RecordError()


@dataclass(frozen=True)
class R1Record:
    profile: ProtocolProfile
    record_id: str
    leader: LeaderRecord | None = None
    grants: tuple[GrantRecord, ...] = ()
    reply: ReplyRecord | None = None
    handled_request: HandledRequest | None = None

    def __post_init__(self):
        _uuid(self.record_id)
        if (type(self.profile) is not ProtocolProfile
                or self.leader is not None and type(self.leader) is not LeaderRecord
                or type(self.grants) is not tuple or len(self.grants) > 2
                or any(type(g) is not GrantRecord for g in self.grants)
                or self.reply is not None and type(self.reply) is not ReplyRecord
                or self.handled_request is not None and type(self.handled_request) is not HandledRequest
                or self.grants and self.leader is None):
            raise RecordError()
        for g in self.grants:
            if (g.issuer_term_id != self.leader.term_id or g.clock_id != self.leader.clock_id
                    or self.leader.leader_until < g.valid_until + self.profile.delta_seconds):
                raise RecordError()
        if len(self.grants) == 2:
            a, b = self.grants
            guard = self.profile.delta_cmd_seconds * clock_factor(self.profile.delta_cmd_seconds)
            if a.grant_id == b.grant_id or b.valid_from < a.valid_until + guard:
                raise RecordError()


@dataclass(frozen=True)
class R2Record:
    profile_id: str
    request: RequestRecord | None = None

    def __post_init__(self):
        _uuid(self.profile_id)
        if self.request is not None and type(self.request) is not RequestRecord:
            raise RecordError()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecordError()
        result[key] = value
    return result


def _load(raw, keys):
    try:
        data = json.loads(raw, object_pairs_hook=_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(RecordError()))
    except (ValueError, TypeError, RecursionError):
        raise RecordError() from None
    if (not isinstance(data, dict) or data.keys() != set(keys)
            or type(data['v']) is not int or data['v'] != 2):
        raise RecordError()
    return data


def _record(cls, data):
    if not isinstance(data, dict) or data.keys() != set(cls.__dataclass_fields__):
        raise RecordError()
    try:
        return cls(**data)
    except TypeError:
        raise RecordError() from None


def _dump(record):
    return json.dumps({'v': 2, **asdict(record)}, ensure_ascii=False,
                      allow_nan=False, separators=(',', ':'))


_SUFFIX = ' [zc:v2 управление занято '


def original_name(raw):
    if not isinstance(raw, str):
        raise RecordError()
    prefix, marker, _ = raw.rpartition(_SUFFIX)
    if marker:
        decode_r1(raw, prefix)
        return prefix
    if '[zc:' in raw:
        raise RecordError()
    return raw


def encode_r1(name, record):
    if not isinstance(name, str) or type(record) is not R1Record:
        raise RecordError()
    return name + _SUFFIX + _dump(record) + ']'


def decode_r1(raw, name):
    if raw == name:
        return None
    prefix = name + _SUFFIX
    if not isinstance(raw, str) or not raw.startswith(prefix) or not raw.endswith(']'):
        raise RecordError()
    data = _load(raw[len(prefix):-1], ('v', *R1Record.__dataclass_fields__))
    if not isinstance(data['grants'], list):
        raise RecordError()
    return R1Record(_record(ProtocolProfile, data['profile']), data['record_id'],
                    _record(LeaderRecord, data['leader']) if data['leader'] is not None else None,
                    tuple(_record(GrantRecord, g) for g in data['grants']),
                    _record(ReplyRecord, data['reply']) if data['reply'] is not None else None,
                    _record(HandledRequest, data['handled_request'])
                    if data['handled_request'] is not None else None)


def encode_r2(record):
    if type(record) is not R2Record:
        raise RecordError()
    return _dump(record)


def decode_r2(raw):
    if raw is None or raw == '':
        return None
    data = _load(raw, ('v', 'profile_id', 'request'))
    return R2Record(data['profile_id'], _record(RequestRecord, data['request'])
                    if data['request'] is not None else None)
