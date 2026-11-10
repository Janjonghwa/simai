#!/usr/bin/env python3
"""통합 이미지 빌드 단계에서 시연 가중치를 받아 sha256 을 확인한다 (S15P31A101-577, deploy/tier0/Dockerfile 의 weights-* 단계).

    python bake_weight.py <출력 폴더> <경로> <sha256> <원본> [<경로> <sha256> <원본> ...]

- 원본: https://… 또는 hf:<저장소>@<revision>:<파일>. 경로·해시·원본은 deploy/weights.json 항목과 같아야 한다(시험이 대조한다).
- HF 토큰은 BuildKit 비밀 hf_token(파일 /run/secrets/hf_token)만 읽고, 첫 요청에만 싣는다(HF 가 보내는 다른 호스트 리다이렉트에는 실리지 않는다).
  토큰은 출력·파일·층에 남기지 않는다. 비밀이 없으면 익명으로 받는다(승인제·비공개 저장소는 401·403 으로 빌드가 멈춘다).
- 받은 파일은 <출력 폴더>/<경로> 에 두고, 옆에 <경로>.sha256(해시 한 줄)을 쓴다. simai-bootstrap 이 그 값을 목록 해시와 먼저 대조한다.
  해시가 다르면 받은 것을 지우고 빌드를 멈춘다.
- 출력 폴더 안 파일·폴더의 mtime 은 0 으로 둔다: 캐시를 잃어 다시 받아도 내용이 같으면 같은 층이 되게(같은 다이제스트 = 다시 올리지 않음).
표준 라이브러리만 쓴다(빌드 단계 이미지는 python slim).
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SECRET = Path("/run/secrets/hf_token")
ATTEMPTS = 3


def request(src: str) -> urllib.request.Request:
    if src.startswith("https://"):
        return urllib.request.Request(src)
    if not src.startswith("hf:") or ":" not in src[3:] or "@" not in src[3:].split(":", 1)[0]:
        raise SystemExit(f"원본 형식 오류(https://… 또는 hf:<저장소>@<revision>:<파일>): {src}")
    left, file = src[3:].split(":", 1)
    repo, rev = left.split("@", 1)
    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    req = urllib.request.Request(f"{endpoint}/{repo}/resolve/{rev}/{file}")
    token = SECRET.read_text(encoding="utf-8").strip() if SECRET.is_file() else ""
    if token:
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    return req


def fetch(src: str, dest: Path, want: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f"{dest.name}.part")
    for n in range(1, ATTEMPTS + 1):
        h = hashlib.sha256()
        try:
            with urllib.request.urlopen(request(src), timeout=60) as r, open(part, "wb") as f:  # 목록의 https·HF 주소만
                for chunk in iter(lambda: r.read(1 << 20), b""):
                    h.update(chunk)
                    f.write(chunk)
            break
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise SystemExit(f"{dest}: HTTP {e.code} (HF 저장소면 비밀 hf_token 과 그 계정의 읽기 권한을 확인한다)") from None
            err: Exception = e
        except (urllib.error.URLError, OSError) as e:
            err = e
        if n == ATTEMPTS:
            raise SystemExit(f"{dest}: 받기 실패({ATTEMPTS}회) {type(err).__name__}: {err}")
        print(f"{dest.name} 받기 끊김({type(err).__name__}) → 다시 ({n}/{ATTEMPTS})", file=sys.stderr)
        time.sleep(5 * n)
    got = h.hexdigest()
    if got != want:
        part.unlink()
        raise SystemExit(f"{dest}: 해시 불일치 {got} ≠ 목록 {want}")
    os.replace(part, dest)
    dest.with_name(f"{dest.name}.sha256").write_text(want + "\n", encoding="utf-8")
    print(f"{dest} {want[:12]}… {dest.stat().st_size / 1e9:.2f} GB", file=sys.stderr)


def main(argv: list[str]) -> int:
    if len(argv) < 4 or (len(argv) - 1) % 3:
        print(__doc__, file=sys.stderr)
        return 64
    out = Path(argv[0])
    for i in range(1, len(argv), 3):
        rel, want, src = argv[i], argv[i + 1].lower(), argv[i + 2]
        if Path(rel).is_absolute() or ".." in Path(rel).parts or len(want) != 64:
            raise SystemExit(f"경로·해시 형식 오류: {rel} {want}")
        fetch(src, out / rel, want)
    for p in sorted(out.rglob("*"), reverse=True):  # 파일 먼저, 폴더는 안쪽부터(폴더 mtime 을 나중에 맞춘다)
        os.utime(p, (0, 0))
    os.utime(out, (0, 0))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
