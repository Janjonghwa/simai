"""Compare two relay status-log captures (same camera load, helper off/on)."""
import argparse
import datetime as dt
import json
import statistics
from pathlib import Path


def read(path):
    cameras = {}
    for line in path.read_text(encoding='utf-8-sig', errors='replace').splitlines():
        if '상태 {' not in line or '미리보기 상태' in line or '원격 CCTV 상태' in line:
            continue
        try:
            prefix, payload = line.split('상태 ', 1)
            timestamp = dt.datetime.strptime(prefix[:19], '%Y-%m-%d %H:%M:%S').timestamp()
            data = json.loads(payload)
            cameras.setdefault(data['camera_id'], []).append((timestamp, data))
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
    result = {}
    for cid, rows in cameras.items():
        if len(rows) < 2:
            continue
        elapsed = rows[-1][0] - rows[0][0]
        frames = rows[-1][1]['frames'] - rows[0][1]['frames']
        if elapsed <= 0 or frames < 0:
            continue
        p95 = [row['receive_delay_ms']['p95'] for _, row in rows if row['receive_delay_ms']['p95'] is not None]
        result[cid] = {'seconds': elapsed, 'fps': frames / elapsed,
                       'receive_delay_p95_median_ms': statistics.median(p95) if p95 else None}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    args = parser.parse_args()
    print(json.dumps({'helper_off': read(args.before), 'helper_on': read(args.after)}, indent=2))


if __name__ == '__main__':
    main()
