"""Relative executor grants in the accepted, conditional R1/R2 protocol.

Calls perform one bounded attempt. Callers schedule explicit retries and polls;
there is no background queue or automatic retry of an uncertain write. R1 RMW
can replay stale leadership: these checks provide neither CAS nor fencing.
"""

from dataclasses import dataclass
import math
from threading import RLock
from uuid import UUID, uuid4

from ..controller.contracts import (
    CommandOutcomeUnknown, ControllerError, ControlOwnershipRequired,
    ControlSession, DeviceControlBusy, InvalidArguments,
)
from .clock import BootClock, _duration, clock_factor, clock_rho
from .models import GrantRecord, R1Record, RecordError, RequestRecord
from .statistics import error_event, observe


class GrantCoordinator:
    """One leader's grant polling; independent of algorithm ownership/lifecycle."""

    def __init__(self, store, leader, *, clock=None, window=None, statistics=None):
        self._store = store
        self._leader = leader
        self._clock = clock if clock is not None else BootClock()
        self.window = _duration(leader.timing.delta if window is None else window, positive=True)
        self._observation = None
        self._statistics = statistics
        self._changed_grant = None

    def _observe(self, event, grant=None, **fields):
        context = {'device_id': self._store.device_id}
        if grant is not None:
            context.update(episode_id=grant.request_id, request_id=grant.request_id,
                           run_id=grant.owner, grant_id=grant.grant_id,
                           revision=grant.revision, leader_term=grant.leader_token)
        observe(self._statistics, event, **{**context, **fields})

    @staticmethod
    def _matches(request, grant):
        return (request.owner == grant.owner and request.request_id == grant.request_id
                and request.grant_id == grant.grant_id
                and request.grant_revision == grant.revision)

    def step(self, *, accepting=True):
        """Confirm R1 before R2 work; never replace an unexpired foreign grant.

        accepting=False only settles completion/expiry during host shutdown;
        it cannot grant a pending request or renew a heartbeat.

        The full window includes R1 confirmation, R2 reads and any write/readback.
        Expiry observation begins at the first R2 response and ends at the send
        of a final read of exactly the same full value. No remote clocks enter it.
        """
        def coordinate(record):
            sent = self._clock.now()
            current = self._store.read_r2()
            received = self._clock.now()
            self._observe('poll')
            if current is not None and current.leader_token != record.leader.token:
                identity = current.grant_id, current.revision, record.leader.token
                if identity != self._changed_grant:
                    self._observe('leader_changed_active_grant', current)
                    self._changed_grant = identity
            expired = False
            if current is None:
                self._observation = None
            elif self._observation is None or self._observation[0] != current:
                self._observation = (current, received)
            else:
                expired = sent - self._observation[1] >= current.lease_s
            request = record.request
            desired = current
            if (current is not None and request is not None
                    and request.state == "finished" and self._matches(request, current)
                    and request.nonce == current.nonce):
                desired = None
            elif (accepting and current is not None and request is not None
                    and request.state == "heartbeat" and self._matches(request, current)
                    and current.leader_token == record.leader.token
                    and request.nonce != current.nonce and not expired):
                desired = GrantRecord(current.owner, current.request_id, current.grant_id,
                                      record.leader.token, current.revision + 1,
                                      request.lease_seconds * clock_factor(request.lease_seconds),
                                      clock_rho(request.lease_seconds), request.nonce)
            elif current is None or expired:
                desired = None
                if accepting and request is not None and request.state == "request":
                    # An expired request/nonce must not resurrect its old grant.
                    if current is None or (request.owner, request.request_id) != (
                            current.owner, current.request_id):
                        desired = GrantRecord(request.owner, request.request_id, str(uuid4()),
                                              record.leader.token, 0,
                                              request.lease_seconds * clock_factor(request.lease_seconds),
                                              clock_rho(request.lease_seconds), request.nonce)
            if desired != current:
                if (expired and request is not None and current is not None
                        and (request.owner, request.request_id) ==
                        (current.owner, current.request_id)):
                    # Consume this request before clearing R2. An uncertain R1
                    # write stops the role; no blind retry or R2 compensation.
                    self._store.write_r1(R1Record(record.leader))
                self._store.write_r2(desired)
                self._observation = None
                self._observe('cleanup' if desired is None else
                              'heartbeat' if current is not None and
                              desired.grant_id == current.grant_id else 'grant',
                              current if desired is None else desired, outcome='success',
                              **({'phase': 'metadata'} if desired is None else {}))
            return desired

        return self._leader.coordinate(coordinate, self.window)


@dataclass
class _Pending:
    request: RequestRecord
    sent: float
    uncertain: bool = False


