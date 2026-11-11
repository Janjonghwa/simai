"""세팅 보조 촬영(S15P31A101-605): 고정 카메라 4대가 못 본 곳을 관리자가 따로 찍은 동영상·사진의 실제 관측으로 메운다.

흐름(작업 폴더 `root` = static_reconstruct·site_setup 과 같은 레이아웃, 고정 카메라만으로 prepare·mapanything 이 끝난 상태):
1. 프레임 고르기(`select_frames`): 동영상은 낮춘 속도로 디코드해 선명도(라플라시안 분산)를 매기고, 흐린 프레임을 버리고,
   최소 간격·총 상한(기본 40, 파일 전체 합)을 지켜 고르고, 거의 같은 프레임(dHash)을 뺀다. 사진은 한 장이 한 후보다.
   EXIF 35 mm 환산 초점 거리가 아주 짧은 사진(초광각·어안)은 미리 뺀다.
2. 묶음(chunk)마다 MapAnything 한 번(`run_supplement`): 고정 카메라 4 view(알려진 K, 사람 마스크) + 보조 프레임(K·자세 모름,
   같은 분할기의 사람 마스크). 기본은 묶음 하나(프레임 ≤ 40). 상한을 올리면 40 장씩 나눠 묶음마다 같은 고정 4 view 를 넣는다.
3. 묶음마다 World 정렬은 고정 카메라 4대만으로 푼다(`reconstruct_tasks.solve_fixed_alignment`, 지금과 같은 규칙). 보조 프레임
   자세는 출력일 뿐이다. 보조 프레임 등록(`register_views`): 고정 view 에서 시작해 깊이가 맞는 겹침으로 이어지는 프레임만 남긴다.
   화각이 아주 넓게 추정된 파일(어안·초광각 의심)은 통째로 뺀다.
4. 품질 관문(`gate_chunk`): 등록 비율이 낮거나, 고정 카메라 중심 정렬 잔차·고정 카메라끼리 재투영이 보조 촬영 없을 때보다
   허용치 넘게 나빠지면 그 묶음만 뺀다. 남는 묶음이 없으면 보조 촬영 없는 맵(지금과 같음) + 사유 코드.
5. 받아들인 묶음을 World 좌표의 한 MapAnything Run 으로 합쳐(`materialize_world_run`) prepared/·mapanything/ 자리에 놓는다
   (원래 것은 *_base 로 옮긴다). 기존 굽기(TSDF + texrecon)가 보조 프레임 깊이·사진을 그대로 형상·텍스처에 쓴다.

AGENTS 규칙: World·보정·위치·판정에는 넣지 않는다(규칙 1). 출처는 `input_role` SETUP_SUPPLEMENT_CAPTURE 와 촬영 시각·기기로 남긴다
(규칙 7). 사람은 마스크로 지우고 생성형 채움을 더하지 않는다. 보조 프레임의 view 이름(`sup_…`)은 작업 폴더 안 열쇠일 뿐 등록 카메라
ID 가 아니다(카메라 연결 관리·스트림에 없다).

선택 의존성(cv2·PIL·torch)은 함수 안에서만 import 한다(기본 설치·nightly 수집에서 이 모듈 import 가 깨지지 않게).
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

INPUT_ROLE = "SETUP_SUPPLEMENT_CAPTURE"
VIEW_ROLE_FIXED = "FIXED_CAMERA"
VIEW_ROLE_SUPPLEMENT = "SETUP_SUPPLEMENT"
SUP_PREFIX = "sup_"  # = calibration.extrinsics.initial.SUPPLEMENT_VIEW_PREFIX(단위 검사 제외 접두, 시험이 같은지 본다)
PREPARED_FORMAT = "simai.mapanything_inputs"
JOINT_FORMAT_VERSION = "0.2.0"  # 0.1.0 + K 없는 view(K_undistorted null)·view_role·supplement 블록
SUPPLEMENT_DIR = "supplement"
REQUEST_FILE = "request.json"
RESULT_FILE = "result.json"

# 사유 코드(계약 site_setup#/$defs/supplement_summary/reason)
R_NO_USABLE_FRAMES = "NO_USABLE_FRAMES"
R_MEDIA_UNREADABLE = "MEDIA_UNREADABLE"
R_MEDIA_DOWNLOAD_FAILED = "MEDIA_DOWNLOAD_FAILED"
R_TOO_FEW_REGISTERED = "TOO_FEW_REGISTERED"
R_ALIGNMENT_WORSE = "ALIGNMENT_WORSE"
R_FIXED_REPROJECTION_WORSE = "FIXED_REPROJECTION_WORSE"
R_RECONSTRUCTION_FAILED = "RECONSTRUCTION_FAILED"
R_BAKE_FAILED = "BAKE_FAILED"
R_PATH_UNSUPPORTED = "PATH_UNSUPPORTED"
R_SEGMENTER_MISSING = "SEGMENTER_MISSING"
R_TIME_BUDGET = "TIME_BUDGET"

# 파일별 상태(계약 supplement_summary.media[].status)
M_USED = "USED"
M_DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
M_UNREADABLE = "UNREADABLE"
M_NO_FRAMES = "NO_FRAMES"
M_NOT_REGISTERED = "NOT_REGISTERED"
M_WIDE_FOV_REJECTED = "WIDE_FOV_REJECTED"

MAX_MEDIA = 32  # 계약 supplement_media maxItems


@dataclass(frozen=True)
class SupplementConfig:
    """수치 근거는 docs/design/S15P31A101-605-map-supplement-capture.md "정한 수치"."""

    max_frames: int = 40  # 모든 파일 합. 40 이면 묶음 하나(기본)
    chunk_frames: int = 40  # 묶음 하나의 보조 프레임 상한(고정 4 view 별도)
    bridge_frames: int = 4  # 다음 묶음에 앞 묶음의 등록된 마지막 프레임을 이만큼 다시 넣어 겹침을 잇는다
    time_budget_s: float = 600.0  # 이 시간을 넘으면 묶음을 더 돌리지 않는다(첫 묶음은 돈다)
    sample_fps: float = 2.0  # 동영상 후보 디코드 속도(초당)
    min_spacing_s: float = 0.5  # 같은 동영상에서 고른 프레임 사이 최소 간격
    blur_ratio: float = 0.35  # 같은 파일(사진은 사진끼리) 선명도 중앙값의 이 배 미만은 흐림으로 버린다
    dup_hamming: int = 5  # dHash 64비트 해밍 거리 ≤ 이면 거의 같은 프레임
    score_long_side: int = 640  # 선명도·해시를 재는 축소 해상도(긴 변)
    wide_exif_35mm: float = 16.0  # EXIF 35 mm 환산 초점 거리 < 이면 초광각·어안으로 보고 뺀다(가로 화각 약 97° 초과)
    wide_fov_deg: float = 100.0  # MapAnything 이 추정한 가로 화각 중앙값 > 이면 그 파일을 뺀다
    min_registered_ratio: float = 0.30  # 등록된 보조 프레임 / 넣은 보조 프레임 하한
    align_worse_m: float = 0.02  # 고정 카메라 중심 잔차(중심만 맞춘 similarity)가 기준보다 이만큼 넘게 커지면 거부
    reproj_worse_ratio: float = 1.25  # 고정 카메라끼리 왕복 재투영 중앙값이 기준×이 값 + reproj_worse_px 를 넘으면 거부
    reproj_worse_px: float = 0.5
    reg_samples: int = 2000  # 등록 검사에서 view 하나에서 뽑는 점 수
    reg_depth_tol: float = 0.05  # 깊이 일치 상대 허용치(cross_view_reprojection 과 같은 값)
    reg_min_agree: int = 150  # 깊이가 맞는 점 수 하한(2000 점 중 7.5%)
    reg_min_agree_ratio: float = 0.5  # 깊이 맞음 / (맞음 + 빈 공간 위반) 하한(가린 점은 세지 않는다)
    mask_dilate_px: int = 31  # 사람 마스크 팽창(prepare 와 같은 값)
    seed: int = 0

    @classmethod
    def from_env(cls, env: Mapping[str, str], **overrides: Any) -> SupplementConfig:
        """SIMAI_SUPPLEMENT_MAX_FRAMES(1~240)·SIMAI_SUPPLEMENT_CHUNK_FRAMES(1~60)·SIMAI_SUPPLEMENT_TIME_BUDGET_S(>0)."""
        kw: dict[str, Any] = {}
        for key, name, lo, hi, cast in (("SIMAI_SUPPLEMENT_MAX_FRAMES", "max_frames", 1, 240, int),
                                        ("SIMAI_SUPPLEMENT_CHUNK_FRAMES", "chunk_frames", 1, 60, int),
                                        ("SIMAI_SUPPLEMENT_TIME_BUDGET_S", "time_budget_s", 1, 7200, float)):
            raw = (env.get(key) or "").strip()
            if not raw:
                continue
            try:
                value = cast(raw)
            except ValueError:
                raise ValueError(f"{key} 는 숫자다") from None
            if not lo <= value <= hi:
                raise ValueError(f"{key} 는 {lo}~{hi}")
            kw[name] = value
        kw.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kw)


@dataclass(frozen=True)
class MediaInput:
    """보조 촬영 파일 하나(계약 supplement_media 항목 + 받은 파일 경로)."""

    media_id: str
    kind: str  # VIDEO | IMAGE
    path: Path | None  # 받지 못했으면 None
    captured_at_ms: int | None = None
    source: str | None = None
    device: Mapping[str, str] | None = None
    sha256: str | None = None
    status: str | None = None  # 미리 정해진 상태(DOWNLOAD_FAILED 등)

    def provenance(self) -> dict[str, Any]:
        out: dict[str, Any] = {"media_id": self.media_id, "kind": self.kind}
        for key in ("captured_at_ms", "source", "sha256"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        if self.device:
            out["device"] = dict(self.device)
        return out


@dataclass
class Candidate:
    media_id: str
    index: int  # 동영상 프레임 번호(사진은 0)
    t_s: float | None
    sharpness: float
    dhash: int


@dataclass
class SelectedFrame:
    media_id: str
    index: int
    t_s: float | None
    sharpness: float
    image: np.ndarray  # BGR, 원본 해상도

    @property
    def name(self) -> str:
        # 끝 조각이 숫자가 아니어야 cameras.json 자동 짝짓기(cam_A_01 → cam_A)가 프레임들을 한 카메라로 묶지 않는다
        return f"{SUP_PREFIX}{self.media_id}_f{self.index:06d}"


@dataclass
class Selection:
    frames: list[SelectedFrame]
    media: dict[str, dict[str, Any]]  # media_id → 통계(status, candidates, selected, …)


# --------------------------------------------------------------------------- 프레임 점수
def _gray_small(image: np.ndarray, long_side: int) -> np.ndarray:
    import cv2

    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    s = long_side / max(h, w)
    if s < 1.0:
        gray = cv2.resize(gray, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
    return gray


def sharpness(gray: np.ndarray) -> float:
    """라플라시안 분산(초점·흔들림 흐림 지표). 축소 해상도에서 잰다(해상도가 다른 파일끼리도 비교되게)."""
    import cv2

    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def dhash(gray: np.ndarray) -> int:
    """64비트 차이 해시(9×8 축소, 가로 이웃 밝기 비교)."""
    import cv2

    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA).astype(np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int(sum(1 << i for i, b in enumerate(bits) if b))


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _score(media_id: str, index: int, t_s: float | None, image: np.ndarray, cfg: SupplementConfig) -> Candidate:
    gray = _gray_small(image, cfg.score_long_side)
    return Candidate(media_id, index, t_s, sharpness(gray), dhash(gray))


def _scan_video(media: MediaInput, cfg: SupplementConfig) -> tuple[list[Candidate], dict[str, Any]]:
    """낮춘 속도(cfg.sample_fps)로 프레임을 골라 점수만 매긴다(원본 프레임은 들고 있지 않는다)."""
    import cv2

    cap = cv2.VideoCapture(str(media.path))
    if not cap.isOpened():
        raise ValueError("동영상을 열지 못함")
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if not math.isfinite(fps) or fps <= 0 or fps > 480:
            fps = 30.0
        step = max(1, round(fps / cfg.sample_fps))
        cands: list[Candidate] = []
        idx, size = 0, None
        while cap.grab():
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if ok and frame is not None:
                    size = (int(frame.shape[1]), int(frame.shape[0]))
                    cands.append(_score(media.media_id, idx, idx / fps, frame, cfg))
            idx += 1
    finally:
        cap.release()
    return cands, {"fps": round(fps, 3), "frames": idx, "duration_s": round(idx / fps, 3),
                   "image_size": list(size) if size else None}


def _read_video_frames(path: Path, indices: Iterable[int]) -> dict[int, np.ndarray]:
    """고른 프레임 번호만 다시 디코드한다(순차 읽기, 코덱마다 다른 탐색 오차를 피한다)."""
    import cv2

    want = set(indices)
    out: dict[int, np.ndarray] = {}
    if not want:
        return out
    cap = cv2.VideoCapture(str(path))
    try:
        idx, last = 0, max(want)
        while idx <= last and cap.grab():
            if idx in want:
                ok, frame = cap.retrieve()
                if ok and frame is not None:
                    out[idx] = frame
            idx += 1
    finally:
        cap.release()
    return out


def read_image(path: Path) -> np.ndarray | None:
    """사진 → BGR. EXIF 회전을 반영한다(cv2.imread 기본). HEIC 는 cv2 가 못 읽어 None(범위 밖, 설계 문서)."""
    import cv2

    try:
        data = np.frombuffer(Path(path).read_bytes(), dtype=np.uint8)
    except OSError:
        return None
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    return image if image is not None and image.size else None


def exif_focal_35mm(path: Path) -> float | None:
    """EXIF FocalLengthIn35mmFilm(0xA405). 읽지 못하거나 없으면 None. Pillow 가 없으면 None(검사 생략)."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            value = exif.get_ifd(0x8769).get(0xA405) or exif.get(0xA405)
    except Exception:  # noqa: BLE001 - 메타데이터는 힌트일 뿐이라 어떤 실패도 검사 생략으로 다룬다
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _allocate(total: int, counts: Mapping[str, int]) -> dict[str, int]:
    """총 상한을 파일별 후보 수에 비례해 나눈다. 후보가 있는 파일은 (상한이 허락하는 한) 적어도 1장."""
    live = {k: int(v) for k, v in counts.items() if v > 0}
    if sum(live.values()) <= total:
        return dict(live)
    order = sorted(live, key=lambda k: -live[k])
    alloc = {k: 0 for k in live}
    for k in order[:total]:
        alloc[k] = 1
    rest = total - sum(alloc.values())
    if rest > 0:
        room = {k: live[k] - alloc[k] for k in live}
        tot_room = sum(room.values())
        shares = {k: rest * room[k] / tot_room for k in live} if tot_room else {}
        for k, s in shares.items():
            alloc[k] += int(math.floor(s))
        left = total - sum(alloc.values())
        for k in sorted(shares, key=lambda k: -(shares[k] - math.floor(shares[k]))):
            if left <= 0:
                break
            if alloc[k] < live[k]:
                alloc[k] += 1
                left -= 1
    return alloc


