"""Strict version-1 relative-time wire records for notes/R1 and name/R2.

Local clock instants never enter these records. Unknown or malformed metadata
raises RecordError rather than implying FREE. This codec provides no CAS,
leader election or proof of the accepted two-register protocol's safety.
"""

from dataclasses import asdict, dataclass
import json
import math
from uuid import UUID


class RecordError(ValueError):
    def __init__(self):
        super().__init__("Invalid or incompatible coordination record")


def _uuid(value):
    if not isinstance(value, str):
        raise RecordError()
    try:
        if str(UUID(value)) != value:
            raise RecordError()
    except ValueError:
        raise RecordError() from None


def _token(value):
    if (not isinstance(value, str) or len(value) != 32
            or any(char not in "0123456789abcdef" for char in value)):
        raise RecordError()


def _counter(value):
    if type(value) is not int or value < 0:
        raise RecordError()


def _positive(value):
    if type(value) not in (int, float):
        raise RecordError()
    try:
        if not math.isfinite(value) or value <= 0:
            raise RecordError()
    except OverflowError:
        raise RecordError() from None


def _lease(lease_s, clock_rho):
    _positive(lease_s)
    if type(clock_rho) not in (int, float) or clock_rho not in (.1, .05, .01, .005):
        raise RecordError()
    period = lease_s / ((1 + clock_rho) / (1 - clock_rho))
    # Undo floating-point rounding at exact policy boundaries, not a new band
    # chosen from the already increased lease_s.
    for boundary in (60, 300, 600):
        if math.isclose(period, boundary, rel_tol=1e-12):
            period = boundary
    expected = .1 if period <= 60 else .05 if period <= 300 else .01 if period <= 600 else .005
    if clock_rho != expected:
        raise RecordError()


@dataclass(frozen=True)
class LeaderRecord:
    owner: str
    token: str
    seq: int
    lease_s: float
    clock_rho: float

    def __post_init__(self):
        _uuid(self.owner)
        _token(self.token)
        _counter(self.seq)
        _lease(self.lease_s, self.clock_rho)
        if self.lease_s > 1200:
            raise RecordError()


@dataclass(frozen=True)
class RequestRecord:
    owner: str
    request_id: str
    nonce: str
    lease_seconds: float
    state: str
    grant_id: str | None = None
    grant_revision: int | None = None

    def __post_init__(self):
        for value in (self.owner, self.request_id, self.nonce):
            _uuid(value)
        _positive(self.lease_seconds)
        if self.state not in ("request", "heartbeat", "finished"):
            raise RecordError()
        if self.state == "request":
            if self.grant_id is not None or self.grant_revision is not None:
                raise RecordError()
        else:
            _uuid(self.grant_id)
            _counter(self.grant_revision)


@dataclass(frozen=True)
class R1Record:
    leader: LeaderRecord | None = None
    request: RequestRecord | None = None

    def __post_init__(self):
        if (self.leader is not None and type(self.leader) is not LeaderRecord
                or self.request is not None and type(self.request) is not RequestRecord):
            raise RecordError()


@dataclass(frozen=True)
class GrantRecord:
    owner: str
    request_id: str
    grant_id: str
    leader_token: str
    revision: int
    lease_s: float
    clock_rho: float
    nonce: str

    def __post_init__(self):
        for value in (self.owner, self.request_id, self.grant_id, self.nonce):
            _uuid(value)
        _token(self.leader_token)
        _counter(self.revision)
        _lease(self.lease_s, self.clock_rho)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecordError()
        result[key] = value
    return result


def _invalid_constant(value):
    raise RecordError()


def _load(raw, keys):
    if not isinstance(raw, str):
        raise RecordError()
    try:
        data = json.loads(raw, object_pairs_hook=_object, parse_constant=_invalid_constant)
    except (ValueError, RecursionError):
        raise RecordError() from None
    if (not isinstance(data, dict) or data.keys() != set(keys)
            or type(data["v"]) is not int or data["v"] != 1):
        raise RecordError()
    return data


def _dump(data):
    try:
        return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError):
        raise RecordError() from None


def _record(cls, data):
    if not isinstance(data, dict):
        raise RecordError()
    try:
        return cls(**data)
    except TypeError:
        raise RecordError() from None


def encode_r1(record: R1Record) -> str:
    if type(record) is not R1Record:
        raise RecordError()
    if record == R1Record():
        return ""
    return _dump({"v": 1, **asdict(record)})


def decode_r1(raw: str | None) -> R1Record:
    if raw is None or raw == "":
        return R1Record()
    data = _load(raw, ("v", "leader", "request"))
    return R1Record(_record(LeaderRecord, data["leader"]) if data["leader"] is not None else None,
                    _record(RequestRecord, data["request"]) if data["request"] is not None else None)


_SUFFIX = " [zc:v1 управление занято "


def encode_r2(original_name: str, grant: GrantRecord | None) -> str:
    if not isinstance(original_name, str) or grant is not None and type(grant) is not GrantRecord:
        raise RecordError()
    if grant is None:
        return original_name
    return original_name + _SUFFIX + _dump({"v": 1, **asdict(grant)}) + "]"


def decode_r2(raw: str, original_name: str) -> GrantRecord | None:
    if not isinstance(raw, str) or not isinstance(original_name, str):
        raise RecordError()
    if raw == original_name:
        return None
    prefix = original_name + _SUFFIX
    if not raw.startswith(prefix) or not raw.endswith("]"):
        raise RecordError()
    data = _load(raw[len(prefix):-1], ("v", *GrantRecord.__dataclass_fields__))
    return _record(GrantRecord, {key: value for key, value in data.items() if key != "v"})