@dataclass
class _Held:
    grant: GrantRecord
    session: ControlSession
    finished: bool = False
    renewal: _Pending | None = None
    observed_finished: bool = False


class ExecutorGate:
    """Internal DI gate over device-specific, read-only-R2 executor stores.

    A pending request reserves no local device and confers no right. The caller
    must repeat try_acquire explicitly. Renew is an internal protocol operation:
    it returns a replacement immutable session only after the new nonce is acked.
    Ordinary reacquisition returns the original session without extending it.
    """

    def __init__(self, stores, *, clock=None, delta, statistics=None):
        self._stores = dict(stores)
        self._clock = clock if clock is not None else BootClock()
        self._delta = _duration(delta, positive=True)
        self._locks = {device: RLock() for device in self._stores}
        self._pending = {}
        self._held = {}
        self._statistics = statistics

    def _observe(self, event, store, request=None, **fields):
        context = {'device_id': store.device_id}
        if request is not None:
            context.update(episode_id=request.request_id, request_id=request.request_id,
                           run_id=request.owner)
            if request.grant_id is not None:
                context['grant_id'] = request.grant_id
        observe(self._statistics, event, **{**context, **fields})

    def _device(self, device_id):
        if type(device_id) is not int or device_id not in self._stores:
            raise InvalidArguments()
        return self._stores[device_id], self._locks[device_id]

    @staticmethod
    def _arguments(owner, lease_seconds):
        if not isinstance(owner, UUID) or owner.int == 0:
            raise InvalidArguments()
        try:
            clock_rho(lease_seconds)
        except (ValueError, OverflowError):
            raise InvalidArguments() from None

    def _read(self, store):
        start = self._clock.now()
        try:
            grant = store.read_r2()
            record = store.read_r1()
        except (ControllerError, RecordError) as error:
            self._observe(error_event(error), store, outcome='unknown')
            raise
        self._observe('rtt', store, rtt_s=self._clock.now() - start)
        if self._clock.now() - start > self._delta:
            self._observe('delta_breach', store, elapsed_s=self._clock.now() - start,
                          delta_s=self._delta)
            raise ControlOwnershipRequired()
        return grant, record.leader

    def _ack(self, store, pending):
        grant, leader = self._read(store)
        request = pending.request
        if (grant is None or leader is None or grant.owner != request.owner
                or grant.request_id != request.request_id or grant.nonce != request.nonce
                or grant.leader_token != leader.token
                or grant.clock_rho != clock_rho(request.lease_seconds)
                or not math.isclose(grant.lease_s, request.lease_seconds * clock_factor(
                    request.lease_seconds), rel_tol=1e-12)):
            return None
        if request.state == "heartbeat" and (grant.grant_id != request.grant_id
                or grant.revision != request.grant_revision + 1):
            return None
        if request.state == "request" and grant.revision != 0:
            return None
        factor = (1 + grant.clock_rho) / (1 - grant.clock_rho)
        deadline = pending.sent + grant.lease_s / factor
        if self._clock.now() >= deadline:
            return None
        return grant, deadline

    def _publish(self, store, pending):
        start = self._clock.now()
        try:
            store.publish_request(pending.request)
        except CommandOutcomeUnknown:
            pending.uncertain = True
            self._observe('timeout', store, pending.request, outcome='unknown')
            raise
        except (ControllerError, RecordError) as error:
            self._observe(error_event(error), store, pending.request, outcome='failure')
            raise
        self._observe('rtt', store, pending.request, rtt_s=self._clock.now() - start)
        if self._clock.now() - start > self._delta:
            pending.uncertain = True
            self._observe('delta_breach', store, pending.request,
                          elapsed_s=self._clock.now() - start, delta_s=self._delta)
            self._observe('timeout', store, pending.request, outcome='unknown')
            raise CommandOutcomeUnknown()
        self._observe(pending.request.state, store, pending.request, outcome='success')

    def try_acquire(self, owner, device_id, *, lease_seconds):
        self._arguments(owner, lease_seconds)
        store, lock = self._device(device_id)
        with lock:
            now = self._clock.now()
            for key, pending in tuple(self._pending.items()):
                if key[1] == device_id and now >= pending.sent + pending.request.lease_seconds:
                    del self._pending[key]
            for key, held in tuple(self._held.items()):
                if key[1] == device_id and now >= held.session.expires_at:
                    del self._held[key]
            self._observe('attempt', store)
            key = owner, device_id
            held = self._held.get(key)
            if held is not None and not held.finished:
                if self._clock.now() >= held.session.expires_at:
                    raise ControlOwnershipRequired()
                return held.session
            pending = self._pending.get(key)
            if pending is None or self._clock.now() >= pending.sent + pending.request.lease_seconds:
                pending = _Pending(RequestRecord(str(owner), str(uuid4()), str(uuid4()),
                                                 lease_seconds, "request"), self._clock.now())
                self._pending[key] = pending
                self._observe('episode_started', store, pending.request)
                self._publish(store, pending)
            else:
                ack = self._ack(store, pending)
                if ack is not None:
                    grant, deadline = ack
                    session = ControlSession(device_id, uuid4(), deadline)
                    self._held[key] = _Held(grant, session)
                    del self._pending[key]
                    self._observe('grant_ack', store, pending.request,
                                  grant_id=grant.grant_id, leader_term=grant.leader_token,
                                  revision=grant.revision, wait_s=self._clock.now() - pending.sent)
                    return session
                # Lost requests are repeated explicitly, with the original nonce
                # timestamp. An unknown send is only polled, never blindly resent.
                if not pending.uncertain and store.read_r1().request != pending.request:
                    self._observe('request_lost', store, pending.request)
                    self._observe('request_repeated', store, pending.request)
                    self._publish(store, pending)
            raise DeviceControlBusy()

    def _match(self, owner, session, *, allow_finished=False):
        if not isinstance(session, ControlSession):
            raise ControlOwnershipRequired()
        held = self._held.get((owner, session.device_id))
        if (held is None or held.session is not session
                or held.finished and not allow_finished):
            raise ControlOwnershipRequired()
        return held

    def observation_context(self, owner, session):
        _, lock = self._device(session.device_id)
        with lock:
            grant = self._match(owner, session, allow_finished=True).grant
            return dict(episode_id=grant.request_id, request_id=grant.request_id,
                        run_id=grant.owner, grant_id=grant.grant_id,
                        revision=grant.revision, leader_term=grant.leader_token)

    def validate(self, owner, session):
        if not isinstance(session, ControlSession):
            raise ControlOwnershipRequired()
        store, lock = self._device(session.device_id)
        with lock:
            held = self._match(owner, session)
            try:
                grant, leader = self._read(store)
            except (ControllerError, RecordError):
                raise ControlOwnershipRequired() from None
            if (grant != held.grant or leader is None or grant.leader_token != leader.token
                    or not self._clock.now() < session.expires_at):
                raise ControlOwnershipRequired()

    def renew(self, owner, session, *, lease_seconds):
        """Publish once, poll explicitly; an old ack cannot extend the deadline."""
        self._arguments(owner, lease_seconds)
        if not isinstance(session, ControlSession):
            raise ControlOwnershipRequired()
        store, lock = self._device(session.device_id)
        with lock:
            held = self._match(owner, session)
            pending = held.renewal
            if pending is None:
                self.validate(owner, session)
                grant = held.grant
                pending = _Pending(RequestRecord(str(owner), grant.request_id, str(uuid4()),
                                                 lease_seconds, "heartbeat", grant.grant_id,
                                                 grant.revision), self._clock.now())
                held.renewal = pending
                self._publish(store, pending)
            else:
                ack = self._ack(store, pending)
                if ack is not None:
                    held.grant, deadline = ack
                    held.session = ControlSession(session.device_id, uuid4(), deadline)
                    held.renewal = None
                    return held.session
                if not pending.uncertain and store.read_r1().request != pending.request:
                    self._observe('request_lost', store, pending.request)
                    self._observe('request_repeated', store, pending.request)
                    self._publish(store, pending)
            raise DeviceControlBusy()

    def release(self, owner, session):
        """Completion may be explicitly republished after a lost R1 update.

        No current grant is required to announce completion of this exact grant.
        This does not authorize any heating command, including cleanup/off.
        """
        if not isinstance(session, ControlSession):
            raise ControlOwnershipRequired()
        store, lock = self._device(session.device_id)
        with lock:
            held = self._match(owner, session, allow_finished=True)
            held.finished = True
            grant = held.grant
            request = RequestRecord(str(owner), grant.request_id, grant.nonce,
                                    grant.lease_s / ((1 + grant.clock_rho) / (1 - grant.clock_rho)),
                                    "finished", grant.grant_id, grant.revision)
            # A completion retry is an explicit reconciliation, not a blind
            # repeat of an uncertain write. Never overwrite a newer grant's
            # request, and accept an already observed finished without resending.
            start = self._clock.now()
            current = store.read_r2()
            record = store.read_r1()
            if self._clock.now() - start > self._delta:
                raise ControlOwnershipRequired()
            if current != grant or record.request == request:
                pass
            else:
                self._publish(store, _Pending(request, self._clock.now()))
            if not held.observed_finished:
                held.observed_finished = True
                # Metadata release cannot prove actual command exclusivity or
                # cleanup. Only an independent resource observer can complete it.
                self._observe('episode_finished', store, request,
                              outcome='unknown', coverage='partial')