def _pick_spaced(cands: Sequence[Candidate], n: int, spacing_s: float) -> list[Candidate]:
    """선명한 순서로 최소 간격을 지키며 n 장(동영상)."""
    chosen: list[Candidate] = []
    for c in sorted(cands, key=lambda c: -c.sharpness):
        if len(chosen) >= n:
            break
        if c.t_s is None or all(abs(c.t_s - o.t_s) >= spacing_s for o in chosen if o.t_s is not None):
            chosen.append(c)
    return chosen


def select_frames(media: Sequence[MediaInput], cfg: SupplementConfig) -> Selection:
    """보조 촬영 파일들 → 복원에 넣을 프레임(총 cfg.max_frames 이하). 파일 하나의 실패는 그 파일 상태로만 남긴다."""
    stats: dict[str, dict[str, Any]] = {}
    per_media: dict[str, list[Candidate]] = {}
    image_cache: dict[str, np.ndarray] = {}
    for m in media:
        st: dict[str, Any] = {"kind": m.kind, "status": m.status, "candidates": 0, "selected": 0,
                              "blurry_dropped": 0, "duplicates_dropped": 0}
        stats[m.media_id] = st
        if m.status is not None or m.path is None:
            st["status"] = m.status or M_DOWNLOAD_FAILED
            continue
        if m.kind == "IMAGE":
            f35 = exif_focal_35mm(m.path)
            st["exif_focal_35mm"] = f35
            if f35 is not None and f35 < cfg.wide_exif_35mm:
                st["status"] = M_WIDE_FOV_REJECTED
                continue
            image = read_image(m.path)
            if image is None:
                st["status"] = M_UNREADABLE
                continue
            image_cache[m.media_id] = image
            st["image_size"] = [int(image.shape[1]), int(image.shape[0])]
            cands = [_score(m.media_id, 0, None, image, cfg)]
        else:
            try:
                cands, meta = _scan_video(m, cfg)
            except (ValueError, OSError):
                st["status"] = M_UNREADABLE
                continue
            st.update(meta)
            if not cands:
                st["status"] = M_UNREADABLE if not meta.get("frames") else M_NO_FRAMES
                continue
            thr = cfg.blur_ratio * float(np.median([c.sharpness for c in cands]))
            keep = [c for c in cands if c.sharpness >= thr] or [max(cands, key=lambda c: c.sharpness)]
            st["blurry_dropped"] = len(cands) - len(keep)
            cands = keep
        st["candidates"] = len(cands)
        per_media[m.media_id] = cands
    # 사진끼리 흐림 거르기(3장 이상일 때만, 기준이 될 만큼 모였을 때)
    image_ids = [mid for mid in per_media if stats[mid]["kind"] == "IMAGE"]
    if len(image_ids) >= 3:
        thr = cfg.blur_ratio * float(np.median([per_media[mid][0].sharpness for mid in image_ids]))
        for mid in image_ids:
            if per_media[mid][0].sharpness < thr:
                stats[mid].update(status=M_NO_FRAMES, blurry_dropped=1, candidates=0)
                del per_media[mid]
    alloc = _allocate(cfg.max_frames, {k: len(v) for k, v in per_media.items()})
    chosen: list[Candidate] = []
    for mid, cands in per_media.items():
        n = alloc.get(mid, 0)
        if n <= 0:
            continue
        dur = stats[mid].get("duration_s") or 0.0
        spacing = max(cfg.min_spacing_s, 0.6 * dur / n) if stats[mid]["kind"] == "VIDEO" else 0.0
        chosen.extend(_pick_spaced(cands, n, spacing))
    kept: list[Candidate] = []
    for c in sorted(chosen, key=lambda c: -c.sharpness):
        if any(hamming(c.dhash, k.dhash) <= cfg.dup_hamming for k in kept):
            stats[c.media_id]["duplicates_dropped"] += 1
            continue
        kept.append(c)
    by_media: dict[str, list[Candidate]] = {}
    for c in kept:
        by_media.setdefault(c.media_id, []).append(c)
    frames: list[SelectedFrame] = []
    for m in media:  # 입력 순서 → 시간 순서(묶음이 이웃 프레임끼리 묶이게)
        cs = sorted(by_media.get(m.media_id, []), key=lambda c: c.index)
        if not cs:
            if stats[m.media_id]["status"] is None:
                stats[m.media_id]["status"] = M_NO_FRAMES
            continue
        if m.kind == "IMAGE":
            images = {0: image_cache[m.media_id]}
        else:
            images = _read_video_frames(m.path, [c.index for c in cs])
        for c in cs:
            if c.index in images:
                frames.append(SelectedFrame(c.media_id, c.index, c.t_s, c.sharpness, images[c.index]))
        stats[m.media_id]["selected"] = sum(1 for f in frames if f.media_id == m.media_id)
        if stats[m.media_id]["selected"] == 0 and stats[m.media_id]["status"] is None:
            stats[m.media_id]["status"] = M_UNREADABLE
    return Selection(frames, stats)


