"""9차 GPU 세션용 버전별 진단 PNG 저장과 전후 비교 그림(로컬 파일만)."""

import argparse
import json
from pathlib import Path
import re

import numpy as np


def save_result(root, asset, result):
    wid = asset["world_id"]
    if not re.fullmatch(r"[A-Za-z0-9_~-][A-Za-z0-9._~-]{0,63}", wid) or ".." in wid:
        raise ValueError("진단 저장 World ID 오류")
    directory = Path(root) / wid / f'e{asset["stream_epoch"]}' / f'v{asset["appearance_version"]}'
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in (("albedo.png", result.albedo.data), ("surface_source.png", result.surface.data),
                       ("avatar_asset.json", json.dumps(asset, ensure_ascii=False, indent=2).encode("utf-8"))):
        path = directory / name
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError("진단 자산 버전 덮어쓰기 거절")
        else:
            with path.open("xb") as stream:
                stream.write(data)
    return directory


def comparison(before, after, output):
    """albedo/출처 지도를 위아래로, 이전/이후를 좌우로 놓는다. 합성 복원 없음."""
    import cv2

    columns = []
    for label, directory in (("BEFORE", Path(before)), ("AFTER", Path(after))):
        rgb = cv2.imdecode(np.frombuffer((directory / "albedo.png").read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        mask = cv2.imdecode(np.frombuffer((directory / "surface_source.png").read_bytes(), np.uint8), cv2.IMREAD_UNCHANGED)
        if rgb is None or mask is None or mask.ndim != 2 or rgb.shape[:2] != mask.shape:
            raise ValueError("비교 PNG 해상도·채널 오류")
        if not np.isin(mask, [0, 1, 2, 128, 130]).all():
            raise ValueError("출처 지도 값 오류")
        palette = np.array([[64, 64, 64], [0, 190, 255], [80, 220, 60]], np.uint8)
        colored = palette[mask & 127]
        contours, _ = cv2.findContours(((mask & 128) > 0).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(colored, contours, -1, (80, 80, 255), 1)
        header = np.full((40, rgb.shape[1], 3), 245, np.uint8)
        cv2.putText(header, label, (8, 27), cv2.FONT_HERSHEY_SIMPLEX, .65, (20, 20, 20), 1)
        columns.append(np.concatenate([header, rgb, colored], axis=0))
    if columns[0].shape != columns[1].shape:
        raise ValueError("전후 PNG 해상도가 다르다")
    success, encoded = cv2.imencode(".png", np.concatenate(columns, axis=1))
    if not success:
        raise ValueError("비교 PNG 인코딩 실패")
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(encoded.tobytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    comparison(args.before, args.after, args.output)


if __name__ == "__main__":
    main()
