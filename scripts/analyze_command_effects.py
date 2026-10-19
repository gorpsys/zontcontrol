#!/usr/bin/env python3
"""Analyze explicitly typed offline command-effect JSONL; never contact ZONT.

command_effect samples require sample_kind=critical_section, command, or
residual_effect. Sections/commands use started_at/finished_at. Physical upper
bounds use command_observation_id, clock_id, admitted_at, effect_completed_at,
completion_basis=physical_upper_bound, outcome=success and coverage=complete.
They must match a command_started row from that same local BOOTTIME domain.
Optional sent_at yields a separate network-to-effect duration. API RTT and a
40-second reset cycle cannot calibrate Delta_cmd. Missing evidence is unknown.
Residual exceedances use delta_cmd_s (initial assumption: 5 seconds); no such
threshold is applied to critical sections or API command durations.
"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zontcontrol.coordination.statistics import ObservationError, analyze_command_effects, read_jsonl


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path, help='Explicitly typed JSONL samples')
    args = parser.parse_args(argv)

    def observations():
        for path in args.files:
            with path.open(encoding='utf-8') as stream:
                yield from read_jsonl(stream)

    try:
        result = analyze_command_effects(observations())
    except (OSError, UnicodeError, ObservationError):
        parser.exit(2, 'Cannot analyze command effects: invalid or unreadable JSONL\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
