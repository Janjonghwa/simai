"""VLM 입력 프레임 얼굴 처리 관문 (명세 5.4 "VLM 입력 프레임", 11.5, FR-89, S15P31A101-270).

VLM 에 들어가는 이미지는 이 모듈의 `VlmFrame` 뿐이고, `VlmFrame` 은 `prepare_vlm_frames` 만 만들 수 있다.
그래서 얼굴 처리를 거치지 않은 원본 프레임은 VLM 입력이 될 수 없다(VLM 클라이언트는 `VlmFrame` 만 받는다).

- 얼굴 영역은 pose keypoint(COCO17 코·눈·귀)로 잡는다. 별도 얼굴 검출 모델은 쓰지 않는다(5.4).
- 머리 윗부분(이마 위·안전모)은 남기고, 눈높이 조금 위부터 턱 아래까지를 처리한다.
- BLURRED: 영역을 굵은 모자이크로 바꾼다(복원 불가). EXCLUDED: 영역을 단색으로 칠해 얼굴 픽셀을 남기지 않는다.
- 원본 배열은 바꾸지 않는다(복사). 처리한 이미지는 메모리에서만 쓰고 저장하지 않는다(저장된 원본은 그대로, 11.5).
- 불확실하면 안전한 쪽(fail-closed): 얼굴 점이 일부만 보이면 사람 크기로 넓힌 상자, 얼굴 점이 없으면 사람 상자 위쪽
  기하로 처리하고, 검출·디코드가 실패한 프레임은 VLM 에 넘기지 않는다(흐리지 않은 프레임으로 대신하지 않는다).

numpy 만 쓴다. 이미지 디코드와 pose 검출은 주입한다(Tier 0 경로는 이 모듈을 import 하지 않는다, import-linter
tier0-no-worker-stack).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import numpy as np

FacePolicy = Literal["BLURRED", "EXCLUDED"]
FACE_POLICIES: tuple[str, ...] = ("BLURRED", "EXCLUDED")

# COCO17 얼굴 관절(contracts common joint_layout 순서): 코, 왼눈, 오른눈, 왼귀, 오른귀
COCO17_NOSE, COCO17_L_EYE, COCO17_R_EYE, COCO17_L_EAR, COCO17_R_EAR = 0, 1, 2, 3, 4
FACE_JOINTS = (COCO17_NOSE, COCO17_L_EYE, COCO17_R_EYE, COCO17_L_EAR, COCO17_R_EAR)
EYE_JOINTS = (COCO17_L_EYE, COCO17_R_EYE)

# --- 얼굴 영역 비율 (모두 머리 폭 추정값 s 의 배수). 근거는 docs/design/vlm-face-blur.md ---
KEYPOINT_CONF_MIN = 0.2
"""이 신뢰도 이상인 얼굴 점만 쓴다. 낮게 둔다: 점을 버리면 그 사람이 '얼굴 점 없음'(기하 처리)으로 가서 더 넓게 처리될 뿐이다."""
HEAD_FROM_BBOX_WIDTH = 1 / 3
"""사람 상자 폭 → 머리 폭. 선 사람의 어깨 폭이 머리 폭의 약 2.5~3배라 1/3 로 잡는다(웅크리면 상자가 넓어져 더 크게 = 안전한 쪽)."""
MIN_HEAD_PX = 12.0
"""머리 폭 하한(px). 3 m 높이 카메라에서 얼굴은 30~50 px(16장 결정 근거)라 그보다 작게 잡히는 것은 점 잡음으로 본다."""
SIDE_MARGIN = 0.75
"""두 눈이 보이면 얼굴 점 중심에서 좌우로 s × 0.75. 머리 폭(s) 에 양쪽 25% 여유."""
PARTIAL_SIDE = 1.0
"""눈이 하나라도 안 보이면(옆모습·가림) 보이는 점들의 양 끝에서 좌우로 s × 1.0(얼굴이 어느 쪽에 있는지 모르므로 양쪽 다)."""
ABOVE_EYES = 0.30
"""눈높이 위로 s × 0.30 까지만 처리한다. 그 위(이마 위·안전모)는 남긴다(5.4 '안전모가 보이도록 머리 윗부분은 남긴다')."""
BELOW_EYES = 0.95
"""눈높이 아래로 s × 0.95(턱 아래까지). 정면에서 눈→턱은 머리 폭의 약 0.7 배, 고개 숙임·위에서 본 각도 여유를 더했다."""
NOSE_BELOW_EYES = 0.35
"""눈이 하나도 안 보이면 눈높이를 모른다. 코는 눈보다 머리 폭의 약 1/3 아래라, 보이는 점 중 가장 위 점이 코라고 보고 그만큼 위로
올려 잡는다(귀만 보일 때는 안전모 아래쪽 일부가 함께 처리된다 — 얼굴을 남기는 것보다 낫다). 아래는 가장 아래 점에서 s × 0.95."""
NO_FACE_TOP = 0.05
NO_FACE_BOTTOM = 0.30
"""얼굴 점이 하나도 없는 사람: 상자 높이의 5%~30% 띠(상자 폭 전체)를 처리한다. 맨 위 5% 는 안전모 꼭대기로 남긴다.
얼굴이 안 보이는 것(뒤돌아섬)인지 점을 못 잡은 것인지 구별할 수 없어 처리하는 쪽을 고른다."""

# --- 처리 강도 ---
MOSAIC_CELLS = 3
"""BLURRED: 영역의 긴 변을 3칸으로 나눈 모자이크(칸마다 평균색 하나). 30~50 px 얼굴이면 칸이 10~17 px 라 눈·코·입이 한 칸에
섞여 복원할 수 없다(영역 전체 정보가 최대 9개 색)."""
EXCLUDED_FILL = 128
"""EXCLUDED: 영역을 이 회색으로 칠한다(얼굴 픽셀을 남기지 않음, avatar_asset face_policy EXCLUDED 와 같은 뜻)."""

Keypoint = tuple[float, float, float] | None


@dataclass(frozen=True)
class Person:
    """사람 한 명의 pose 검출. bbox 는 (x1, y1, x2, y2) px, keypoints 는 COCO17 (u, v, conf) 또는 None."""

    bbox: tuple[float, float, float, float]
    keypoints: tuple[Keypoint, ...]


class KeypointProvider(Protocol):
    def people(self, image: np.ndarray) -> Sequence[Person]:
        """BGR uint8 (H, W, 3) 이미지의 사람 목록. 실패는 예외로 알린다(빈 목록은 '사람 없음'이다)."""
        ...


class PoseDetectorProvider:
    """`PoseDetector`(calibration.extrinsics.extract·services.tier0.yolo_detector 와 같은 dict 형식)를 KeypointProvider 로.

    검출기는 분석 워커 프로세스 안에서 돈다(GPU 메모리 상한은 워커 프로세스 상한 gpu.reserve.cap_process_memory 을 따른다).
    사람 검출 신뢰도 기준은 검출기 설정을 낮게 두는 쪽이 안전하다(놓친 사람의 얼굴은 처리되지 않는다).
    """

    def __init__(self, detector: Any) -> None:
        self._detector = detector

    def people(self, image: np.ndarray) -> list[Person]:
        (dets,) = self._detector.detect([image])
        return [
            Person(
                bbox=tuple(float(v) for v in d["bbox"]),  # type: ignore[arg-type]
                keypoints=tuple(None if k is None else (float(k[0]), float(k[1]), float(k[2])) for k in d.get("keypoints_2d") or ()),
            )
            for d in dets
        ]


@dataclass(frozen=True)
class FaceRegion:
    """처리한 영역 (x1, y1, x2, y2) 정수 px, 반열림. source 는 keypoints 또는 bbox(얼굴 점 없음)."""

    x1: int
    y1: int
    x2: int
    y2: int
    source: Literal["keypoints", "bbox"]


def face_region(person: Person, shape: tuple[int, ...]) -> FaceRegion | None:
    """사람 한 명의 처리 영역. 이미지 밖이면 None."""
    h, w = shape[:2]
    x1, y1, x2, y2 = person.bbox
    pts = {
        j: (kp[0], kp[1])
        for j in FACE_JOINTS
        if j < len(person.keypoints) and (kp := person.keypoints[j]) is not None and kp[2] >= KEYPOINT_CONF_MIN
    }
    if pts:
        xs = [p[0] for p in pts.values()]
        ys = [p[1] for p in pts.values()]
        s = max(max(xs) - min(xs), max(ys) - min(ys), (x2 - x1) * HEAD_FROM_BBOX_WIDTH, MIN_HEAD_PX)
        eyes = [pts[j][1] for j in EYE_JOINTS if j in pts]
        if eyes:
            eye_y = sum(eyes) / len(eyes)
            top, bottom = eye_y - ABOVE_EYES * s, eye_y + BELOW_EYES * s
        else:
            top, bottom = min(ys) - (NOSE_BELOW_EYES + ABOVE_EYES) * s, max(ys) + BELOW_EYES * s
        if len(eyes) == 2:
            cx = sum(xs) / len(xs)
            left, right = cx - SIDE_MARGIN * s, cx + SIDE_MARGIN * s
        else:
            # 눈이 하나라도 없으면 얼굴 중심을 모른다(보이는 점이 귀 하나면 얼굴은 그 옆 머리 폭 안 어디든 있다): 양옆으로 s 씩 넓힌다
            left, right = min(xs) - PARTIAL_SIDE * s, max(xs) + PARTIAL_SIDE * s
        box = (left, top, right, bottom)
        source: Literal["keypoints", "bbox"] = "keypoints"
    else:
        bh = y2 - y1
        box = (x1, y1 + NO_FACE_TOP * bh, x2, y1 + NO_FACE_BOTTOM * bh)
        source = "bbox"
    r = (
        max(0, int(np.floor(box[0]))),
        max(0, int(np.floor(box[1]))),
        min(w, int(np.ceil(box[2]))),
        min(h, int(np.ceil(box[3]))),
    )
    if r[0] >= r[2] or r[1] >= r[3]:
        return None
    return FaceRegion(*r, source=source)


def _mosaic(region: np.ndarray) -> None:
    rh, rw = region.shape[:2]
    cell = max(1, -(-max(rh, rw) // MOSAIC_CELLS))
    for y in range(0, rh, cell):
        for x in range(0, rw, cell):
            block = region[y : y + cell, x : x + cell]
            block[...] = block.reshape(-1, block.shape[-1]).mean(axis=0).round().astype(region.dtype)


@dataclass(frozen=True)
class FaceRecord:
    """프레임 하나의 처리 기록(이미지 없이 남길 수 있는 값만)."""

    people: int
    regions: int
    bbox_fallback: int


def apply_face_policy(
    image: np.ndarray, people: Sequence[Person], face_policy: FacePolicy
) -> tuple[np.ndarray, FaceRecord, tuple[FaceRegion, ...]]:
    """얼굴 영역을 처리한 사본, 기록, 영역. image 는 바꾸지 않는다."""
    if face_policy not in FACE_POLICIES:
        raise ValueError(f"face_policy 는 BLURRED 또는 EXCLUDED 다: {face_policy!r}")
    _check_image(image)
    out = image.copy()
    regions = tuple(r for p in people if (r := face_region(p, image.shape)) is not None)
    for r in regions:
        view = out[r.y1 : r.y2, r.x1 : r.x2]
        if face_policy == "BLURRED":
            _mosaic(view)
        else:
            view[...] = EXCLUDED_FILL
    record = FaceRecord(people=len(people), regions=len(regions), bbox_fallback=sum(r.source == "bbox" for r in regions))
    return out, record, regions


def _check_image(image: Any) -> None:
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("이미지는 BGR uint8 (H, W, 3) 배열이어야 한다")
    if image.shape[0] == 0 or image.shape[1] == 0:
        raise ValueError("빈 이미지")


# --- 관문 ---------------------------------------------------------------------------------------

_GATE = object()


class VlmFrame:
    """얼굴 처리를 거친 VLM 입력 프레임. `prepare_vlm_frames` 만 만들 수 있고 픽셀은 읽기 전용이다."""

    __slots__ = ("source", "face_policy", "record", "_pixels")

    def __init__(self, token: object, source: str, face_policy: FacePolicy, record: FaceRecord, pixels: np.ndarray) -> None:
        if token is not _GATE:
            raise TypeError("VlmFrame 은 prepare_vlm_frames 로만 만든다(얼굴 처리 관문)")
        pixels.setflags(write=False)
        self.source = source
        self.face_policy = face_policy
        self.record = record
        self._pixels = pixels

    @property
    def pixels(self) -> np.ndarray:
        """처리된 BGR 이미지(읽기 전용). 메모리에서만 쓰고 저장소에 올리지 않는다."""
        return self._pixels


@dataclass(frozen=True)
class DroppedFrame:
    """VLM 에 넘기지 않은 프레임과 사유. reason: decode | detect."""

    source: str
    reason: Literal["decode", "detect"]
    detail: str


@dataclass(frozen=True)
class GateResult:
    frames: tuple[VlmFrame, ...]
    dropped: tuple[DroppedFrame, ...]


Decoder = Callable[[bytes], np.ndarray]


def prepare_vlm_frames(
    items: Sequence[tuple[str, bytes | np.ndarray]],
    face_policy: FacePolicy,
    provider: KeypointProvider,
    *,
    decode: Decoder | None = None,
) -> GateResult:
    """(source, 인코딩된 바이트 또는 디코드된 BGR 배열) 목록 → 얼굴을 처리한 VLM 프레임과 뺀 프레임.

    바이트는 decode 로 푼다(이벤트 프레임). 배열은 클립에서 뽑은 프레임이다(incident_summary, S15P31A101-271).
    디코드·검출이 실패한 프레임은 빼고 사유를 남긴다. 남는 프레임이 없어도 예외를 던지지 않는다(작업 실패는 호출자가 정한다).
    """
    if face_policy not in FACE_POLICIES:
        raise ValueError(f"face_policy 는 BLURRED 또는 EXCLUDED 다: {face_policy!r}")
    frames: list[VlmFrame] = []
    dropped: list[DroppedFrame] = []
    for source, item in items:
        try:
            if isinstance(item, np.ndarray):
                image = item
            else:
                if decode is None:
                    raise ValueError("바이트 프레임에는 decode 가 필요하다")
                image = decode(item)
            _check_image(image)
        except Exception as e:  # 디코드 실패·형식 오류: 이 프레임은 넘기지 않는다
            dropped.append(DroppedFrame(source, "decode", f"{type(e).__name__}: {e}"))
            continue
        try:
            people = list(provider.people(image))
        except Exception as e:  # 검출 실패: 얼굴을 모르므로 넘기지 않는다(fail-closed)
            dropped.append(DroppedFrame(source, "detect", f"{type(e).__name__}: {e}"))
            continue
        out, record, _ = apply_face_policy(image, people, face_policy)
        frames.append(VlmFrame(_GATE, source, face_policy, record, out))
    return GateResult(tuple(frames), tuple(dropped))


def summarize(result: GateResult) -> Mapping[str, Any]:
    """처리 기록 요약(로그·결과용, 이미지 없음)."""
    return {
        "frames": len(result.frames),
        "people": sum(f.record.people for f in result.frames),
        "regions": sum(f.record.regions for f in result.frames),
        "bbox_fallback": sum(f.record.bbox_fallback for f in result.frames),
        "dropped": [{"source": d.source, "reason": d.reason} for d in result.dropped],
    }
