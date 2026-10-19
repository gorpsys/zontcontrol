"""Two-slot v2 scheduler and causal executor-local grant admission."""

from dataclasses import dataclass, replace
from threading import RLock
from uuid import UUID, uuid4

from ..control_types import (CommandOutcomeUnknown, ControllerError, ControlOwnershipRequired,
                            ControlSession, DeviceControlBusy, InvalidArguments)
from .clock import BootClock, ClockSample, GrantInterval, _duration, clock_factor, clock_rho
from .models import GrantRecord, HandledRequest, RecordError, ReplyRecord, RequestRecord
from .statistics import error_event, observe
from .timing import OperationWindow, TimingViolation


def _key(request):
    return request.owner_id, request.request_id, request.nonce, request.leader_term_id


class GrantCoordinator:
    """All intra-process interaction with executors still goes through R1/R2."""

    def __init__(self, store, leader, *, clock=None, window=None, statistics=None,
                 uuid_factory=None):
        self._store, self._leader = store, leader
        self._clock = BootClock() if clock is None else clock
        self.window = _duration(leader.timing.delta if window is None else window, positive=True)
        self._statistics = statistics
        self._uuid_factory = uuid4 if uuid_factory is None else uuid_factory
        self._term = None
        self._results = {}

    def _observe(self, event, grant=None, **fields):
        context = {'device_id': self._store.device_id}
        if grant is not None:
            context.update(episode_id=grant.request_id, request_id=grant.request_id,
                           run_id=grant.owner_id, grant_id=grant.grant_id,
                           revision=grant.revision, leader_term=grant.issuer_term_id)
        observe(self._statistics, event, **context, **fields)

    @staticmethod
    def _matches(request, grant):
        return (request.owner_id == grant.owner_id and request.request_id == grant.request_id
                and request.grant_id == grant.grant_id
                and request.grant_revision == grant.revision
                and request.leader_term_id == grant.issuer_term_id)

    def step(self, *, accepting=True):
        # Keep both protocol phases serialized with renewal and shutdown.
        with self._leader._lock:
            if self._leader.record is None:
                self._leader.confirm(self.window)
            if self._term != self._leader.record.term_id:
                self._results.clear()
                self._term = self._leader.record.term_id

            def inspect(record, window):
                r2 = self._store.read_r2(window=window)
                request = r2.request if (r2 is not None
                    and r2.profile_id == self._store.profile.profile_id) else None
                now = self._leader.timeline.now()
                guard = self._leader.timing.command_guard
                grants = tuple(g for g in record.grants if now < g.valid_until + guard)
                return request, grants

            request, grants = self._leader.coordinate(inspect, self.window)
            if request is None or request.leader_term_id != self._term:
                if grants != self._leader.confirm(self.window).grants:
                    self._publish_slots(grants)
                return grants[0] if grants else None

            key = _key(request)
            old = self._results.get(key)
            if old is not None:
                # Never reconstruct an interval or sample for a handled nonce.
                record = self._leader.confirm(self.window)
                if isinstance(old, GrantRecord) and self._leader.timeline.now() >= old.valid_until:
                    reply = ReplyRecord(request.owner_id, request.request_id, request.nonce, 'expired')
                    marker = HandledRequest(request.owner_id, request.request_id, request.nonce,
                                            self._term, request.operation, old.grant_id, 'expired')
                    if record.grants != grants or record.reply != reply or record.handled_request != marker:
                        def expired(record, window):
                            self._leader.publish(replace(record, record_id=str(self._uuid_factory()),
                                grants=grants, reply=reply, handled_request=marker), window)
                        self._leader.coordinate(expired, self.window)
                elif grants != record.grants:
                    self._publish_slots(grants)
                return old if isinstance(old, GrantRecord) and (
                    self._leader.timeline.now() < old.valid_until) else None
            record = self._leader.confirm(self.window)
            marker = record.handled_request
            if marker is not None and (marker.owner_id, marker.request_id, marker.nonce,
                                       marker.term_id) == key:
                return next((g for g in grants if g.nonce == request.nonce), None)

            if request.operation in ('finish', 'cancel'):
                current = next((g for g in grants if self._matches(request, g)), None)
                if current is not None:
                    self._handled(request, grants, current.grant_id,
                                  'finished' if request.operation == 'finish' else 'cancelled')
                    self._results[key] = request.operation
                return current
            if not accepting:
                return grants[0] if grants else None
            renewing = request.operation == 'heartbeat'
            current = next((g for g in grants if self._matches(request, g)), None) if renewing else None
            if (renewing and (current is None or self._leader.timeline.now() >= current.valid_until)
                    or not renewing and len(grants) >= 2):
                return None

            # Mandatory separate, readback-confirmed renewal; a later grant has
            # its own independent READ-to-WRITE window and guard.
            self._leader.renew()

            def issue(record, window):
                now = self._leader.timeline.now()
                guard = self._leader.timing.command_guard
                live = tuple(g for g in record.grants if now < g.valid_until + guard)
                # Allocate identities before sampling, so factory pauses count.
                grant_id = current.grant_id if renewing else str(self._uuid_factory())
                record_id = str(self._uuid_factory())
                tau = self._leader.timeline.now()
                until = tau + request.lease_seconds * clock_factor(request.lease_seconds)
                start = max(tau, self._leader.quarantine_until)
                if renewing:
                    position = next((i for i, g in enumerate(live)
                                     if self._matches(request, g)), None)
                    if position is None or tau >= live[position].valid_until:
                        return None
                    if position:
                        start = max(start, live[position - 1].valid_until + guard)
                    if position + 1 < len(live):
                        until = min(until, live[position + 1].valid_from - guard)
                else:
                    if len(live) >= 2:
                        return None
                    if live:
                        start = max(start, live[-1].valid_until + guard)
                if (start >= until or record.leader.leader_until < until + self._leader.timing.delta):
                    reply = ReplyRecord(request.owner_id, request.request_id, request.nonce, 'horizon')
                    self._leader.publish(replace(record, record_id=record_id,
                                                 grants=live, reply=reply), window)
                    return None
                grant = GrantRecord(request.owner_id, request.request_id, grant_id,
                    request.lease_seconds, clock_rho(request.lease_seconds), self._term,
                    str(self._leader.timeline.clock_id), current.revision + 1 if renewing else 0,
                    request.nonce, tau, start, until)
                slots = tuple(grant if g.grant_id == grant_id else g for g in live) if renewing else (*live, grant)
                handled = HandledRequest(request.owner_id, request.request_id, request.nonce,
                    self._term, request.operation, grant_id, 'issued')
                self._leader.publish(replace(record, record_id=record_id, grants=slots,
                                             reply=None, handled_request=handled), window)
                return grant

            grant = self._leader.coordinate(issue, self.window)
            if grant is not None:
                self._results[key] = grant
                self._observe('heartbeat' if renewing else 'grant', grant, outcome='success')
            return grant

    def _publish_slots(self, grants):
        def publish(record, window):
            self._leader.publish(replace(record, record_id=str(self._uuid_factory()),
                                         grants=grants), window)
        self._leader.coordinate(publish, self.window)
        self._observe('cleanup', phase='metadata', outcome='success')

    def _handled(self, request, grants, grant_id, outcome):
        def publish(record, window):
            marker = HandledRequest(request.owner_id, request.request_id, request.nonce,
                                    self._term, request.operation, grant_id, outcome)
            self._leader.publish(replace(record, record_id=str(self._uuid_factory()),
                                         grants=grants, handled_request=marker), window)
        self._leader.coordinate(publish, self.window)