# --------------------------------------------------------------------------- 입력 폴더
def normalize_frame(image: np.ndarray, target_wh: Sequence[int]) -> np.ndarray:
    """고정 카메라 영상과 같은 화면비로 가운데를 자르고, 더 크면 고정 카메라 크기로 줄인다(키우지 않는다).

    MapAnything 은 한 번에 넣는 view 를 같은 모델 해상도로 맞춘다. 화면비가 다른 프레임(세로 영상 등)이 섞이면 고정 카메라 영상까지
    잘리므로 보조 프레임 쪽을 맞춘다. K 를 모르므로 자르기는 기하를 해치지 않는다(주점만 옮겨 간다)."""
    import cv2

    tw, th = int(target_wh[0]), int(target_wh[1])
    h, w = image.shape[:2]
    target_ar = tw / th
    if w / h > target_ar:
        nw = max(1, round(h * target_ar))
        x0 = (w - nw) // 2
        image = image[:, x0:x0 + nw]
    else:
        nh = max(1, round(w / target_ar))
        y0 = (h - nh) // 2
        image = image[y0:y0 + nh, :]
    h, w = image.shape[:2]
    if w > tw:
        image = cv2.resize(image, (tw, max(1, round(h * tw / w))), interpolation=cv2.INTER_AREA)
    return np.ascontiguousarray(image)


