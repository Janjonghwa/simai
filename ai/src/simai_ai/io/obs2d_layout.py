"""2D 관측(observation_2d) Parquet 보관소의 경로 규칙·설정·보관 기간 정리 (명세 11.2, 11.5, 18.4, 요구사항 FR-141).

`observation_2d` 는 EC2 DB 에 쌓지 않고 Tier 0 Pod 와 같은 데이터센터의 RunPod Network Volume 에 Parquet 로 둔다.
이 모듈은 표준 라이브러리만 쓴다(pyarrow 없이 정리 작업을 돌릴 수 있게). 기록·읽기는 `obs2d_parquet`.

경로: `{root}/{dir_template}/obs2d.{camera_id}.{YYYYMMDDTHHMM}Z.{session_id}.{part:04d}.parquet`

- root: 생성자 인자 또는 환경변수 `SIMAI_OBS2D_ROOT`. 둘 다 없으면 오류다(노트북에서 엉뚱한 곳에 쓰지 않게).
  Pod 에서는 Network Volume 아래(예: `/workspace/simai/obs2d`)를 준다.
- dir_template: `{date}`(YYYY-MM-DD), `{hour}`(HH), `{camera_id}`, `{session_id}` 를 쓸 수 있다. 시각은 모두 UTC.
- 파일 이름은 고정 형식이다. 구간 시작 시각(촬영 시각 기준 UTC), 카메라, 세션, 같은 구간 안의 일련번호(part)를 담는다.
  `.` 은 카메라·세션 ID 에 쓸 수 없는 문자라 이름을 거꾸로 해석할 수 있다.
- 쓰는 중인 파일은 `<최종 이름>.inprogress` 다. 닫을 때 최종 이름으로 바꾸며 같은 이름이 있으면 덮어쓰지 않고 오류다.
  읽기는 닫힌 파일만 본다. 프로세스가 죽어 남은 `.inprogress` 는 footer 가 없어 읽을 수 없고, 보관 기간 정리에서 함께 지운다.

보관 기간은 11.5 의 "2D 관측 7~14일" 이다. 기본 14일, 7~14 밖은 받지 않는다. 파일 안 모든 행이 보관 기간을 넘겼을 때만
지운다(구간 시작 + 1시간 + 보관 기간 ≤ 지금). 정리 함수는 지금 시각을 주입받고 기본은 미리보기(dry-run)다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import string
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from simai_ai.tracking.local_tracker import require_camera_id
from simai_ai.tracking.time_model import require_ms

ROOT_ENV = "SIMAI_OBS2D_ROOT"
"""보관소 루트 환경변수. Pod 에서는 Network Volume 아래 경로(예: /workspace/simai/obs2d)"""

MINUTE_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

RETENTION_DAYS_MIN = 7
RETENTION_DAYS_MAX = 14
DEFAULT_RETENTION_DAYS = 14
"""명세 11.2·11.5 의 2D 관측 보관 7~14일. 기본은 보정 재계산에 여유가 있는 14일"""

STORE_HZ_MIN = 5.0
STORE_HZ_MAX = 15.0
DEFAULT_STORE_MAX_HZ = 15.0
"""명세 11.2 저장 5~15 Hz. 카메라마다 이 빈도를 넘는 프레임은 솎아 낸다(처리는 12 FPS 이상, 5.1)"""

DECIMATION_TOLERANCE = 0.2
"""솎기 허용 오차(저장 주기에 대한 비율). 카메라마다 저장 예정 시각을 1000 / store_max_hz 간격 격자로 두고,
촬영 시각이 (예정 시각 − 주기 × 0.2) 이상인 첫 프레임을 저장한다. 프레임 간격 지터로 15 fps 입력이 15 Hz 상한에
걸려 빠지지 않게 하는 여유이고, 격자를 따르므로 긴 구간 평균 저장 빈도는 상한을 넘지 않는다"""

DEFAULT_DIR_TEMPLATE = "{date}/{hour}/{camera_id}"
DEFAULT_ROLL_INTERVAL_MS = HOUR_MS
DEFAULT_ROW_GROUP_ROWS = 20_000
DEFAULT_COMPRESSION = "zstd"

FILE_PREFIX = "obs2d"
FILE_SUFFIX = ".parquet"
TEMP_SUFFIX = ".inprogress"

_TEMPLATE_FIELDS = frozenset({"date", "hour", "camera_id", "session_id"})
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_STAMP = "%Y%m%dT%H%M"


def require_session_id(session_id: object) -> str:
    """세션 ID: 영문·숫자·_·- 1~64자. 파일 이름에 들어가므로 `.`·경로 문자를 막는다."""
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ValueError(f"session_id: 영문·숫자·_·- 로 된 1~64자여야 한다 (받은 값 {session_id!r})")
    return session_id


def require_retention_days(days: object) -> int:
    if isinstance(days, bool) or not isinstance(days, int) or not RETENTION_DAYS_MIN <= days <= RETENTION_DAYS_MAX:
        raise ValueError(
            f"retention_days: 명세 11.5 의 {RETENTION_DAYS_MIN}~{RETENTION_DAYS_MAX}일 안의 정수여야 한다 (받은 값 {days!r})"
        )
    return days


def _check_template(template: str) -> str:
    if not isinstance(template, str) or not template.strip():
        raise ValueError("dir_template 이 비어 있다")
    for _literal, field, spec, conv in string.Formatter().parse(template):
        if field is None:
            continue
        if field not in _TEMPLATE_FIELDS or spec or conv:
            raise ValueError(f"dir_template: {{{field}}} 는 쓸 수 없다. {sorted(_TEMPLATE_FIELDS)} 만 쓴다")
    parts = template.replace("\\", "/").split("/")
    if template.startswith(("/", "\\")) or ":" in template or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"dir_template: root 아래 상대 경로여야 한다 (받은 값 {template!r})")
    return template


def resolve_root(root: Path | str | None = None) -> Path:
    """root 인자, 없으면 환경변수 SIMAI_OBS2D_ROOT. 둘 다 없으면 오류."""
    if root is None:
        env = os.environ.get(ROOT_ENV)
        if not env:
            raise ValueError(f"2D 관측 보관소 루트가 없다: root 인자나 환경변수 {ROOT_ENV} 를 준다")
        root = env
    return Path(root)


@dataclass(frozen=True)
class Obs2DStoreConfig:
    """2D 관측 보관소 설정. 기본값은 명세 11.2·11.5 숫자다.

    roll_interval_ms: 파일 하나가 담는 촬영 시각 구간. 기본 1시간. 1분 단위이고 1시간을 나누어떨어지게 해야 한다
        (파일이 한 시간 디렉터리 안에 들어가게). 줄이면 프로세스가 죽었을 때 잃는 양(닫지 못한 파일)이 줄고 파일 수가 는다.
    row_group_rows: 메모리에 모았다가 Parquet row group 하나로 쓰는 행 수.
    """

    root: Path
    dir_template: str = DEFAULT_DIR_TEMPLATE
    retention_days: int = DEFAULT_RETENTION_DAYS
    store_max_hz: float = DEFAULT_STORE_MAX_HZ
    roll_interval_ms: int = DEFAULT_ROLL_INTERVAL_MS
    row_group_rows: int = DEFAULT_ROW_GROUP_ROWS
    compression: str = DEFAULT_COMPRESSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        _check_template(self.dir_template)
        require_retention_days(self.retention_days)
        hz = self.store_max_hz
        if isinstance(hz, bool) or not isinstance(hz, (int, float)) or not STORE_HZ_MIN <= hz <= STORE_HZ_MAX:
            raise ValueError(f"store_max_hz: 명세 11.2 의 {STORE_HZ_MIN}~{STORE_HZ_MAX} Hz 안이어야 한다 (받은 값 {hz!r})")
        roll = self.roll_interval_ms
        if isinstance(roll, bool) or not isinstance(roll, int) or roll <= 0 or roll % MINUTE_MS or HOUR_MS % roll:
            raise ValueError(f"roll_interval_ms: 1분 단위로 1시간을 나누어떨어지게 해야 한다 (받은 값 {roll!r})")
        if isinstance(self.row_group_rows, bool) or not isinstance(self.row_group_rows, int) or self.row_group_rows < 1:
            raise ValueError(f"row_group_rows: 1 이상 정수여야 한다 (받은 값 {self.row_group_rows!r})")

    @classmethod
    def from_env(cls, root: Path | str | None = None, **overrides: object) -> Obs2DStoreConfig:
        """root 를 주지 않으면 환경변수 SIMAI_OBS2D_ROOT 를 쓴다."""
        return cls(root=resolve_root(root), **overrides)  # type: ignore[arg-type]

    @property
    def store_period_ms(self) -> float:
        """카메라별 저장 주기 (솎기 격자 간격)."""
        return 1000.0 / self.store_max_hz

    def window_start(self, capture_time_ms: int) -> int:
        """촬영 시각이 들어가는 파일 구간의 시작 시각."""
        require_ms("capture_time_ms", capture_time_ms)
        return capture_time_ms - capture_time_ms % self.roll_interval_ms

    def directory(self, camera_id: str, window_start_ms: int, session_id: str) -> Path:
        dt = _utc(window_start_ms)
        rel = self.dir_template.format(
            date=dt.strftime("%Y-%m-%d"), hour=dt.strftime("%H"), camera_id=camera_id, session_id=session_id
        )
        return self.root / rel

    def file_path(self, camera_id: str, window_start_ms: int, session_id: str, part: int) -> Path:
        return self.directory(camera_id, window_start_ms, session_id) / file_name(
            camera_id, window_start_ms, session_id, part
        )


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def file_name(camera_id: str, window_start_ms: int, session_id: str, part: int) -> str:
    """닫힌 파일 이름. 구간 시작은 1분 단위여야 한다."""
    require_camera_id(camera_id)
    require_session_id(session_id)
    require_ms("window_start_ms", window_start_ms)
    if window_start_ms % MINUTE_MS:
        raise ValueError(f"window_start_ms 는 1분 단위여야 한다 (받은 값 {window_start_ms})")
    if isinstance(part, bool) or not isinstance(part, int) or part < 0:
        raise ValueError(f"part: 0 이상 정수여야 한다 (받은 값 {part!r})")
    stamp = _utc(window_start_ms).strftime(_STAMP)
    return f"{FILE_PREFIX}.{camera_id}.{stamp}Z.{session_id}.{part:04d}{FILE_SUFFIX}"


@dataclass(frozen=True)
class StoredFile:
    """보관소의 파일 하나 (이름에서 읽은 정보). in_progress 는 아직 닫히지 않은(또는 죽은 프로세스가 남긴) 파일."""

    path: Path
    camera_id: str
    window_start_ms: int
    session_id: str
    part: int
    in_progress: bool
    size_bytes: int

    @property
    def latest_possible_ms(self) -> int:
        """이 파일의 행이 가질 수 있는 촬영 시각의 상한(배타). 구간 길이는 1시간 이하다."""
        return self.window_start_ms + HOUR_MS


def parse_file_name(name: str) -> tuple[str, int, str, int, bool] | None:
    """파일 이름 → (camera_id, window_start_ms, session_id, part, in_progress). 형식이 아니면 None."""
    in_progress = name.endswith(TEMP_SUFFIX)
    base = name[: -len(TEMP_SUFFIX)] if in_progress else name
    if not base.endswith(FILE_SUFFIX):
        return None
    fields = base[: -len(FILE_SUFFIX)].split(".")
    if len(fields) != 5 or fields[0] != FILE_PREFIX:
        return None
    _, camera_id, stamp, session_id, part = fields
    try:
        require_camera_id(camera_id)
        require_session_id(session_id)
        if not stamp.endswith("Z") or not part.isdigit():
            return None
        dt = datetime.strptime(stamp[:-1], _STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None
    return camera_id, int(dt.timestamp()) * 1000, session_id, int(part), in_progress


def list_files(
    root: Path | str,
    *,
    camera_ids: Iterable[str] | None = None,
    session_ids: Iterable[str] | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    include_in_progress: bool = False,
) -> list[StoredFile]:
    """root 아래 보관소 파일을 (구간 시작, 카메라, 세션, part) 순서로 돌려준다. 이름 형식이 아닌 파일은 무시한다.

    start_ms·end_ms([start, end))를 주면 그 구간과 겹칠 수 있는 파일만 고른다(이름의 구간 시작 기준, 보수적으로 1시간 폭).
    """
    root = Path(root)
    cams = None if camera_ids is None else frozenset(camera_ids)
    sessions = None if session_ids is None else frozenset(session_ids)
    out: list[StoredFile] = []
    if not root.is_dir():
        return out
    for path in root.rglob(f"{FILE_PREFIX}.*"):
        parsed = parse_file_name(path.name)
        if parsed is None or not path.is_file():
            continue
        camera_id, window_start, session_id, part, in_progress = parsed
        if in_progress and not include_in_progress:
            continue
        if cams is not None and camera_id not in cams:
            continue
        if sessions is not None and session_id not in sessions:
            continue
        if end_ms is not None and window_start >= end_ms:
            continue
        if start_ms is not None and window_start + HOUR_MS <= start_ms:
            continue
        out.append(StoredFile(path, camera_id, window_start, session_id, part, in_progress, path.stat().st_size))
    out.sort(key=lambda f: (f.window_start_ms, f.camera_id, f.session_id, f.part, f.in_progress, str(f.path)))
    return out


# --------------------------------------------------------------------------- 보관 기간 정리


@dataclass(frozen=True)
class DeletionReport:
    """삭제(또는 미리보기) 결과. dry_run 이면 files 는 지울 대상이고 실제로 지우지 않았다."""

    dry_run: bool
    files: tuple[StoredFile, ...]
    removed_dirs: tuple[Path, ...] = ()

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    def to_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "file_count": len(self.files),
            "total_bytes": self.total_bytes,
            "files": [str(f.path) for f in self.files],
            "removed_dirs": [str(d) for d in self.removed_dirs],
        }


def plan_expired(root: Path | str, *, now_ms: int, retention_days: int = DEFAULT_RETENTION_DAYS) -> list[StoredFile]:
    """보관 기간이 지난 파일(쓰는 중 이름 포함). 파일 안 모든 행이 now − 보관 기간보다 오래됐을 때만 고른다."""
    require_ms("now_ms", now_ms)
    cutoff = now_ms - require_retention_days(retention_days) * DAY_MS
    return [f for f in list_files(root, include_in_progress=True) if f.latest_possible_ms <= cutoff]


def delete_files(root: Path | str, files: Sequence[StoredFile], *, dry_run: bool = True) -> DeletionReport:
    """files 를 지운다(dry_run 이면 목록만). 지운 뒤 비게 된 root 아래 디렉터리도 지운다(root 자체는 남긴다).

    참여자 삭제 요청(18.4)은 `list_files(root, session_ids=[...], include_in_progress=True)` 로 고른 뒤 이 함수로 지운다.
    """
    root = Path(root).resolve()
    for f in files:
        resolved = f.path.resolve()
        if root not in resolved.parents or parse_file_name(resolved.name) is None:
            raise ValueError(f"{f.path}: 보관소({root}) 안의 2D 관측 파일이 아니다")
    if dry_run:
        return DeletionReport(True, tuple(files))
    parents: set[Path] = set()
    for f in files:
        f.path.unlink(missing_ok=True)
        parents.add(f.path.resolve().parent)
    removed: list[Path] = []
    for d in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        while d != root and root in d.parents:
            try:
                d.rmdir()  # 비어 있을 때만 지워진다
            except OSError:
                break
            removed.append(d)
            d = d.parent
    return DeletionReport(False, tuple(files), tuple(removed))


def purge_expired(
    root: Path | str, *, now_ms: int, retention_days: int = DEFAULT_RETENTION_DAYS, dry_run: bool = True
) -> DeletionReport:
    """보관 기간이 지난 파일을 지운다. 기본은 미리보기(dry_run=True)다. Network Volume 정리 작업(11.5)이 부른다."""
    return delete_files(root, plan_expired(root, now_ms=now_ms, retention_days=retention_days), dry_run=dry_run)


def main(argv: Sequence[str] | None = None) -> int:
    """보관 기간 정리 CLI. 기본은 미리보기이고 --apply 를 줘야 지운다.

    uv run python -m simai_ai.io.obs2d_layout [--root DIR] [--retention-days 14] [--now-ms MS] [--apply]
    """
    parser = argparse.ArgumentParser(description="2D 관측 Parquet 보관 기간 정리 (명세 11.5)")
    parser.add_argument("--root", help=f"보관소 루트. 없으면 환경변수 {ROOT_ENV}")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--now-ms", type=int, help="기준 시각(UTC ms). 없으면 이 명령의 현재 시각")
    parser.add_argument("--apply", action="store_true", help="실제로 지운다 (없으면 대상 목록만)")
    args = parser.parse_args(argv)
    # 시계는 진입점에서만 읽고 정리 함수에는 주입한다 (ai/CONVENTION.md)
    now_ms = args.now_ms if args.now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
    report = purge_expired(
        resolve_root(args.root), now_ms=now_ms, retention_days=args.retention_days, dry_run=not args.apply
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
