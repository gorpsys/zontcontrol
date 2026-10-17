"""Lazy device coordination workers and their shared runtime."""

from threading import Event, Lock, Thread
from uuid import uuid4

from ..control_types import ControllerError, ControlOwnershipRequired, InvalidArguments
from .grants import ExecutorGate, GrantCoordinator
from .leader import LeaderElection, LeaseLost
from .models import RecordError, original_name
from .ports import Clock, CoordinationSettings, MetadataPort
from .statistics import observe
from .store import ExecutorStore, LeaderStore


class CoordinatorWorker:
    def __init__(self, store, leader, coordinator, clock, statistics):
        self.store, self.leader, self.coordinator = store, leader, coordinator
        self.clock, self.statistics = clock, statistics
        self.cancelled = Event()
        self.thread = Thread(target=self._work, name=f'coordinator-{store.device_id}', daemon=False)

    def _work(self):
        renewed_at = self.clock.now()
        try:
            while not self.cancelled.is_set():
                try:
                    if not self.leader.can_coordinate(self.coordinator.window):
                        if not self.leader.try_acquire(cancelled=self.cancelled):
                            self.clock.wait(self.cancelled, self.leader.timing.poll_after)
                            continue
                        renewed_at = self.clock.now()
                    if self.cancelled.is_set():
                        break
                    if self.clock.now() - renewed_at >= self.leader.timing.renew_after:
                        self.leader.renew()
                        renewed_at = self.clock.now()
                    self.coordinator.step()
                except (LeaseLost, ControllerError, RecordError):
                    # Role loss never reaches algorithm cancellation or finally.
                    observe(self.statistics, 'error', device_id=self.store.device_id,
                            outcome='unknown')
                self.clock.wait(self.cancelled, self.leader.timing.poll_after)
        finally:
            # Foreign/unsettled grants must expire, never be blindly cleared.
            try:
                if self.leader.can_coordinate(self.leader.timing.delta):
                    self.coordinator.step(accepting=False)
                    if self.store.read_r2() is None:
                        self.leader.release()
            except (LeaseLost, ControllerError, RecordError):
                pass
            self.leader.stop()

    def close(self):
        self.cancelled.set()
        self.thread.join()


class CoordinationRuntime:
    """Lazily attach independent device workers; R2 writers stay internal.

    Byte capacities are local refusal budgets, not inferred API limits.
    Names are recovered only from a strictly validated v1 suffix.
    """

    def __init__(self, adapter: MetadataPort, config: CoordinationSettings, clock: Clock,
                 statistics, worker_factory=None):
        self.adapter, self.config, self.clock = adapter, config, clock
        self.statistics = statistics
        self._worker_factory = CoordinatorWorker if worker_factory is None else worker_factory
        self._lock = Lock()
        self._devices = {}
        self._device_locks = {}
        self._closed = False

    def _check_attempt(self, local_deadline=None, attempt_deadline=None):
        if (self._closed or any(deadline is not None and self.clock.now() >= deadline
                              for deadline in (local_deadline, attempt_deadline))):
            raise ControlOwnershipRequired()

    def _device(self, device_id, *, local_deadline=None, attempt_deadline=None):
        if type(device_id) is not int or device_id <= 0:
            raise InvalidArguments()
        with self._lock:
            self._check_attempt(local_deadline, attempt_deadline)
            lock = self._device_locks.setdefault(device_id, Lock())
        # Account state lock never spans metadata I/O for a different device.
        with lock:
            with self._lock:
                self._check_attempt(local_deadline, attempt_deadline)
            if device_id not in self._devices:
                metadata = self.adapter.read_metadata(device_id)
                name = original_name(metadata['name'])
                options = dict(original_name=name, notes_capacity=65536, name_capacity=16384)
                executor_store = ExecutorStore(self.adapter, device_id, **options)
                leader_store = LeaderStore(self.adapter, device_id, **options)
                with self._lock:
                    self._check_attempt(local_deadline, attempt_deadline)
                executor_store.read_r1()
                with self._lock:
                    self._check_attempt(local_deadline, attempt_deadline)
                executor_store.read_r2()
                leader = LeaderElection(leader_store, str(uuid4()), self.config.timing,
                                        clock=self.clock, statistics=self.statistics)
                coordinator = GrantCoordinator(leader_store, leader, clock=self.clock,
                                               statistics=self.statistics)
                worker = self._worker_factory(leader_store, leader, coordinator,
                                             self.clock, self.statistics)
                gate = ExecutorGate({device_id: executor_store}, clock=self.clock,
                                    delta=self.config.delta_seconds, statistics=self.statistics)
                with self._lock:
                    self._check_attempt(local_deadline, attempt_deadline)
                    self._devices[device_id] = (gate, worker)
                    worker.thread.start()
            with self._lock:
                self._check_attempt(local_deadline, attempt_deadline)
                return self._devices[device_id][0]

    def try_acquire(self, owner, device_id, *, lease_seconds, request_id=None,
                    local_deadline=None, attempt_deadline=None):
        gate = self._device(device_id, local_deadline=local_deadline,
                            attempt_deadline=attempt_deadline)
        return gate.try_acquire(owner, device_id, lease_seconds=lease_seconds,
                                request_id=request_id, local_deadline=local_deadline,
                                attempt_deadline=attempt_deadline)

    def abandon_request(self, owner, device_id, request_id):
        # Cancellation cannot initialize a device or start an independent role.
        with self._lock:
            device = self._devices.get(device_id)
        if device is not None:
            device[0].abandon_request(owner, device_id, request_id)

    def validate(self, owner, session):
        return self._device(session.device_id).validate(owner, session)

    def release(self, owner, session):
        return self._device(session.device_id).release(owner, session)

    def observation_context(self, owner, session):
        return self._device(session.device_id).observation_context(owner, session)

    def close(self):
        with self._lock:
            self._closed = True
            workers = tuple(worker for _, worker in self._devices.values())
        for worker in workers:
            worker.cancelled.set()
        for worker in workers:
            worker.close()
