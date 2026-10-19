#!/usr/bin/env python3
"""Reproducible v2 protocol experiments; offline by default, live is explicit.

Simulation is an estimate for the published scheduler and inputs, never a
measured ZONT bound. Live uses the application/controller/grant exclusively.
No physical completion evidence is manufactured from API or readback timing.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal
import gzip
import hashlib
import io
from functools import lru_cache
from itertools import product
import json
import math
from pathlib import Path
import random
import signal
import sys
from threading import Event
import time
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zontcontrol.config import CoordinationConfig
from zontcontrol.control_types import (
    CommandOutcomeUnknown, ControllerError, ControlOwnershipRequired, DeviceControlBusy,
    RateLimited,
)
from zontcontrol.controller.contracts import ControlRequestState, ValueKey
from zontcontrol.coordination.clock import BootClock, LeaderTimeline
from zontcontrol.coordination.grants import ExecutorGate, GrantCoordinator
from zontcontrol.coordination.leader import LeaderElection, LeaseLost
from zontcontrol.coordination.models import ProtocolProfile, RecordError, decode_r1
from zontcontrol.coordination.statistics import sample_summary
from zontcontrol.coordination.store import ExecutorStore, LeaderStore
from zontcontrol.coordination.timing import LeaseTiming, TimingViolation

POLL_RETRY = (.25, .5, 1, 2, 3, 5)
JITTER = (0, .1, .25, .5)
RENEWAL = (1/5, 1/4, 1/3)
SEEDS = (17, 42, 73)
SCENARIOS = ('one', 'two', 'dynamic-five', 'term-handover', 'r2-displaced')
FAULTS = ('timeout', 'lost-ack', '429', 'cancel', 'freeze', 'stop-finally')
PROFILE_ID = '99c733ec-f8d4-42fc-9cdb-376d46a4bd31'


@dataclass(frozen=True)
class Safety:
    delta: float = 5
    delta_cmd: float = 5
    period: float = 300
    lease: float = 30
    timeout: float = 30

    @property
    def timing(self):
        return LeaseTiming(self.period, self.delta, self.delta_cmd)


@dataclass(frozen=True, order=True)
class Pacing:
    poll_seconds: float
    retry_seconds: float
    jitter_fraction: float
    renewal_fraction: float

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class SeriesInput:
    scenario: str
    seed: int
    window_fraction: float
    clock_rates: tuple[float, float]
    requests: int = 100


def candidates(safety):
    for values in product(POLL_RETRY, POLL_RETRY, JITTER, RENEWAL):
        pacing = Pacing(*values)
        try:
            CoordinationConfig(safety.delta, safety.period, safety.delta_cmd,
                               PROFILE_ID, **pacing.as_dict())
            # The wake + full renewal window must fit before the stop margin.
            if (safety.period * pacing.renewal_fraction
                    + pacing.poll_seconds * (1 + pacing.jitter_fraction) + 2*safety.delta
                    >= safety.period - safety.timing.stop_margin):
                continue
        except ValueError:
            continue
        yield pacing


def normal_inputs():
    for scenario, seed, fraction, rates in product(
            SCENARIOS, SEEDS, (.1, .5, .9), ((1, 1), (.995, 1.005))):
        yield SeriesInput(scenario, seed, fraction, rates)


def request_summary(samples, *, violations=(), expected=None):
    """Every original request contributes; unknown coverage rejects a profile."""
    samples = [dict(row) for row in samples]
    for row in samples:
        timeout, wait = row.get('timeout_s'), row.get('wait_s')
        finite = lambda value: type(value) in (int, float) and math.isfinite(value)
        if (not finite(timeout) or timeout <= 0
                or row.get('outcome') in ('GRANTED', 'DECLINED') and (
                    not finite(wait) or wait < 0
                    or row['outcome'] == 'GRANTED' and wait >= timeout)):
            row['outcome'] = 'unknown'
    total = len(samples) if expected is None else expected
    if len(samples) > total:
        raise ValueError('More terminal samples than started requests')
    counts = {key: sum(s.get('outcome') == key for s in samples)
              for key in ('GRANTED', 'DECLINED', 'timeout', 'unknown')}
    counts['unknown'] += total - sum(counts.values())
    successful = [s['wait_s'] for s in samples if s.get('outcome') == 'GRANTED'
                  and type(s.get('wait_s')) in (int, float)]
    lower = [s['timeout_s'] if s.get('outcome') == 'timeout' else s['wait_s']
             for s in samples if s.get('outcome') in ('timeout', 'GRANTED', 'DECLINED')
             and type(s.get('timeout_s') if s.get('outcome') == 'timeout'
                      else s.get('wait_s')) in (int, float)]
    incomplete = counts['unknown'] or len(successful) != counts['GRANTED']
    return dict(started_requests=total, **counts,
        success_rate=counts['GRANTED']/total if total else 0,
        timeout_fraction=counts['timeout']/total if total else 0,
        successful_wait=sample_summary(successful),
        all_wait_lower_bounds=sample_summary(lower, unknown=total-len(lower)),
        censoring='right; timeout wait >= timeout_s',
        eligible=bool(total and counts['GRANTED'] and not incomplete and not violations),
        violations=sorted(set(violations)))


def select_profile(results):
    eligible = [r for r in results if r['summary']['eligible']]
    def rank(row):
        summary, pacing = row['summary'], row['pacing']
        return (-summary['success_rate'], summary['successful_wait']['p99'],
                row['http_count'], tuple(pacing[k] for k in Pacing.__dataclass_fields__))
    return min(eligible, key=rank) if eligible else None


class _World:
    def __init__(self):
        self.now = 0.


class _Clock:
    def __init__(self, world, rate):
        self.world, self.rate = world, rate

    def now(self):
        return 100 + self.world.now * self.rate

    def wait(self, cancelled, seconds):
        if not cancelled.is_set():
            self.world.now += seconds / self.rate
        return cancelled.is_set()


class _Port:
    """Atomic in-memory register port; each READ/WRITE has explicit latency.

    Events are serialized by a deterministic, non-preemptive scheduler. A full
    five-step READ/WRITE/confirmation window costs fraction*Delta in the
    slowest clock. No assertion about a production scheduler is made here.
    """
    def __init__(self, world, latency, profile):
        self.world, self.latency, self.profile = world, latency, profile
        self.values = {'name': 'Calibration', 'notes': None}
        self.http_count = 0
        self.fault = None
        self.fault_target = None
        self.violations = []
        self.grants = {}

    def _io(self, timeout, *, writing=False):
        self.http_count += 1
        if timeout is not None and self.latency > timeout:
            self.world.now += timeout
            raise CommandOutcomeUnknown() if writing else ControllerError()
        self.world.now += self.latency
        fault = None
        if self.fault_target in (None, 'write' if writing else 'read'):
            fault, self.fault = self.fault, None
        if fault == 'timeout':
            self.world.now += max(0, (timeout or 0) - self.latency)
            raise CommandOutcomeUnknown() if writing else ControllerError()
        if fault == '429':
            raise RateLimited()
        if fault == 'freeze':
            self.world.now += self.profile.delta_seconds * 2
        return fault

    def read_metadata_field(self, device, field, *, timeout_seconds=None):
        self._io(timeout_seconds)
        return self.values[field]

    def update_metadata(self, device, field, value, *, timeout_seconds=None):
        fault = self._io(timeout_seconds, writing=True)
        self.values[field] = value
        if field == 'name':
            record = decode_r1(value, 'Calibration')
            for grant in record.grants:
                for old in self.grants.values():
                    if (old.grant_id != grant.grant_id and old.owner_id != grant.owner_id
                            and max(old.valid_from, grant.valid_from)
                            < min(old.valid_until, grant.valid_until)):
                        self.violations.append('grant-interval-overlap')
                self.grants[grant.grant_id] = grant
        if fault == 'lost-ack':
            raise CommandOutcomeUnknown()


class _Observations:
    def __init__(self):
        self.violations = []
        self.windows = []
        self.terms = 0

    def emit(self, event, **fields):
        if event == 'leader_acquired':
            self.terms += 1
        if event in ('sync_window', 'freeze') and fields.get('exceeded'):
            self.violations.append('unexpected-' + event)
        if event == 'sync_window':
            self.windows.append(fields['sync_window_s'])


def run_series(pacing, item, safety):
    """Run real stores, election, coordinator and gate on controlled clocks.

    One/two/five slots receive the next fresh UUID after their predecessor's
    terminal result. Both slots in 'two' have the same initial deadline;
    dynamic-five has five initial joiners and five rotating identities.
    term-handover starts requests near the actual continuous-term cap.
    r2-displaced publishes one competing legitimate request before first poll.
    """
    world = _World()
    leader_clock, executor_clock = (_Clock(world, r) for r in item.clock_rates)
    rng = random.Random(item.seed)
    number = item.seed << 80
    def uid():
        nonlocal number
        number += 1
        return UUID(int=number)
    profile = ProtocolProfile(PROFILE_ID, safety.delta, safety.delta_cmd)
    port = _Port(world, safety.delta * item.window_fraction / 5 / max(item.clock_rates), profile)
    observations = _Observations()
    options = dict(original_name='Calibration', notes_capacity=10000,
                   name_capacity=10000, profile=profile, statistics=observations)
    leader_store = LeaderStore(port, 42, **options)
    executor_store = ExecutorStore(port, 42, **options)
    timeline = LeaderTimeline.capture(leader_clock, delta=safety.delta,
                                     epoch_sampler=lambda: 1700000000., uuid_factory=uid)
    def new_leader():
        leader = LeaderElection(leader_store, str(uid()), safety.timing,
            clock=leader_clock, timeline=timeline, uuid_factory=uid,
            statistics=observations, **pacing.as_dict())
        return leader, GrantCoordinator(leader_store, leader, clock=leader_clock,
                                       uuid_factory=uid, statistics=observations)
    leader, coordinator = new_leader()
    leader.try_acquire()
    if item.scenario == 'term-handover':
        world.now += (safety.timing.term_budget - safety.period/2) / leader_clock.rate
        # This is a planned idle interval, not an injected process freeze.
        leader.expect_resume(leader_clock.now())
    gate = ExecutorGate({42: executor_store}, clock=executor_clock, delta=safety.delta,
                        uuid_factory=uid, statistics=observations,
                        retry_seconds=pacing.retry_seconds)
    parallel = {'two': 2, 'dynamic-five': 5}.get(item.scenario, 1)
    samples, active = [], []
    started = 0
    leader_due = world.now
    renewed_at = leader_clock.now()
    displaced = False
    def pause(base, rate):
        return base * (1 + rng.uniform(0, pacing.jitter_fraction)) / rate
    while len(samples) < item.requests:
        # Register all starts in a wave before dispatching any network I/O.
        while len(active) < parallel and started < item.requests:
            created = executor_clock.now()
            active.append(dict(owner=uid(), request_id=uid(), created=created,
                deadline=created+safety.timeout, lease_deadline=created+safety.lease,
                due=world.now, index=started))
            started += 1
        request = min(active, key=lambda r: (r['due'], r['index'])) if active else None
        if request is not None and request['due'] <= leader_due:
            world.now = max(world.now, request['due'])
            outcome = None
            admitted = None
            cleanup_error = None
            if executor_clock.now() >= request['deadline']:
                outcome = 'timeout'
            else:
                try:
                    session = gate.try_acquire(request['owner'], 42, lease_seconds=safety.lease,
                        request_id=request['request_id'], local_deadline=request['lease_deadline'],
                        attempt_deadline=request['deadline'])
                    gate.validate(request['owner'], session)
                    outcome = 'GRANTED'
                    admitted = executor_clock.now()
                    try:
                        gate.release(request['owner'], session)
                    except (ControllerError, RecordError, TimingViolation) as error:
                        # Terminal acquisition is immutable; a subsequent
                        # cleanup failure cannot reclassify an issued session.
                        cleanup_error = type(error).__name__
                except DeviceControlBusy:
                    pass
                except ControlOwnershipRequired:
                    outcome = 'timeout' if executor_clock.now() >= min(
                        request['deadline'], request['lease_deadline']) else 'DECLINED'
                except (ControllerError, RecordError, TimingViolation):
                    outcome = 'DECLINED'
                    observations.violations.append('normal-request-error')
            if (item.scenario == 'r2-displaced' and not displaced and outcome is None
                    and started < item.requests):
                # The one-shot competing immediate acquisition is one of the
                # 100 starts, including its Busy/DECLINED terminal result. It
                # must not be hidden as an uncounted background request.
                other_owner, other_request = uid(), uid()
                other_started = executor_clock.now()
                try:
                    gate.try_acquire(other_owner, 42, lease_seconds=safety.lease,
                        request_id=other_request, local_deadline=other_started+safety.lease,
                        attempt_deadline=other_started+safety.timeout)
                except DeviceControlBusy:
                    pass
                other_wait = executor_clock.now()-other_started
                samples.append(dict(index=started, request_id=str(other_request),
                    outcome='DECLINED', wait_s=other_wait, wait_lower_bound_s=other_wait,
                    timeout_s=safety.timeout, cleanup_error=None,
                    reason='one-shot-contention'))
                started += 1
                gate.abandon_request(other_owner, 42, other_request)
                displaced = True
            if outcome is not None:
                elapsed = (admitted if admitted is not None else executor_clock.now()) - request['created']
                samples.append(dict(index=request['index'], request_id=str(request['request_id']),
                    outcome=outcome, wait_s=elapsed if outcome != 'timeout' else None,
                    wait_lower_bound_s=safety.timeout if outcome == 'timeout' else elapsed,
                    timeout_s=safety.timeout, cleanup_error=cleanup_error))
                gate.abandon_request(request['owner'], 42, request['request_id'])
                active.remove(request)
            else:
                request['due'] = world.now + pause(pacing.poll_seconds, executor_clock.rate)
        else:
            world.now = max(world.now, leader_due)
            # Dispatch time is explicitly planned by this serialized scheduler.
            leader.expect_resume(leader_clock.now())
            try:
                if not leader.can_coordinate(safety.delta):
                    if leader.record is not None and leader_clock.now() >= leader.term_expires_at:
                        leader.stop()
                        leader, coordinator = new_leader()
                    if not leader.try_acquire():
                        leader_due = world.now + pause(pacing.retry_seconds, leader_clock.rate)
                        continue
                    renewed_at = leader_clock.now()
                if leader_clock.now()-renewed_at >= safety.period*pacing.renewal_fraction:
                    leader.renew()
                    renewed_at = leader_clock.now()
                coordinator.step()
            except LeaseLost:
                pass  # Expected retirement at the term/stop-margin boundary.
            leader_due = world.now + pause(pacing.poll_seconds, leader_clock.rate)
    violations = observations.violations + port.violations
    return dict(status='simulation', inputs=asdict(item), samples=sorted(samples, key=lambda s:s['index']),
        summary=request_summary(samples, violations=violations, expected=item.requests),
        http_count=port.http_count, leader_terms=observations.terms,
        sync_windows=sample_summary(observations.windows), window_samples_s=observations.windows,
        violations=sorted(set(violations)))


def run_fault_matrix(pacing, safety, *, seeds=SEEDS):
    """Intentional faults have separate denominators and never improve ranking."""
    rows = []
    for seed, fault in product(seeds, FAULTS):
        world = _World()
        clock = _Clock(world, 1)
        number = seed << 80
        def uid():
            nonlocal number
            number += 1
            return UUID(int=number)
        profile = ProtocolProfile(PROFILE_ID, safety.delta, safety.delta_cmd)
        port = _Port(world, .02, profile)
        observations = _Observations()
        options = dict(original_name='Calibration', notes_capacity=10000,
                       name_capacity=10000, profile=profile, statistics=observations)
        store = LeaderStore(port, 42, **options)
        leader = LeaderElection(store, str(uid()), safety.timing, clock=clock,
            epoch_sampler=lambda:1700000000., uuid_factory=uid, **pacing.as_dict())
        coordinator = GrantCoordinator(store, leader, clock=clock, uuid_factory=uid)
        gate = ExecutorGate({42: ExecutorStore(port, 42, **options)}, clock=clock,
            delta=safety.delta, uuid_factory=uid, retry_seconds=pacing.retry_seconds)
        leader.try_acquire()
        world.now += safety.timing.quarantine
        leader.expect_resume(clock.now())
        owner, request_id = uid(), uid()
        passed, observed = False, 'unknown'
        try:
            if fault in ('timeout', 'lost-ack', '429'):
                port.fault, port.fault_target = fault, 'write'
                before = port.http_count
                try:
                    gate.try_acquire(owner, 42, lease_seconds=safety.lease,
                                     request_id=request_id)
                except (ControllerError, DeviceControlBusy) as error:
                    observed = type(error).__name__
                    expected = {'timeout': CommandOutcomeUnknown, 'lost-ack': CommandOutcomeUnknown,
                                '429': RateLimited}[fault]
                    # Exactly one publication attempt, never an automatic retry.
                    passed = isinstance(error, expected) and port.http_count-before == 4
            elif fault == 'freeze':
                before = port.http_count
                leader.expect_resume(clock.now())
                world.now += 2*safety.delta
                try:
                    leader.renew()
                except LeaseLost:
                    observed = 'LeaseLost; freeze before I/O'
                    passed = port.http_count == before and not leader.can_coordinate(0)
                from zontcontrol.coordination.timing import OperationWindow
                window = OperationWindow(clock, safety.delta)
                world.now += safety.delta * 1.1
                try:
                    window.complete()
                    passed = False
                except TimingViolation:
                    observed += '; full window over Delta rejected'
            else:
                try:
                    gate.try_acquire(owner, 42, lease_seconds=safety.lease,
                                     request_id=request_id)
                except DeviceControlBusy:
                    pass
                if fault == 'cancel':
                    before = port.http_count
                    gate.abandon_request(owner, 42, request_id)
                    observed = 'abandoned; no I/O'
                    passed = port.http_count == before and not gate._pending
                else:
                    leader.expect_resume(clock.now())
                    coordinator.step()
                    session = gate.try_acquire(owner, 42, lease_seconds=safety.lease,
                                               request_id=request_id)
                    leader.stop()
                    # Algorithm stop/leader role loss never cancels finally.
                    # Two actual cached-grant admissions bracket that finally.
                    gate.validate(owner, session)
                    gate.validate(owner, session)
                    gate.release(owner, session)
                    observed, passed = 'finally admitted; released once', True
        except (ControllerError, LeaseLost, RecordError, TimingViolation) as error:
            observed = type(error).__name__
        rows.append(dict(matrix='fault', scenario=fault, seed=seed, passed=passed,
                         observed=observed, http_count=port.http_count))
    return rows


def live_series(controller, device, *, count=4, lease_seconds=180, timeout=90,
                poll_seconds=1, cancelled=None, now=None):
    """Authorized +/-0.1 air-target series; a single outer finally restores mode.

    Never retry an unknown write. Only ownership rejection (before send) can
    trigger one fresh, bounded acquisition for the final Schedule command.
    """
    stop = cancelled if cancelled is not None else Event()
    now = BootClock().now if now is None else now
    report = dict(status='unmeasured', source='runtime', started_requests=0, samples=[],
                  commands=[], schedule_confirmed=False, physical_tail={'status': 'unknown'},
                  error=None, finalization_error=None)
    session = None
    circuit = mode = None
    def acquire():
        report['started_requests'] += 1
        started = now()
        try:
            result = controller.acquire_control(device, lease_seconds=lease_seconds,
                timeout=timeout, poll_seconds=poll_seconds)
        except Exception:
            report['samples'].append(dict(outcome='unknown', wait_s=None, timeout_s=timeout))
            raise
        granted = result.state is ControlRequestState.GRANTED
        reason = getattr(result.reason, 'value', '')
        outcome = 'GRANTED' if granted else 'timeout' if reason == 'timeout' else 'DECLINED'
        report['samples'].append(dict(outcome=outcome,
            wait_s=now()-started if outcome != 'timeout' else None, timeout_s=timeout))
        if not granted:
            raise ControlOwnershipRequired()
        return result.session
    def selected():
        controls = controller.list_controls(device)
        circuits = [c for c in controls if c.key.kind == 'circuit' and c.name == 'Отопление']
        modes = [c for c in controls if c.key.kind == 'mode' and c.name == 'Расписание']
        if len(circuits) != 1 or len(modes) != 1:
            raise ValueError('Unique heating circuit and Schedule mode required')
        # The public API exposes this circuit's target_temp; boiler/setpoint
        # channels are deliberately never selected. prepare_command revalidates
        # capabilities/bounds; its general API does not enforce the UI step.
        report['air_target_step'] = circuits[0].step
        return circuits[0].key, modes[0].key
    def read(field):
        key = ValueKey(device, 'circuit', circuit.element_id, field)
        values = controller.read_current(device, (key,)).values
        matches = [v.value for v in values if v.key == key]
        if len(matches) != 1 or matches[0] is None:
            raise ValueError('Current value unavailable')
        return matches[0]
    def execute(key, action, args):
        command = controller.prepare_command(device, key, action, args)
        started = now()
        controller.execute_command(command, session)
        report['commands'].append(dict(kind=key.kind, element_id=key.element_id,
            action=action, critical_section_s=now()-started, arguments=args))
    try:
        circuit, mode = selected()
        step = report['air_target_step']
        if count and step is not None and Decimal('.1') % Decimal(str(step)) != 0:
            # A float request schema is not evidence that a smaller step than
            # the current inventory advertises is permitted for this series.
            report['preflight_error'] = 'unsupported_air_target_step'
            raise ValueError('The advertised temperature step does not permit 0.1')
        session = acquire()
        for index in range(count):
            if stop.is_set():
                report['error'] = 'stopped'
                break
            # Fresh, confirmed current target every time; never increment a
            # local remembered target after an unknown API result.
            current = read('target_temp')
            if type(current) not in (float, int) or not math.isfinite(current):
                raise ValueError('Invalid air target')
            target = float(Decimal(str(current)) + (Decimal('.1') if index%2 == 0 else Decimal('-.1')))
            execute(circuit, 'target-temp', {'target_temp': target})
            confirmed = read('target_temp')
            if abs(confirmed-target) > 1e-6:
                raise ValueError('Air target readback mismatch')
    except Exception as error:
        report['error'] = type(error).__name__
    finally:
        try:
            if circuit is None or mode is None:
                circuit, mode = selected()
            if session is None:
                session = acquire()
            try:
                execute(mode, 'activate', {})
            except ControlOwnershipRequired:
                # Rejection means no send. The old request is not extended.
                try:
                    controller.release_control(session)
                finally:
                    session = None
                session = acquire()
                execute(mode, 'activate', {})
            report['schedule_confirmed'] = read('mode_id') == mode.element_id
        except Exception as error:
            report['finalization_error'] = type(error).__name__
            if isinstance(error, CommandOutcomeUnknown) and circuit is not None and mode is not None:
                # Readback may confirm the final mode after a lost response;
                # never repeat the unknown write to obtain an acknowledgement.
                try:
                    report['schedule_confirmed'] = read('mode_id') == mode.element_id
                except Exception:
                    pass
        finally:
            if session is not None:
                try:
                    controller.release_control(session)
                except Exception as error:
                    report['release_error'] = type(error).__name__
    report['summary'] = request_summary(report['samples'], expected=report['started_requests'])
    return report


@contextmanager
def cached_codecs():
    """Cache only pure immutable wire decoding in an offline worker process.

    Every protocol call, port operation, deadline and sample still runs. This
    removes repeated JSON/UUID validation of identical strings, with no trace
    substitution or skipped matrix cells. Never used by a live application.
    """
    from zontcontrol.coordination import store as module
    original_r1, original_r2 = module.decode_r1, module.decode_r2
    module.decode_r1, module.decode_r2 = lru_cache(1024)(original_r1), lru_cache(1024)(original_r2)
    try:
        yield
    finally:
        module.decode_r1, module.decode_r2 = original_r1, original_r2


def _profile_job(job):
    pacing, safety, output = job
    return _write_profile(pacing, safety, output,
                          (run_series(pacing, item, safety) for item in normal_inputs()))


def _refresh_r2_job(job):
    pacing, safety, output = job
    label = '-'.join(format(v, '.8g') for v in asdict(pacing).values())
    with gzip.open(Path(output)/(label+'.jsonl.gz'), 'rt', encoding='utf-8') as stream:
        old = [json.loads(line) for line in stream]
    if len(old) != 90:
        raise ValueError('Cannot refresh an incomplete matrix')
    rows = []
    for row in old:
        if row['inputs']['scenario'] == 'r2-displaced':
            item = dict(row['inputs'])
            item['clock_rates'] = tuple(item['clock_rates'])
            with cached_codecs():
                row = run_series(pacing, SeriesInput(**item), safety)
        rows.append(row)
    return _write_profile(pacing, safety, output, rows)


def _write_profile(pacing, safety, output, rows):
    label = '-'.join(format(v, '.8g') for v in asdict(pacing).values())
    path = Path(output) / (label+'.jsonl.gz')
    samples, violations, http_count, windows = [], [], 0, []
    with cached_codecs(), path.open('wb') as raw, \
            gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as zipped, \
            io.TextIOWrapper(zipped, encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row, separators=(',', ':'))+'\n')
            samples.extend(row['samples'])
            violations.extend(row['violations'])
            http_count += row['http_count']
            windows.extend(row['window_samples_s'])
    timing = sample_summary(windows)
    return dict(pacing=pacing.as_dict(), summary=request_summary(samples, violations=violations,
        expected=90*100), http_count=http_count, artifact=path.name,
        sync_windows=timing,
        delta_recommendation_s=(max(5, 10*timing['p99_9'], 1.2*timing['max'])
                                if timing['count'] else None),
        recommendation_source='simulation; not a measured ZONT bound',
        sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--fault-only', action='store_true')
    parser.add_argument('--refresh-r2', action='store_true',
                        help='Recompute all R2-displacement cells in an existing complete matrix')
    parser.add_argument('--device', type=int)
    parser.add_argument('--count', type=int, default=4)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.live:
        if args.device is None or args.count < 0:
            parser.error('--live requires --device and nonnegative --count')
        from zontcontrol.application import create_application
        stop = Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        with create_application(catalog=()) as app:
            report = live_series(app.controller(), args.device, count=args.count, cancelled=stop)
        (args.output/'live.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report, indent=2))
        return int(not report['schedule_confirmed'] or report['error'] is not None)
    safety = Safety()
    if args.fault_only:
        faults = [dict(pacing=p.as_dict(), samples=run_fault_matrix(p, safety))
                  for p in candidates(safety)]
        report = dict(status='simulation', matrix='fault', safety=asdict(safety),
                      scenarios=FAULTS, seeds=SEEDS, profiles=faults)
        (args.output/'fault-report.json').write_text(json.dumps(report, indent=2)+'\n')
        failed = sum(not r['passed'] for p in faults for r in p['samples'])
        print(f'{len(faults)*len(SEEDS)*len(FAULTS)} fault cases; {failed} unexpected results')
        return int(failed != 0)
    jobs = [(p, safety, str(args.output)) for p in candidates(safety)]
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        operation = _refresh_r2_job if args.refresh_r2 else _profile_job
        futures = [pool.submit(operation, job) for job in jobs]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            (args.output/'checkpoint.json').write_text(json.dumps(
                sorted(results, key=lambda r: tuple(r['pacing'].values())), indent=2)+'\n')
            print(f'{len(results)}/{len(jobs)} profiles; '
                  f'{result["summary"]["GRANTED"]}/9000 grants', flush=True)
    results.sort(key=lambda r: tuple(r['pacing'].values()))
    winner = select_profile(results)
    report = dict(status='simulation', harness_revision=2, safety=asdict(safety), seeds=SEEDS,
        normal_scenarios=SCENARIOS, fault_scenarios=FAULTS, requests_per_series=100,
        series_per_profile=90, profiles=results, selected=winner,
        zont_timing_status='unmeasured', physical_tail={'status': 'unknown'})
    (args.output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    return int(winner is None)


if __name__ == '__main__':
    raise SystemExit(main())
