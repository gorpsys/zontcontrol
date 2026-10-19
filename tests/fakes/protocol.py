"""Fresh scenario identities and explicit, shared v2 safety profile."""

from zontcontrol.coordination.models import ProtocolProfile

PROFILE = ProtocolProfile('ea3221c0-2a98-49f0-8705-e81917b9b5a9', 1, 1)


class Scenario:
    def __init__(self, *, period=1000, delta_cmd=1, ready=True):
        from .clock import FakeClock
        from .uuid import uuid_generator
        from .zont import FakeMetadataAdapter, FakeZontServer
        from zontcontrol.coordination.store import LeaderStore
        from zontcontrol.coordination.grants import GrantCoordinator
        self.uid = uuid_generator()
        self.clock = FakeClock(start=100)
        self.profile = ProtocolProfile(PROFILE.profile_id, 1, delta_cmd)
        self.prefix = 'Дом 🏠'
        self.server = FakeZontServer({42: {'name': self.prefix, 'notes': None},
                                      43: {'name': self.prefix, 'notes': None}}, clock=self.clock)
        self.adapter = FakeMetadataAdapter(self.server)
        self.store = self.make_store(LeaderStore)
        self.leader = self.candidate(period=period)
        self.coordinator = GrantCoordinator(self.store, self.leader, clock=self.clock,
                                            uuid_factory=self.uid)
        self.owner = self.uid()
        self.gate = self.make_gate()
        if ready:
            self.leader.try_acquire()
            self.advance(self.leader.timing.quarantine)

    def make_store(self, cls, *, adapter=None, device=42, profile=None, previous=None):
        return cls(adapter or self.adapter, device, original_name=self.prefix,
                   notes_capacity=10000, name_capacity=10000, profile=profile or self.profile,
                   previous_profile=previous)

    def candidate(self, *, period=1000, store=None, epoch=1700000000, **kwargs):
        from zontcontrol.coordination.leader import LeaderElection
        from zontcontrol.coordination.timing import LeaseTiming
        return LeaderElection(store or self.store, str(self.uid()),
            LeaseTiming(period, 1, (store or self.store).profile.delta_cmd_seconds),
            clock=self.clock, epoch_sampler=lambda: epoch, uuid_factory=self.uid, **kwargs)

    def make_gate(self, *, clock=None, adapter=None, device=42):
        from zontcontrol.coordination.grants import ExecutorGate
        from zontcontrol.coordination.store import ExecutorStore
        return ExecutorGate({device: self.make_store(ExecutorStore, adapter=adapter, device=device)},
                            clock=clock or self.clock, delta=1, uuid_factory=self.uid)

    def advance(self, seconds, *leaders):
        for leader in (self.leader, *leaders):
            leader.expect_resume(self.clock.now() + seconds)
        self.clock.advance(seconds)

    def pending(self, *, gate=None, owner=None, seconds=60, **kwargs):
        from zontcontrol.control_types import DeviceControlBusy
        try:
            return (gate or self.gate).try_acquire(owner or self.owner, 42,
                                                 lease_seconds=seconds, **kwargs)
        except DeviceControlBusy:
            return None

    def acquire(self, *, gate=None, owner=None, seconds=60, **kwargs):
        self.pending(gate=gate, owner=owner, seconds=seconds, **kwargs)
        self.coordinator.step()
        return (gate or self.gate).try_acquire(owner or self.owner, 42,
                                             lease_seconds=seconds, **kwargs)

    def inject_r1(self, record):
        from zontcontrol.coordination.models import encode_r1
        self.server.apply(self.server.send_update(42, {'name': encode_r1(self.prefix, record)}))