def _copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def build_joint_prepared(
    base_prepared: Path,
    out_dir: Path,
    frames: Sequence[SelectedFrame],
    media: Mapping[str, MediaInput],
    person_mask: Callable[[np.ndarray], np.ndarray | None] | None,
    *,
    mask_dilate_px: int = 31,
) -> dict[str, Any]:
    """고정 카메라 prepared(알려진 K·마스크) + 보조 프레임(K 없음·같은 분할기 마스크) → 한 MapAnything 입력 폴더.

    고정 view 가 앞(MapAnything 기준 view = 첫 view 가 고정 카메라)에 온다. person_mask(BGR) → bool 마스크(사람 = True) 또는 None."""
    import cv2

    base = json.loads((base_prepared / "prepared.json").read_text(encoding="utf-8"))
    if base.get("format") != PREPARED_FORMAT:
        raise ValueError("고정 카메라 prepared.json 형식이 아니다")
    out_dir.mkdir(parents=True, exist_ok=False)
    views: list[dict[str, Any]] = []
    for v in base["views"]:
        if v.get("K_undistorted") is None:
            raise ValueError(f"고정 카메라 {v['name']} 의 K 가 없다")
        _copy(base_prepared / v["image"], out_dir / v["image"])
        if v.get("mask"):
            _copy(base_prepared / v["mask"], out_dir / v["mask"])
        views.append({**v, "view_role": VIEW_ROLE_FIXED})
    target_wh = base["views"][0]["image_size"]
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (mask_dilate_px, mask_dilate_px))
    for f in frames:
        image = normalize_frame(f.image, target_wh)
        name = f.name
        if not cv2.imwrite(str(out_dir / f"{name}.jpg"), image, [cv2.IMWRITE_JPEG_QUALITY, 95]):
            raise OSError("보조 프레임 쓰기 실패")
        mask_name = None
        if person_mask is not None:
            mask = person_mask(image)
            if mask is not None:
                mask = cv2.dilate(np.asarray(mask, dtype=np.uint8), kernel)
                mask_name = f"{name}.mask.png"
                if not cv2.imwrite(str(out_dir / mask_name), mask * 255):
                    raise OSError("보조 프레임 마스크 쓰기 실패")
        m = media[f.media_id]
        views.append({
            "name": name, "camera_id": name, "view_role": VIEW_ROLE_SUPPLEMENT,
            "image": f"{name}.jpg", "mask": mask_name, "image_size": [int(image.shape[1]), int(image.shape[0])],
            "K_undistorted": None, "k_source": "UNKNOWN",
            "source_media": {"media_id": f.media_id, "kind": m.kind, "frame_index": f.index,
                             "t_s": None if f.t_s is None else round(f.t_s, 3), "captured_at_ms": m.captured_at_ms,
                             "original_size": [int(f.image.shape[1]), int(f.image.shape[0])]},
        })
    doc = {**base, "format_version": JOINT_FORMAT_VERSION, "views": views,
           "person_masks": bool(base.get("person_masks")) or person_mask is not None,
           "supplement": {"input_role": INPUT_ROLE, "frames": len(frames),
                          "media": [media[mid].provenance() for mid in dict.fromkeys(f.media_id for f in frames)]}}
    (out_dir / "prepared.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


# --------------------------------------------------------------------------- 등록·관문
def _agreement(vi: Any, vj: Any, n: int, tol: float, rng: np.random.Generator) -> tuple[int, int]:
    """vi 의 점을 vj 에 투영해 (깊이가 맞는 점 수, 맞음 + 빈 공간 위반 점 수).

    vj 가 본 표면보다 앞(vj 와 그 표면 사이 빈 공간)에 놓인 점은 위반이다. vj 표면보다 뒤인 점은 가림일 수 있어 세지 않는다
    (낮은 손 기기와 높은 고정 카메라는 서로 가리는 곳이 많다)."""
    v_idx, u_idx = np.nonzero(vi.valid)
    if len(u_idx) == 0:
        return 0, 0
    pick = rng.choice(len(u_idx), min(n, len(u_idx)), replace=False)
    m = np.zeros_like(vi.valid)
    m[v_idx[pick], u_idx[pick]] = True
    X, _ = vi.points(m)
    T = np.asarray(vj.cam2world, float)
    Xc = (X - T[:3, 3]) @ T[:3, :3]
    z = Xc[:, 2]
    front = z > 1e-6
    if not front.any():
        return 0, 0
    q = (np.asarray(vj.K, float) @ (Xc[front] / z[front, None]).T).T
    h, w = vj.depth.shape
    ui, vi_ = np.round(q[:, 0]).astype(int), np.round(q[:, 1]).astype(int)
    inside = (ui >= 0) & (ui < w) & (vi_ >= 0) & (vi_ < h)
    ui, vi_, zf = ui[inside], vi_[inside], z[front][inside]
    ok = vj.valid[vi_, ui]
    if not ok.any():
        return 0, 0
    dj = vj.depth[vi_[ok], ui[ok]]
    zk = zf[ok]
    agree = np.abs(zk - dj) <= tol * np.maximum(dj, 1e-6)
    violate = zk < (1.0 - tol) * dj
    return int(agree.sum()), int(agree.sum() + violate.sum())


def register_views(views: Sequence[Any], fixed_names: set[str], cfg: SupplementConfig) -> dict[str, dict[str, Any]]:
    """보조 view 등록: 고정 view 에서 시작해, 이미 등록된 view 와 깊이가 맞는 겹침(점 수·비율 하한)이 있는 view 를 차례로 더한다.

    MapAnything 은 모든 view 에 자세를 내므로 '등록'은 그 자세가 고정 카메라 기하와 이어지는지로 판단한다. 고정 카메라와 직접
    겹치지 않아도 등록된 보조 프레임을 거쳐 이어지면 등록이다(촬영은 고정 카메라도 보는 곳에서 시작하라는 안내의 근거)."""
    rng = np.random.default_rng(cfg.seed)
    by_name = {v.name: v for v in views}
    registered = [n for n in by_name if n in fixed_names]
    pending = [n for n in by_name if n not in fixed_names]
    out: dict[str, dict[str, Any]] = {n: {"registered": False, "best_agree": 0, "best_ratio": 0.0, "via": None}
                                      for n in pending}
    changed = True
    while changed and pending:
        changed = False
        for n in list(pending):
            rec = out[n]
            for r in registered:
                a, k = _agreement(by_name[n], by_name[r], cfg.reg_samples, cfg.reg_depth_tol, rng)
                ratio = a / k if k else 0.0
                if a > rec["best_agree"]:
                    rec["best_agree"], rec["best_ratio"] = a, round(ratio, 4)
                if a >= cfg.reg_min_agree and ratio >= cfg.reg_min_agree_ratio:
                    rec.update(registered=True, via=r, best_agree=a, best_ratio=round(ratio, 4))
                    registered.append(n)
                    pending.remove(n)
                    changed = True
                    break
    return out


def hfov_deg(view: Any) -> float:
    w = view.depth.shape[1]
    return float(np.degrees(2.0 * np.arctan(w / 2.0 / float(view.K[0, 0]))))


def gate_chunk(
    *,
    selected: int,
    registered: int,
    baseline_center_m: float | None,
    chunk_center_m: float | None,
    baseline_px: float | None,
    chunk_px: float | None,
    cfg: SupplementConfig,
) -> str | None:
    """묶음 하나의 관문. 통과면 None, 아니면 사유 코드(먼저 걸린 것). 모르는 값은 통과로 채우지 않는다."""
    if selected <= 0 or registered <= 0 or registered < cfg.min_registered_ratio * selected:
        return R_TOO_FEW_REGISTERED
    if chunk_center_m is None or not math.isfinite(chunk_center_m):
        return R_ALIGNMENT_WORSE
    if baseline_center_m is not None and chunk_center_m > baseline_center_m + cfg.align_worse_m:
        return R_ALIGNMENT_WORSE
    if baseline_px is not None:
        if chunk_px is None or chunk_px > cfg.reproj_worse_ratio * baseline_px + cfg.reproj_worse_px:
            return R_FIXED_REPROJECTION_WORSE
    return None


def fixed_reprojection_px(views: Sequence[Any], fixed_names: set[str]) -> float | None:
    """고정 카메라끼리 MapAnything 왕복 재투영(모델 px) 카메라별 중앙값의 중앙값. 깊이·자세의 카메라 간 일관성 지표."""
    from simai_ai.calibration.world_frame.inputs import MapAnythingRun, cross_view_reprojection

    fixed = tuple(v for v in views if v.name in fixed_names)
    if len(fixed) < 2:
        return None
    rec = cross_view_reprojection(MapAnythingRun(fixed, {}))
    meds = [c["median_model_px"] for c in rec["cameras"].values() if c and c.get("median_model_px") is not None]
    return round(float(np.median(meds)), 4) if meds else None


def centers_only_residual(root: Path, ma_dir: Path, prepared_dir: Path) -> float | None:
    """고정 카메라 중심만 맞춘 similarity(바닥 제약 끔)의 최대 중심 잔차(m). 바닥 가중치 예산에 끌리지 않는 같은 잣대로 비교한다."""
    from .reconstruct import ReconstructionError
    from .reconstruct_tasks import solve_fixed_alignment

    try:
        _, doc = solve_fixed_alignment(root, ma_dir=ma_dir, prepared_dir=prepared_dir, floor_constraint=False,
                                       max_center_m=math.inf, max_rotation_deg=180.0)
    except ReconstructionError:
        return None
    return round(float(doc["max_center_residual_m"]), 5)


# --------------------------------------------------------------------------- 병합
def model_k_to_image_k(K_model: np.ndarray, image_wh: Sequence[int], model_hw: Sequence[int]) -> np.ndarray:
    """모델 해상도 K → 입력 영상(펴진 해상도) K. intrinsics_check.expected_crop_resize_k 의 역(덮도록 줄이고 가운데 자르기)."""
    W, H = float(image_wh[0]), float(image_wh[1])
    h, w = float(model_hw[0]), float(model_hw[1])
    s = max(w / W, h / H)
    K = np.asarray(K_model, float).copy()
    out = np.eye(3)
    out[0, 0], out[1, 1] = K[0, 0] / s, K[1, 1] / s
    out[0, 2] = (K[0, 2] + 0.5 + (W * s - w) / 2) / s - 0.5
    out[1, 2] = (K[1, 2] + 0.5 + (H * s - h) / 2) / s - 0.5
    return out


@dataclass
class ChunkResult:
    index: int
    names: list[str]  # 넣은 보조 view 이름
    prepared_dir: Path
    ma_dir: Path
    M: np.ndarray | None = None
    align_doc: dict[str, Any] | None = None
    center_m: float | None = None
    fixed_px: float | None = None
    registration: dict[str, dict[str, Any]] = field(default_factory=dict)
    wide_media: set[str] = field(default_factory=set)
    reason: str | None = None
    seconds: float = 0.0

    @property
    def kept(self) -> list[str]:
        return [n for n in self.names if self.registration.get(n, {}).get("registered")
                and _media_of(n, self.prepared_views) not in self.wide_media]

    prepared_views: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        return {"chunk": self.index, "frames": len(self.names), "registered": len(self.kept), "reason": self.reason,
                "center_residual_m": self.center_m, "fixed_reprojection_px": self.fixed_px,
                "wide_fov_media": sorted(self.wide_media), "seconds": round(self.seconds, 2),
                "floor_constrained_center_residual_m": None if not self.align_doc
                else round(float(self.align_doc["max_center_residual_m"]), 5)}


def _media_of(name: str, prepared_views: Mapping[str, Mapping[str, Any]]) -> str | None:
    v = prepared_views.get(name) or {}
    return (v.get("source_media") or {}).get("media_id")


def materialize_world_run(
    root: Path,
    chunks: Sequence[ChunkResult],
    out_prepared: Path,
    out_ma: Path,
) -> dict[str, Any]:
    """받아들인 묶음들 → World 좌표의 한 MapAnything Run + 그 입력 폴더 + 항등 world_alignment 문서.

    묶음 k 의 view 는 그 묶음의 고정 카메라 정렬 M_k(= s_k·Q_k, b_k)로 World 에 옮긴다: 깊이 × s_k, 자세 회전 Q_k·R, 중심 M_k(C).
    고정 카메라 view 는 첫 묶음의 것을 쓴다(그 묶음 보조 프레임과 같은 추론이라 서로 맞다). 보조 view 는 처음 나온 묶음의 것만
    (다리 프레임 중복 제거). 보조 view 의 K 는 모델 출력 K 를 입력 영상 해상도로 되돌려 채운다(k_source MODEL_ESTIMATE).
    같은 동영상 프레임이 한 렌즈라도 MapAnything 에 'K 공유'를 넘길 방법이 없어 프레임마다 따로 추정된 K 를 쓴다(설계 문서)."""
    from .reconstruct import sha256

    out_prepared.mkdir(parents=True, exist_ok=False)
    out_ma.mkdir(parents=True, exist_ok=False)
    first = chunks[0]
    first_prep = json.loads((first.prepared_dir / "prepared.json").read_text(encoding="utf-8"))
    fixed_entries = [v for v in first_prep["views"] if v.get("view_role") == VIEW_ROLE_FIXED]
    views_out: list[dict[str, Any]] = []
    cams_out: list[dict[str, Any]] = []
    raw_out: dict[str, np.ndarray] = {}
    names: list[str] = []
    seen: set[str] = set()
    chunk_meta = []
    model_hw = None
    for ch in chunks:
        prep = json.loads((ch.prepared_dir / "prepared.json").read_text(encoding="utf-8"))
        pv = {v["name"]: v for v in prep["views"]}
        cams = {v["name"]: v for v in json.loads((ch.ma_dir / "cameras.json").read_text(encoding="utf-8"))["views"]}
        M = np.asarray(ch.M, float)
        s = float(np.cbrt(np.linalg.det(M[:3, :3])))
        Q = M[:3, :3] / s
        with np.load(ch.ma_dir / "raw.npz") as raw:
            want = ([v["name"] for v in fixed_entries] if ch is first else []) + [n for n in ch.kept if n not in seen]
            for name in want:
                T = np.asarray(cams[name]["cam2world"], float)
                Tw = np.eye(4)
                Tw[:3, :3] = Q @ T[:3, :3]
                Tw[:3, 3] = M[:3, :3] @ T[:3, 3] + M[:3, 3]
                depth = raw[f"{name}__depth_z"].astype(np.float32) * np.float32(s)
                K_model = raw[f"{name}__intrinsics"]
                raw_out[f"{name}__depth_z"] = depth
                raw_out[f"{name}__conf"] = raw[f"{name}__conf"]
                raw_out[f"{name}__mask"] = raw[f"{name}__mask"]
                raw_out[f"{name}__intrinsics"] = K_model
                raw_out[f"{name}__camera_pose"] = Tw.astype(np.float32)
                hw = list(cams[name]["model_input_hw"])
                model_hw = model_hw or hw
                cams_out.append({"name": name, "model_input_hw": hw, "intrinsics_model_res": np.asarray(K_model).tolist(),
                                 "cam2world": Tw.tolist(), "metric_scaling_factor": None,
                                 "fov_x_deg": round(float(np.degrees(2 * np.arctan(hw[1] / 2 / float(K_model[0, 0])))), 2),
                                 "chunk": ch.index})
                entry = dict(pv[name])
                _copy(ch.prepared_dir / entry["image"], out_prepared / entry["image"])
                if entry.get("mask"):
                    _copy(ch.prepared_dir / entry["mask"], out_prepared / entry["mask"])
                if entry.get("view_role") == VIEW_ROLE_SUPPLEMENT:
                    entry["K_undistorted"] = model_k_to_image_k(K_model, entry["image_size"], hw).tolist()
                    entry["k_source"] = "MODEL_ESTIMATE"
                    entry["chunk"] = ch.index
                    seen.add(name)
                views_out.append(entry)
                names.append(name)
        chunk_meta.append({**ch.record(), "mapanything_to_world": M.tolist()})
    sup_media = [v["source_media"]["media_id"] for v in views_out if v.get("view_role") == VIEW_ROLE_SUPPLEMENT]
    prep_doc = {**first_prep, "views": views_out,
                "supplement": {**(first_prep.get("supplement") or {}), "input_role": INPUT_ROLE,
                               "frames": len(sup_media), "chunks": [c["chunk"] for c in chunk_meta],
                               "frame_coordinates": "WORLD (materialized, S15P31A101-605)"}}
    # 실제로 쓴 파일만 출처에 남긴다(받아들인 묶음에 프레임이 하나라도 남은 파일)
    used = set(sup_media)
    media_all = {}
    for ch in chunks:
        prep = json.loads((ch.prepared_dir / "prepared.json").read_text(encoding="utf-8"))
        for m in (prep.get("supplement") or {}).get("media", []):
            media_all[m["media_id"]] = m
    prep_doc["supplement"]["media"] = [m for mid, m in media_all.items() if mid in used]
    prep_raw = json.dumps(prep_doc, ensure_ascii=False, indent=1).encode("utf-8")
    (out_prepared / "prepared.json").write_bytes(prep_raw)
    cams_doc = {"run_id": "supplement-world", "views": cams_out,
                "convention": "cam2world 4x4, OpenCV, simai World m (supplement merged, S15P31A101-605)"}
    cams_raw = json.dumps(cams_doc, indent=1).encode("utf-8")
    (out_ma / "cameras.json").write_bytes(cams_raw)
    np.savez_compressed(out_ma / "raw.npz", names=np.array(names), **raw_out)
    m0 = json.loads((first.ma_dir / "metrics.json").read_text(encoding="utf-8"))
    metrics = {k: m0.get(k) for k in ("model_version", "weights_hash", "env_hash", "git_commit", "gpu", "infer_kwargs", "resize")}
    metrics.update({
        "run_id": "supplement-world", "experiment_id": "VAL-01", "e_gate": None, "jira_key": "S15P31A101-605",
        "result": "SUCCESS", "args": {"mode": "prepared"}, "model_input_hw": model_hw,
        "prepared_sha256": sha256(prep_raw),
        "images": [v["image"] for v in views_out],
        "supplement_merge": {"chunks": chunk_meta,
                             "inference_prepared_sha256": [json.loads((c.ma_dir / "metrics.json").read_text(encoding="utf-8")).get("prepared_sha256") for c in chunks],
                             "note": "묶음별 MapAnything 출력을 고정 카메라 정렬로 World 에 옮겨 합친 파생 Run(깊이 m, 자세 World)"},
    })
    (out_ma / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    cal = json.loads((root / "calibration.json").read_text(encoding="utf-8"))
    align = {"format": "simai.world_alignment", "status": "OK", "cameras_json_sha256": sha256(cams_raw),
             "mapanything_to_world": np.eye(4).tolist(), "calibration": cal, "scale": 1.0,
             "max_center_residual_m": float(first.align_doc["max_center_residual_m"]) if first.align_doc else None,
             "supplement": {"chunks": chunk_meta, "note": "Run 이 이미 World 좌표다(묶음별 고정 카메라 정렬을 적용함)"}}
    for key in ("floor_constrained", "floor_w_normal", "floor_tilt_deg", "floor_z_median_m", "floor_z_iqr_m"):
        if first.align_doc and key in first.align_doc:
            align[key] = first.align_doc[key]
    return align


# --------------------------------------------------------------------------- 단계
BASE_SUFFIX = "_base"


def swap_in(root: Path, prepared: Path, ma: Path, align_doc: Mapping[str, Any]) -> None:
    """원래 prepared/·mapanything/·world_alignment.json 을 *_base 로 옮기고 병합본을 그 자리에 둔다(되돌리기 restore_baseline)."""
    from .reconstruct import write_json

    for name in ("prepared", "mapanything"):
        (root / name).rename(root / f"{name}{BASE_SUFFIX}")
    wa = root / "world_alignment.json"
    if wa.is_file():
        wa.rename(root / f"world_alignment{BASE_SUFFIX}.json")
    prepared.rename(root / "prepared")
    ma.rename(root / "mapanything")
    write_json(wa, dict(align_doc))


def restore_baseline(root: Path) -> bool:
    """swap_in 을 되돌린다(병합본은 *_supplement 로 남긴다). 굽기 결과 폴더도 지워 보조 촬영 없이 다시 굽게 한다."""
    if not (root / f"prepared{BASE_SUFFIX}").is_dir():
        return False
    for name in ("prepared", "mapanything"):
        cur, keep = root / name, root / f"{name}_supplement"
        if keep.exists():
            shutil.rmtree(keep)
        if cur.exists():
            cur.rename(keep)
        (root / f"{name}{BASE_SUFFIX}").rename(cur)
    wa = root / "world_alignment.json"
    if wa.is_file():
        wa.unlink()
    if (root / f"world_alignment{BASE_SUFFIX}.json").is_file():
        (root / f"world_alignment{BASE_SUFFIX}.json").rename(wa)
    for name in ("textured", "textured_raw", "depth", "depth_calib", "depth_fitted"):
        if (root / name).exists():
            shutil.rmtree(root / name, ignore_errors=True)
    for name in ("bake.json", "bake.error.json"):
        if (root / name).is_file():
            (root / name).unlink()
    return True


def load_request(root: Path) -> tuple[list[MediaInput], dict[str, Any]]:
    doc = json.loads((root / SUPPLEMENT_DIR / REQUEST_FILE).read_text(encoding="utf-8"))
    media = []
    for m in doc["media"][:MAX_MEDIA]:
        path = m.get("path")
        media.append(MediaInput(
            media_id=str(m["media_id"]), kind=str(m["kind"]),
            path=None if not path else root / SUPPLEMENT_DIR / path,
            captured_at_ms=m.get("captured_at_ms"), source=m.get("source"), device=m.get("device"),
            sha256=m.get("sha256"), status=m.get("status")))
    return media, dict(doc.get("config") or {})


def write_request(root: Path, media: Sequence[Mapping[str, Any]], config: Mapping[str, Any] | None = None) -> Path:
    """media 항목: media_id, kind, path(supplement/ 기준 상대) 또는 status(DOWNLOAD_FAILED 등), 선택 출처 필드."""
    d = root / SUPPLEMENT_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / REQUEST_FILE
    p.write_text(json.dumps({"media": list(media), "config": dict(config or {})}, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return p


def summary(
    *,
    status: str,
    reason: str | None,
    media: Sequence[MediaInput],
    media_stats: Mapping[str, Mapping[str, Any]] | None = None,
    frames_selected: int = 0,
    frames_registered: int = 0,
    alignment: Mapping[str, Any] | None = None,
    used_media: Iterable[str] = (),
    registered_by_media: Mapping[str, int] | None = None,
    selected_by_media: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """계약 site_setup#/$defs/supplement_summary 모양."""
    used = set(used_media)
    stats = media_stats or {}
    rows = []
    for m in media[:MAX_MEDIA]:
        st = stats.get(m.media_id) or {}
        sel = int((selected_by_media or {}).get(m.media_id, st.get("selected", 0)) or 0)
        reg = int((registered_by_media or {}).get(m.media_id, 0))
        if m.media_id in used:
            state = M_USED
        else:
            state = st.get("status") or m.status or (M_NOT_REGISTERED if sel else M_NO_FRAMES)
            if state == M_USED:
                state = M_NOT_REGISTERED
        rows.append({"media_id": m.media_id, "status": state, "frames_selected": sel, "frames_registered": reg})
    out: dict[str, Any] = {"status": status, "reason": None if status == "APPLIED" else reason, "input_role": INPUT_ROLE,
                           "media_count": len(media), "frames_selected": int(frames_selected),
                           "frames_registered": int(frames_registered)}
    times = [m.captured_at_ms for m in media if m.media_id in used and isinstance(m.captured_at_ms, int)]
    if times:
        out["captured_from_ms"], out["captured_to_ms"] = min(times), max(times)
    if alignment is not None:
        out["alignment"] = {k: (None if alignment.get(k) is None else round(float(alignment[k]), 5))
                            for k in ("baseline_center_residual_m", "supplement_center_residual_m",
                                      "baseline_fixed_reprojection_px", "supplement_fixed_reprojection_px")}
    if rows:
        out["media"] = rows
    return out


def _first_failure_reason(media_stats: Mapping[str, Mapping[str, Any]]) -> str:
    states = [st.get("status") for st in media_stats.values()]
    if states and all(s == M_DOWNLOAD_FAILED for s in states):
        return R_MEDIA_DOWNLOAD_FAILED
    if states and all(s in (M_UNREADABLE, M_DOWNLOAD_FAILED) for s in states):
        return R_MEDIA_UNREADABLE
    return R_NO_USABLE_FRAMES


def run_supplement(
    root: Path,
    *,
    infer: Callable[[Path, Path], None],
    person_mask: Callable[[np.ndarray], np.ndarray | None] | None,
    cfg: SupplementConfig,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """보조 촬영 전체(고르기 → 묶음별 추론·정렬·등록·관문 → 병합·교체). 요약을 돌려주고 supplement/result.json 에 쓴다.

    infer(prepared_dir, ma_dir): MapAnything prepared 모드 한 번(운영은 run_mapanything, 시험은 가짜). 어떤 실패도 예외로
    올리지 않고 FALLBACK 요약으로 바꾼다(세팅을 실패시키지 않는다). 받아들인 묶음이 있으면 작업 폴더를 병합본으로 바꾼다."""
    from simai_ai.calibration.world_frame.inputs import load_mapanything_run, load_prepared

    from .reconstruct import ReconstructionError, write_json
    from .reconstruct_tasks import solve_fixed_alignment

    t0 = monotonic()
    sup_dir = root / SUPPLEMENT_DIR
    media, _ = load_request(root)
    by_id = {m.media_id: m for m in media}

    def finish(doc: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
        write_json(sup_dir / RESULT_FILE, {"summary": doc, "detail": extra or {}, "config": cfg.__dict__,
                                           "seconds": round(monotonic() - t0, 2)})
        return doc

    if person_mask is None:
        return finish(summary(status="SKIPPED", reason=R_SEGMENTER_MISSING, media=media))
    selection = select_frames(media, cfg)
    frames = selection.frames
    if not frames:
        return finish(summary(status="FALLBACK", reason=_first_failure_reason(selection.media), media=media,
                              media_stats=selection.media), {"media": selection.media})
    fixed_ids = {im["camera_id"] for im in json.loads((root / "job.json").read_text(encoding="utf-8"))["input"]["images"]}
    base_prep = root / "prepared"
    baseline_center = centers_only_residual(root, root / "mapanything", base_prep)
    try:
        base_run = load_mapanything_run(root / "mapanything", prepared=load_prepared(base_prep))
        baseline_px = fixed_reprojection_px(base_run.views, fixed_ids)
    except (OSError, ValueError, KeyError):
        baseline_px = None
    alignment = {"baseline_center_residual_m": baseline_center, "baseline_fixed_reprojection_px": baseline_px,
                 "supplement_center_residual_m": None, "supplement_fixed_reprojection_px": None}

    chunks: list[ChunkResult] = []
    pending = list(frames)
    bridge: list[SelectedFrame] = []
    budget_hit = False
    while pending:
        if chunks and monotonic() - t0 >= cfg.time_budget_s:
            budget_hit = True
            break
        take = pending[: cfg.chunk_frames]
        pending = pending[cfg.chunk_frames:]
        batch = bridge + take
        idx = len(chunks)
        cdir = sup_dir / f"chunk_{idx:02d}"
        ch = ChunkResult(idx, [f.name for f in batch], cdir / "prepared", cdir / "mapanything")
        tc = monotonic()
        chunks.append(ch)
        try:
            prep_doc = build_joint_prepared(base_prep, ch.prepared_dir, batch, by_id, person_mask,
                                            mask_dilate_px=cfg.mask_dilate_px)
            ch.prepared_views = {v["name"]: v for v in prep_doc["views"]}
            infer(ch.prepared_dir, ch.ma_dir)
            run = load_mapanything_run(ch.ma_dir, prepared=load_prepared(ch.prepared_dir))
            try:
                ch.M, ch.align_doc = solve_fixed_alignment(root, ma_dir=ch.ma_dir, prepared_dir=ch.prepared_dir)
            except ReconstructionError:
                ch.M = None
            ch.center_m = centers_only_residual(root, ch.ma_dir, ch.prepared_dir)
            ch.fixed_px = fixed_reprojection_px(run.views, fixed_ids)
            ch.registration = register_views(run.views, fixed_ids, cfg)
            fov: dict[str, list[float]] = {}
            for v in run.views:
                mid = _media_of(v.name, ch.prepared_views)
                if mid is not None:
                    fov.setdefault(mid, []).append(hfov_deg(v))
                    ch.registration.setdefault(v.name, {})["hfov_deg"] = round(hfov_deg(v), 2)
            ch.wide_media = {mid for mid, fs in fov.items() if float(np.median(fs)) > cfg.wide_fov_deg}
            new_names = [n for n in ch.names if n not in {f.name for f in bridge}]
            new_kept = [n for n in ch.kept if n in new_names]
            ch.reason = R_ALIGNMENT_WORSE if ch.M is None else gate_chunk(
                selected=len(new_names), registered=len(new_kept), baseline_center_m=baseline_center,
                chunk_center_m=ch.center_m, baseline_px=baseline_px, chunk_px=ch.fixed_px, cfg=cfg)
        except ReconstructionError as exc:
            ch.reason = R_RECONSTRUCTION_FAILED
            log.warning("보조 촬영 묶음 %d MapAnything 실패: %s %s", idx, exc.code, exc)
        except Exception as exc:  # noqa: BLE001 - 보조 촬영은 어떤 실패도 세팅을 멈추지 않는다
            ch.reason = R_RECONSTRUCTION_FAILED
            log.warning("보조 촬영 묶음 %d 실패: %s: %s", idx, type(exc).__name__, exc)
        ch.seconds = monotonic() - tc
        bridge = []
        if ch.reason is None and cfg.bridge_frames > 0 and pending:
            kept_set = set(ch.kept)
            bridge = [f for f in batch if f.name in kept_set][-cfg.bridge_frames:]

    accepted = [c for c in chunks if c.reason is None]
    inferred_names = {n for c in chunks for n in c.names}
    used_names = {n for c in accepted for n in c.kept}
    registered_names = {n for c in chunks for n in c.kept}
    media_of = {f.name: f.media_id for f in frames}
    sel_by = {mid: sum(1 for n in inferred_names if media_of.get(n) == mid) for mid in by_id}
    reg_by = {mid: sum(1 for n in registered_names if media_of.get(n) == mid) for mid in by_id}
    for c in chunks:
        for mid in c.wide_media:
            selection.media[mid]["status"] = M_WIDE_FOV_REJECTED
    used_media = {media_of[n] for n in used_names}
    worst = max(accepted or chunks[:1], key=lambda c: c.center_m if c.center_m is not None else math.inf)
    alignment["supplement_center_residual_m"] = worst.center_m
    alignment["supplement_fixed_reprojection_px"] = worst.fixed_px
    detail = {"chunks": [c.record() for c in chunks], "media": selection.media, "time_budget_hit": budget_hit,
              "registration": {c.index: c.registration for c in chunks}}
    common = dict(media=media, media_stats=selection.media, frames_selected=len(inferred_names),
                  frames_registered=len(registered_names), alignment=alignment,
                  registered_by_media=reg_by, selected_by_media=sel_by)
    if not accepted:
        reason = next((c.reason for c in chunks if c.reason), R_RECONSTRUCTION_FAILED)
        return finish(summary(status="FALLBACK", reason=reason, **common), detail)
    try:
        merged_prep, merged_ma = sup_dir / "merged" / "prepared", sup_dir / "merged" / "mapanything"
        align_doc = materialize_world_run(root, accepted, merged_prep, merged_ma)
        swap_in(root, merged_prep, merged_ma, align_doc)
    except Exception as exc:  # noqa: BLE001
        log.warning("보조 촬영 병합 실패: %s: %s", type(exc).__name__, exc)
        restore_baseline(root)
        return finish(summary(status="FALLBACK", reason=R_RECONSTRUCTION_FAILED, **common), detail)
    return finish(summary(status="APPLIED", reason=None, used_media=used_media, **common), detail)


def annotate_bake(root: Path, summary_doc: Mapping[str, Any] | None) -> None:
    """bake.json 에 보조 촬영 출처를 남기고, map_cameras(뷰어·축척 점검용)에서 보조 view 를 뺀다(등록 카메라만).

    텍셀 단위로 고정 카메라·보조 촬영을 나누지는 않는다(texrecon 면 출처는 관측 OBSERVED 로 같다). 맵 전체 단위로
    '세팅 때 따로 찍은 관측이 들어감'과 촬영 시각·기기를 남긴다(후속: 텍셀 단위 출처, 설계 문서)."""
    from .reconstruct import read_json, write_json

    path = root / "bake.json"
    if not path.is_file():
        return
    doc = read_json(path)
    prep = root / "prepared" / "prepared.json"
    sup = None
    if prep.is_file():
        pdoc = json.loads(prep.read_text(encoding="utf-8"))
        sup = pdoc.get("supplement")
        names = {v["name"] for v in pdoc["views"] if v.get("view_role") == VIEW_ROLE_SUPPLEMENT}
        mc = doc.get("map_cameras")
        if isinstance(mc, dict) and names:
            doc["map_cameras"] = {k: v for k, v in mc.items() if k not in names}
    if summary_doc is not None or sup is not None:
        doc["supplement_capture"] = {"input_role": INPUT_ROLE, "surface_source": "OBSERVED",
                                     "applied": bool(sup), "summary": dict(summary_doc or {}),
                                     "provenance": sup}
    write_json(path, doc)