@dataclass
class _Pending:
    request: RequestRecord
    sent: float
    local_deadline: float
    attempt_deadline: float | None
    request_id: UUID | None
    uncertain: bool = False
    grant: GrantRecord | None = None
    interval: GrantInterval | None = None
    last_publish: float = 0


@dataclass
class _Held:
    grant: GrantRecord
    session: ControlSession
    interval: GrantInterval
    attempt_deadline: float | None = None
    finished: bool = False
    renewal: _Pending | None = None
    release_attempted: bool = False


class ExecutorGate:
    def __init__(self, stores, *, clock=None, delta, statistics=None, uuid_factory=None,
                 retry_seconds=0):
        self._stores = dict(stores)
        self._clock = BootClock() if clock is None else clock
        self._delta = _duration(delta, positive=True)
        self._retry_seconds = _duration(retry_seconds)
        self._statistics = statistics
        self._uuid_factory = uuid4 if uuid_factory is None else uuid_factory
        self._locks = {device: RLock() for device in self._stores}
        self._pending, self._held = {}, {}

    def _device(self, device_id):
        if type(device_id) is not int or device_id not in self._stores:
            raise InvalidArguments()
        return self._stores[device_id], self._locks[device_id]

    @staticmethod
    def _arguments(owner, lease_seconds):
        if not isinstance(owner, UUID) or not owner.int:
            raise InvalidArguments()
        try:
            clock_rho(lease_seconds)
        except (ValueError, OverflowError):
            raise InvalidArguments() from None

    def _check(self, *deadlines):
        if any(d is not None and self._clock.now() >= d for d in deadlines):
            raise ControlOwnershipRequired()

    def _observe(self, event, store, request=None, **fields):
        context = {'device_id': store.device_id}
        if request is not None:
            context.update(episode_id=request.request_id, request_id=request.request_id,
                           run_id=request.owner_id)
        observe(self._statistics, event, **context, **fields)

    def _read(self, store):
        window = OperationWindow(self._clock, self._delta)
        try:
            record = store.read_r1(window=window)
        except (ControllerError, RecordError) as error:
            self._observe(error_event(error), store, outcome='unknown')
            raise
        except TimingViolation:
            self._observe('delta_breach', store, delta_s=self._delta,
                          elapsed_s=self._clock.now()-window.started_at)
            raise ControlOwnershipRequired() from None
        self._observe('rtt', store, rtt_s=self._clock.now()-window.started_at)
        return record

    def _publish(self, store, pending):
        self._check(pending.local_deadline, pending.attempt_deadline)
        window = OperationWindow(self._clock, self._delta)
        try:
            store.publish_request(pending.request, window=window)
        except (CommandOutcomeUnknown, TimingViolation):
            pending.uncertain = True
            self._observe('timeout', store, pending.request, outcome='unknown')
            raise CommandOutcomeUnknown() from None
        except (ControllerError, RecordError) as error:
            self._observe(error_event(error), store, pending.request, outcome='failure')
            raise
        pending.last_publish = self._clock.now()
        self._check(pending.local_deadline, pending.attempt_deadline)
        event = {'acquire': 'request', 'finish': 'finished'}.get(
            pending.request.operation, pending.request.operation)
        self._observe(event, store, pending.request, outcome='success')

    def _ack(self, store, pending):
        self._check(pending.local_deadline, pending.attempt_deadline)
        if pending.grant is not None:
            return None
        record = self._read(store)
        self._check(pending.local_deadline, pending.attempt_deadline)
        if pending.grant is None and record is not None and record.profile == store.profile:
            req = pending.request
            grant = next((g for g in record.grants if
                (g.owner_id, g.request_id, g.nonce, g.issuer_term_id) == _key(req)
                and g.lease_seconds == req.lease_seconds
                and (req.operation != 'acquire' or g.revision == 0)
                and (req.operation != 'heartbeat' or
                     g.grant_id == req.grant_id and g.revision == req.grant_revision + 1)), None)
            if grant is not None:
                try:
                    sample = ClockSample(UUID(grant.clock_id), UUID(grant.nonce), grant.sampled_at,
                        pending.sent, self._clock.now(), req.lease_seconds, grant.clock_rho)
                    interval = sample.project(valid_from=grant.valid_from,
                        valid_until=grant.valid_until, local_lease_deadline=pending.local_deadline)
                except (ValueError, TimingViolation):
                    return record
                pending.grant, pending.interval = grant, interval
        return record

    def _session(self, store, pending):
        if pending.grant is None or not pending.interval.contains(self._clock.now()):
            return None
        session = ControlSession(store.device_id, self._uuid_factory(), pending.interval.expires_at)
        self._observe('grant_ack', store, pending.request,
            grant_id=pending.grant.grant_id, leader_term=pending.grant.issuer_term_id,
            revision=pending.grant.revision, wait_s=self._clock.now() - pending.sent)
        self._check(pending.local_deadline, pending.attempt_deadline, session.expires_at)
        return _Held(pending.grant, session, pending.interval, pending.attempt_deadline)

    def try_acquire(self, owner, device_id, *, lease_seconds, request_id=None,
                    local_deadline=None, attempt_deadline=None):
        self._arguments(owner, lease_seconds)
        if request_id is not None and (not isinstance(request_id, UUID) or not request_id.int):
            raise InvalidArguments()
        store, lock = self._device(device_id)
        with lock:
            self._check(local_deadline, attempt_deadline)
            key = owner, device_id
            now = self._clock.now()
            for k, held in tuple(self._held.items()):
                if k[1] == device_id and now >= held.session.expires_at:
                    del self._held[k]
            held = self._held.get(key)
            if held is not None and not held.finished:
                if request_id is not None and held.grant.request_id != str(request_id):
                    raise DeviceControlBusy()
                self._check(held.attempt_deadline if request_id is not None else None)
                return held.session
            pending = self._pending.get(key)
            if pending is not None and request_id is None and now >= pending.local_deadline:
                del self._pending[key]
                pending = None
            if pending is None or pending.request_id != request_id:
                record = self._read(store)
                term = record.leader.term_id if record is not None and record.leader is not None and (
                    record.profile == store.profile) else None
                request = RequestRecord(str(owner), str(request_id or self._uuid_factory()),
                    str(self._uuid_factory()), term, 'acquire', lease_seconds)
                pending = _Pending(request, self._clock.now(),
                    min(now + lease_seconds, local_deadline) if local_deadline is not None else now + lease_seconds,
                    attempt_deadline, request_id)
                self._pending[key] = pending
                self._observe('episode_started', store, request)
                if not (store.previous_profile is not None and (
                        record is None or record.profile != store.profile)):
                    self._publish(store, pending)
                raise DeviceControlBusy()
            self._check(pending.local_deadline, pending.attempt_deadline)
            record = self._ack(store, pending)
            held = self._session(store, pending)
            if held is not None:
                self._held[key] = held
                del self._pending[key]
                return held.session
            if pending.grant is not None:
                raise DeviceControlBusy()
            if store.previous_profile is not None and (record is None or record.profile != store.profile):
                raise DeviceControlBusy()
            term = record.leader.term_id if record is not None and record.leader is not None and (
                record.profile == store.profile) else None
            if term != pending.request.leader_term_id:
                pending.request = replace(pending.request, nonce=str(self._uuid_factory()), leader_term_id=term)
                pending.sent, pending.uncertain = self._clock.now(), False
                self._publish(store, pending)
            elif not pending.uncertain and self._clock.now() >= pending.last_publish + self._retry_seconds:
                r2 = store.read_r2(window=OperationWindow(self._clock, self._delta))
                if r2 is None or r2.request != pending.request:
                    self._observe('request_lost', store, pending.request)
                    self._observe('request_repeated', store, pending.request)
                    self._publish(store, pending)
            raise DeviceControlBusy()

    def abandon_request(self, owner, device_id, request_id):
        _, lock = self._device(device_id)
        with lock:
            pending = self._pending.get((owner, device_id))
            if pending is not None and pending.request.request_id == str(request_id):
                del self._pending[(owner, device_id)]

    def _match(self, owner, session, *, allow_finished=False):
        if not isinstance(session, ControlSession):
            raise ControlOwnershipRequired()
        held = self._held.get((owner, session.device_id))
        if held is None or held.session is not session or held.finished and not allow_finished:
            raise ControlOwnershipRequired()
        return held

    def observation_context(self, owner, session):
        _, lock = self._device(session.device_id)
        with lock:
            grant = self._match(owner, session, allow_finished=True).grant
            return dict(episode_id=grant.request_id, request_id=grant.request_id,
                run_id=grant.owner_id, grant_id=grant.grant_id, revision=grant.revision,
                leader_term=grant.issuer_term_id)

    def validate(self, owner, session):
        if not isinstance(session, ControlSession):
            raise ControlOwnershipRequired()
        _, lock = self._device(session.device_id)
        with lock:
            held = self._match(owner, session)
            if not held.interval.contains(self._clock.now()):
                raise ControlOwnershipRequired()

    def renew(self, owner, session, *, lease_seconds):
        self._arguments(owner, lease_seconds)
        if not isinstance(session, ControlSession):
            raise ControlOwnershipRequired()
        store, lock = self._device(session.device_id)
        with lock:
            self.validate(owner, session)
            held = self._match(owner, session)
            pending = held.renewal
            if pending is None:
                g = held.grant
                request = RequestRecord(str(owner), g.request_id, str(self._uuid_factory()),
                    g.issuer_term_id, 'heartbeat', lease_seconds, g.grant_id, g.revision)
                now = self._clock.now()
                pending = _Pending(request, now, now + lease_seconds, None, None)
                held.renewal = pending
                self._publish(store, pending)
            else:
                self._ack(store, pending)
                updated = self._session(store, pending)
                if updated is not None:
                    self._held[(owner, session.device_id)] = updated
                    return updated.session
            raise DeviceControlBusy()

    def release(self, owner, session):
        if not isinstance(session, ControlSession):
            raise ControlOwnershipRequired()
        store, lock = self._device(session.device_id)
        with lock:
            held = self._match(owner, session, allow_finished=True)
            held.finished = True
            if held.release_attempted:
                return
            held.release_attempted = True
            g = held.grant
            request = RequestRecord(str(owner), g.request_id, str(self._uuid_factory()), g.issuer_term_id,
                                    'finish', g.lease_seconds, g.grant_id, g.revision)
            # Unknown completion is never retried. Its immutable reservation is
            # unaffected by a lost finish. No network is needed after expiry.
            if self._clock.now() < session.expires_at:
                record = self._read(store)
                if record is not None and g in record.grants:
                    self._publish(store, _Pending(request, self._clock.now(),
                        session.expires_at, None, None))
            self._observe('episode_finished', store, request, outcome='unknown', coverage='partial')
