"""Read-only chunked collection of normalized ZONT history, with local checkpoints."""

import argparse
import gzip
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zontcontrol.client import ZontClient, ZontError
from zontcontrol.history import cli_period


def collect(client, start, end, output, device_id, circuit_id, *, request_delay=2):
    path = output / f"{int(start.timestamp())}-{int(end.timestamp())}.json.gz"
    if path.exists():
        with gzip.open(path, "rt") as stream:
            data = json.load(stream)
    else:
        for attempt in range(6):
            time.sleep(request_delay)
            try:
                data = client.read_history(start, end, device_id=device_id, circuit_id=circuit_id,
                                           include_controls=True)
                break
            except ZontError as error:
                if "rate limit" in str(error) and attempt < 5:
                    delay = min(15 * 2 ** attempt, 60)
                    print(f"API rate limit: retrying read in {delay} seconds", flush=True)
                    time.sleep(delay)
                    continue
                if "size limit" not in str(error) or end - start <= timedelta(hours=1):
                    raise
                midpoint = start + (end - start) / 2
                return collect(client, start, midpoint, output, device_id, circuit_id, request_delay=request_delay) + collect(
                    client, midpoint, end, output, device_id, circuit_id, request_delay=request_delay)
        if any(device["time_truncated"] for device in data["devices"]) and end - start > timedelta(hours=1):
            midpoint = start + (end - start) / 2
            return collect(client, start, midpoint, output, device_id, circuit_id, request_delay=request_delay) + collect(
                client, midpoint, end, output, device_id, circuit_id, request_delay=request_delay)
        with gzip.open(path.with_suffix(".tmp"), "wt", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
        path.with_suffix(".tmp").replace(path)
    device = data["devices"][0]
    return [{"file": path.name, "start": start.isoformat(), "end": end.isoformat(),
             "time_truncated": device["time_truncated"],
             "sensor_points": {str(sensor["id"]): len(sensor["points"])
                               for sensor in device["temperature_sensors"]},
             "circuit_points": {str(circuit["id"]): {key: len(points) for key, points in circuit["series"].items()}
                                for circuit in device["circuits"]}}]


def main():
    parser = argparse.ArgumentParser()
    period = parser.add_mutually_exclusive_group()
    period.add_argument("--days", type=int, help="Number of days (default: 21)")
    period.add_argument("--from", dest="start", help="Explicit RFC 3339 start with timezone")
    parser.add_argument("--to", dest="end", help="Explicit RFC 3339 end with timezone")
    parser.add_argument("--device-id", type=int, default=700877)
    parser.add_argument("--circuit-id", type=int, default=20496)
    parser.add_argument("--output", type=Path, default=Path("reports/history-3w"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text())
        if old["device_id"] != args.device_id or old["circuit_id"] != args.circuit_id:
            raise ValueError("choose another output directory for a different device or circuit")
        start = datetime.fromisoformat(old["start"])
        end = datetime.fromisoformat(old["end"])
        if args.start and cli_period(args.start, old["end"], None)[0] != start:
            raise ValueError("choose another output directory for a different history start")
        if args.end and cli_period(old["start"], args.end, None)[1] != end:
            raise ValueError("choose another output directory for a different history end")
    else:
        if args.days is not None and args.days <= 0:
            parser.error("--days must be positive")
        start, end = cli_period(args.start, args.end, None if args.start else 24 * (args.days or 21))
    client = ZontClient(os.environ["ZONT_CONTACT"], token=os.environ["ZONT_TOKEN"])
    chunks = []
    cursor = start
    count = 0
    while cursor < end:
        next_time = min(cursor + timedelta(days=1), end)
        chunks.extend(collect(client, cursor, next_time, args.output, args.device_id, args.circuit_id))
        cursor = next_time
        count += 1
        manifest = {"device_id": args.device_id, "circuit_id": args.circuit_id,
                    "start": start.isoformat(), "end": end.isoformat(), "chunks": chunks}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        if count % 4 == 0 or cursor == end:
            print(f"Collected {count} daily windows, {len(chunks)} chunks", flush=True)
    print(f"Manifest: {manifest_path.resolve()}", flush=True)


if __name__ == "__main__":
    main()
