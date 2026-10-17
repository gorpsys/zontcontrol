"""Field-specific metadata access, exact readback, and local size checks.

Capacities are explicit UTF-8 byte budgets for the complete serialized fields,
not claimed ZONT limits. Readback detects truncation/normalization; a successful
round trip does not prove durability, linearizability or absence of races.
R1 read/modify/write is deliberately not compare-and-swap. LeaderStore is an
internal writer capability: the coordinator must confirm its R1 term before
each R2 write. The store itself does not elect or authorize a leader.
"""

from ..control_types import CommandOutcomeUnknown, ControllerError
from .models import R1Record, RecordError, decode_r1, decode_r2, encode_r1, encode_r2


class RecordCapacityError(RecordError):
    def __init__(self):
        ValueError.__init__(self, "Complete coordination field exceeds configured capacity")


class ExecutorStore:
    """Executors may publish R1 requests; R2 has no write method here."""

    def __init__(self, adapter, device_id, *, original_name, notes_capacity, name_capacity):
        if (type(device_id) is not int or device_id <= 0 or not isinstance(original_name, str)
                or any(type(size) is not int or size <= 0 for size in (notes_capacity, name_capacity))):
            raise RecordError()
        self._adapter = adapter
        self.device_id = device_id
        self.original_name = original_name
        self._capacities = {"notes": notes_capacity, "name": name_capacity}

    def read_r1(self):
        return decode_r1(self._adapter.read_metadata_field(self.device_id, "notes"))

    def read_r2(self):
        return decode_r2(self._adapter.read_metadata_field(self.device_id, "name"), self.original_name)

    def _write(self, field, raw):
        try:
            size = len(raw.encode("utf-8"))
        except UnicodeError:
            raise RecordError() from None
        if size > self._capacities[field]:
            raise RecordCapacityError()
        # Refuse to erase unknown data or a human rename. This check is not CAS:
        # a concurrent writer can still change the field after the read.
        if field == "notes":
            self.read_r1()
        else:
            self.read_r2()
        self._adapter.update_metadata(self.device_id, field, raw)
        try:
            confirmed = self._adapter.read_metadata_field(self.device_id, field)
        except ControllerError:
            raise CommandOutcomeUnknown() from None
        if confirmed != raw:
            raise CommandOutcomeUnknown()

    def write_r1(self, record):
        self._write("notes", encode_r1(record))

    def publish_request(self, request):
        """Preserve the observed header; this bounded RMW is still not CAS."""
        record = self.read_r1()
        self.write_r1(R1Record(record.leader, request))


class LeaderStore(ExecutorStore):
    """Internal coordinator capability; executors receive ExecutorStore only."""

    def write_r2(self, grant):
        self._write("name", encode_r2(self.original_name, grant))
