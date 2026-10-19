"""Role-specific v2 capabilities, bounded I/O and exact metadata readback."""

from ..control_types import CommandOutcomeUnknown, ControllerError
from .models import (ProtocolProfile, R2Record, RecordError,
                     decode_r1, decode_r2, encode_r1, encode_r2)


class RecordCapacityError(RecordError):
    def __init__(self):
        ValueError.__init__(self, 'Complete coordination field exceeds configured capacity')


class _Reader:
    def __init__(self, adapter, device_id, *, original_name, notes_capacity, name_capacity,
                 profile: ProtocolProfile, previous_profile: ProtocolProfile | None = None):
        if (type(device_id) is not int or device_id <= 0 or not isinstance(original_name, str)
                or any(type(n) is not int or n <= 0 for n in (notes_capacity, name_capacity))
                or type(profile) is not ProtocolProfile
                or previous_profile is not None and (type(previous_profile) is not ProtocolProfile
                                                     or previous_profile.profile_id == profile.profile_id)):
            raise RecordError()
        self._adapter = adapter
        self.device_id, self.original_name = device_id, original_name
        self.profile, self.previous_profile = profile, previous_profile
        self._capacities = {'name': name_capacity, 'notes': notes_capacity}

    def _read(self, field, window):
        raw = self._adapter.read_metadata_field(self.device_id, field,
                    timeout_seconds=window.before_io() if window is not None else None)
        if window is not None:
            window.complete()
        return raw

    def read_r1(self, *, window=None):
        record = decode_r1(self._read('name', window), self.original_name)
        if record is not None and record.profile not in (self.profile, self.previous_profile):
            raise RecordError()
        return record

    def read_r2(self, *, window=None):
        record = decode_r2(self._read('notes', window))
        ids = (self.profile.profile_id,
               self.previous_profile.profile_id if self.previous_profile else None)
        if record is not None and record.profile_id not in ids:
            raise RecordError()
        return record

    def _capacity(self, field, raw):
        try:
            size = len(raw.encode('utf-8'))
        except UnicodeError:
            raise RecordError() from None
        if size > self._capacities[field]:
            raise RecordCapacityError()

    def _send(self, field, raw, window, timeout):
        self._adapter.update_metadata(self.device_id, field, raw, timeout_seconds=timeout)
        window.complete()
        try:
            confirmed = self._read(field, window)
        except ControllerError:
            raise CommandOutcomeUnknown() from None
        if confirmed != raw:
            raise CommandOutcomeUnknown()


class ExecutorStore(_Reader):
    """Only notes/R2 may be written by an executor."""

    def publish_request(self, request, *, window):
        raw = encode_r2(R2Record(self.profile.profile_id, request))
        self._capacity('notes', raw)
        current = self.read_r2(window=window)
        r1 = self.read_r1(window=window)
        if r1 is not None and r1.profile != self.profile:
            raise RecordError()
        if current is not None and current.profile_id != self.profile.profile_id:
            if r1 is None or r1.profile != self.profile:
                raise RecordError()
        self._send('notes', raw, window, window.before_io())


class LeaderStore(_Reader):
    """Only name/R1 may be written; every send requires the original guard."""

    def write_r1(self, record, *, window, role_deadline, stop_margin=0):
        if record.profile != self.profile:
            raise RecordError()
        raw = encode_r1(self.original_name, record)
        self._capacity('name', raw)
        current = self.read_r1(window=window)
        if current is not None and current.record_id == record.record_id:
            raise RecordError()
        self.read_r2(window=window)
        timeout = window.guard_write(role_deadline=role_deadline, stop_margin=stop_margin)
        self._send('name', raw, window, timeout)
        window.complete(role_deadline=role_deadline, stop_margin=stop_margin)
