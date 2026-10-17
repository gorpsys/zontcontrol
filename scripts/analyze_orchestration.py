#!/usr/bin/env python3
"""Read observation JSONL; no controller, network, recovery or command effects."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zontcontrol.coordination import ObservationError, analyze, read_jsonl


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path, help='JSONL observation files')
    args = parser.parse_args(argv)

    def observations():
        for path in args.files:
            with path.open(encoding='utf-8') as stream:
                yield from read_jsonl(stream)

    try:
        result = analyze(observations())
    except (OSError, UnicodeError, ObservationError):
        parser.exit(2, 'Cannot analyze orchestration observations: invalid or unreadable JSONL\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
