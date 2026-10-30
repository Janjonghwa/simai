"""실측 정물 목록 + MapAnything 점으로 축척 근거 만들기 (명세 4.3 스케일 1순위 실측 정물, S15P31A101-100).

**목록(`simai.measured_objects`).** 정물마다 id·이름·치수(m)·기준 변(`edges`: 이름, 길이, 쓰는 조건)·잰 날짜·방법·잰 사람·
찾는 방법(`detector`)·재는 방법(`measurement`)·묶음 기준(`aggregation`)을 둔다. 기본 목록은 패키지 안
`calibration/data/measured_objects.json` 이다. 정물을 더할 때 코드는 새 `detector.kind`(`DETECTORS`)나 새
`measurement.kind`(`MEASURERS`)가 필요할 때만 바꾼다.

**노트북 윗판(`LAPTOP_LID`).** 펴진(prepare) 그림의 분할 마스크를 `inputs.mask_to_model_res` 와 같은 crop·resize 로 모델
해상도로 옮겨(덮인 면적 0.5 이상 = 픽셀 중심이 마스크 안) 그 카메라의 MapAnything 점을 모으고, 바닥 평면(`fix` 와 같은
`find_floor`) 기준 높이로 정리한다. 높이 폭(2~98 백분위)이 작으면 닫힌 덮개(수평 판: 사각형 두 변), 크면 연 덮개(가장 높은
띠 = 덮개 윗변: 가로 변 하나), 그 사이는 버린다. 공통 검사(점 수·섞인 마스크·바닥 판(자판) 보임·덮개 비율·끝점 가림·그림
경계)를 지난 뒤 길이를 두 방법으로 잰다(근거는 `length_method`, 기본 IMAGE_EDGE, 다른 방법은 비교용 기록):
- IMAGE_EDGE(연 덮개만): 원본(펴진) 그림 마스크 윤곽을 점구름으로 맞춘 덮개 평면에 되투영해, 점구름 띠 둘레 윤곽의 위쪽
  끝 토막에 직선을 맞추고 그 양 끝을 잰다(원본 4.45 배 해상도, 깊이 번짐은 평면 위치로만 들어감). 같은 노트북이 두 카메라
  이상에 보이면 끝점 광선을 삼각측량해 그 길이를 먼저 쓴다(깊이와 무관). 윗변 끝점이 사람 마스크에 닿으면 버린다.
- POINT_CLOUD: 띠 끝점 백분위 폭을 균등 분포로 되돌리고(÷(1 − 2α)) 끝 픽셀 위상 몫(2 / (줄 수 + 1) 칸)을 더한다.
배율 = 목록 길이 / MapAnything 길이. 판정이 애매하면 버린다(모르면 비움).

**묶기.** 같은 노트북이 여러 카메라(또는 한 카메라의 겹친 검출)에 보이면 3D 위치(윗변 가운데·판 가운데)로 묶어 한 표본으로
센다(독립 표본 수를 부풀리지 않는다). 묶음 값은 삼각측량 값 또는 구성원 중앙값이다. 묶음 값들의 중앙값에서 목록의
허용치보다 벗어난 표본은 다른 기종·오검출로 보고 빼되, 뺀 비율이 목록의 상한을 넘거나 남은 표본이 최소 수보다 적으면
근거를 내지 않는다.
결과는 `fix --scale-evidence` 가 받는 `{"estimates": [{"basis": "MEASURED_OBJECT", "scale", "sample_count", "label": 정물 id}]}`
이고, 같은 MapAnything 실행(cameras.json sha256)에서만 유효하다. 수치 근거는 docs/design/measured-object-scale.md.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from simai_ai.calibration.world_frame.frame import Plane, WorldFrameConfig, _up_prior, find_floor
from simai_ai.calibration.world_frame.inputs import MapAnythingRun, View, mask_to_model_res, prepared_to_model_px
from simai_ai.calibration.world_frame.stool_tops import HSVStoolDetector, StoolTopMeasurer

CATALOG_FORMAT = "simai.measured_objects"
EVIDENCE_FORMAT = "simai.scale_evidence"
EVIDENCE_VERSION = "0.1.0"
DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / "data" / "measured_objects.json"

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
OPEN = "OPEN"
CLOSED = "CLOSED"
STATUS_OK = "OK"
STATUS_REJECTED = "REJECTED"


# --------------------------------------------------------------------------- 목록


@dataclass(frozen=True)
class MeasuredObject:
    id: str
    name: str
    edges: Mapping[str, float]  # 변 이름 → 길이(m)
    detector: Mapping[str, Any]
    measurement: Mapping[str, Any]
    min_samples: int
    agreement_tolerance: float
    max_outlier_fraction: float
    raw: Mapping[str, Any] = field(default_factory=dict)


def load_catalog(path: Path = DEFAULT_CATALOG) -> tuple[tuple[MeasuredObject, ...], dict[str, Any]]:
    """목록 파일 → (정물들, 입력 기록). 형식이 어긋나면 ValueError(무엇이 틀렸는지)."""
    raw = Path(path).read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    return parse_catalog(doc, Path(path).name), {"path": Path(path).name, "sha256": _sha256(raw),
                                                  "format_version": doc.get("format_version")}


def parse_catalog(doc: Mapping[str, Any], where: str = "목록") -> tuple[MeasuredObject, ...]:
    if doc.get("format") != CATALOG_FORMAT:
        raise ValueError(f"{where}: 실측 정물 목록 형식이 아니다: {doc.get('format')!r}")
    objs = doc.get("objects")
    if not isinstance(objs, list) or not objs:
        raise ValueError(f"{where}: objects 가 비었다")
    out, seen = [], set()
    for i, o in enumerate(objs):
        at = f"{where}: objects[{i}]"
        oid = o.get("id")
        if not isinstance(oid, str) or not oid or oid in seen:
            raise ValueError(f"{at}: id 가 없거나 겹친다: {oid!r}")
        seen.add(oid)
        if not isinstance(o.get("name"), str) or not o["name"]:
            raise ValueError(f"{at}: name 이 없다")
        edges = {}
        for e in o.get("edges") or []:
            length = e.get("length_m")
            if not isinstance(e.get("name"), str) or not _positive(length) or length > 10:
                raise ValueError(f"{at}: edges 는 이름과 0 < length_m ≤ 10 (m) 이다: {e!r}")
            if e["name"] in edges:
                raise ValueError(f"{at}: 변 이름이 겹친다: {e['name']}")
            edges[e["name"]] = float(length)
        if not edges:
            raise ValueError(f"{at}: edges 가 없다")
        m = o.get("measured") or {}
        for key in ("date", "by", "method"):
            if not isinstance(m.get(key), str) or not m[key]:
                raise ValueError(f"{at}: measured.{key}(잰 날짜·사람·방법)가 없다")
        det, meas = o.get("detector") or {}, o.get("measurement") or {}
        if det.get("kind") not in DETECTORS:
            raise ValueError(f"{at}: detector.kind 는 {sorted(DETECTORS)} 중 하나다: {det.get('kind')!r}")
        if meas.get("kind") not in MEASURERS:
            raise ValueError(f"{at}: measurement.kind 는 {sorted(MEASURERS)} 중 하나다: {meas.get('kind')!r}")
        MEASURERS[meas["kind"]].validate(meas, edges, at)
        agg = o.get("aggregation") or {}
        n, tol, frac = agg.get("min_samples"), agg.get("agreement_tolerance"), agg.get("max_outlier_fraction", 0.0)
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ValueError(f"{at}: aggregation.min_samples 는 1 이상의 정수다: {n!r}")
        if not _positive(tol) or tol >= 0.5:
            raise ValueError(f"{at}: aggregation.agreement_tolerance 는 0 < x < 0.5 다: {tol!r}")
        if not isinstance(frac, (int, float)) or isinstance(frac, bool) or not 0 <= frac < 0.5:
            raise ValueError(f"{at}: aggregation.max_outlier_fraction 는 0 ≤ x < 0.5 다(과반은 남아야 한다): {frac!r}")
        out.append(MeasuredObject(oid, o["name"], edges, dict(det), dict(meas), n, float(tol), float(frac), dict(o)))
    return tuple(out)


def _positive(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


# --------------------------------------------------------------------------- 찾기


@dataclass(frozen=True)
class Detection:
    """펴진(prepare) 그림 해상도의 분할 마스크 하나."""

    mask: np.ndarray  # (H, W) bool, 펴진 그림 크기
    confidence: float
    class_name: str = ""


class ObjectDetector(Protocol):
    def describe(self) -> dict[str, Any]: ...

    def detect(self, image_bgr: np.ndarray) -> list[Detection]: ...


class UltralyticsSegDetector:
    """ultralytics YOLO seg(extras detection). 목록의 classes·conf·imgsz 로 찾는다. 가중치는 sha256 을 확인한다."""

    def __init__(self, spec: Mapping[str, Any], weights_path: Path, *, device: str | None = None) -> None:
        from ultralytics import YOLO

        from simai_ai.calibration.intrinsics.estimate import WeightsRef

        self._ref = WeightsRef(str(weights_path), spec["weights_sha256"])
        self._model = YOLO(str(self._ref.verify()), task="segment")
        self._spec, self._device = dict(spec), device

    def describe(self) -> dict[str, Any]:
        import ultralytics

        s = self._spec
        return {"kind": s["kind"], "model": Path(self._ref.path).name, "sha256": self._ref.sha256, "classes": s["classes"],
                "conf": s["conf"], "imgsz": s["imgsz"], "ultralytics": ultralytics.__version__}

    def detect(self, image_bgr: np.ndarray) -> list[Detection]:
        h, w = image_bgr.shape[:2]
        s = self._spec
        r = self._model.predict(image_bgr, classes=list(s["classes"]), conf=s["conf"], imgsz=s["imgsz"],
                                retina_masks=True, verbose=False, device=self._device)[0]
        if r.masks is None:
            return []
        out = []
        for m, c, k in zip(r.masks.data.cpu().numpy() > 0.5, r.boxes.conf.cpu().numpy(), r.boxes.cls.cpu().numpy()):
            if m.shape != (h, w):
                import cv2

                m = cv2.resize(m.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
            out.append(Detection(m, float(c), str(r.names.get(int(k), int(k)))))
        return out


def _ultralytics_seg(spec: Mapping[str, Any], weights_dir: Path | None, device: str | None) -> ObjectDetector:
    for key in ("weights", "weights_sha256", "classes", "conf", "imgsz"):
        if key not in spec:
            raise ValueError(f"detector ULTRALYTICS_SEG 에 {key} 가 없다")
    if weights_dir is None:
        raise ValueError("가중치 폴더(--weights-dir)가 필요하다(가중치는 커밋하지 않는다)")
    return UltralyticsSegDetector(spec, Path(weights_dir) / spec["weights"], device=device)


# detector.kind → 만드는 함수(spec, 가중치 폴더, 장치). 라바콘(YOLOE 텍스트 프롬프트 등)은 여기에 하나 더한다
DETECTORS: dict[str, Callable[[Mapping[str, Any], Path | None, str | None], ObjectDetector]] = {
    "ULTRALYTICS_SEG": _ultralytics_seg,
    "HSV_STOOL": lambda spec, weights_dir, device: HSVStoolDetector(spec),
}


# --------------------------------------------------------------------------- 재기

IMAGE_EDGE = "IMAGE_EDGE"  # 원본(펴진) 그림 마스크 윤곽의 끝점 → 덮개 평면(한 대) 또는 다시점 삼각측량
POINT_CLOUD = "POINT_CLOUD"  # 모델 해상도 MapAnything 점의 띠 끝점(518 이면 원본 4.45 px 가 점 하나)
METHODS = (IMAGE_EDGE, POINT_CLOUD)


@dataclass(frozen=True)
class LidConfig:
    """노트북 윗판 재기 한도. MapAnything 단위(ma, 모델 추정 m), 모델 해상도 픽셀(px), 원본(펴진) 그림 픽셀(image px).
    근거는 설계 문서 표."""

    length_method: str = IMAGE_EDGE  # 근거로 낼 방법(다른 방법은 비교용으로 기록)
    mask_min_fraction: float = 0.5  # 모델 픽셀을 정물로 볼 마스크 덮임 비율
    min_points: int = 30  # 정물 점 하한
    cluster_radius_ma: float = 0.45  # 점 중앙값에서 이보다 먼 점은 배경 번짐으로 뺀다(덮개 대각선 0.44 m)
    max_cluster_drop: float = 0.3  # 위에서 뺀 비율 상한(넘으면 마스크가 다른 물체와 섞임)
    closed_max_height_ma: float = 0.05  # 높이 폭(2~98 백분위)이 이 이하면 닫힌 덮개
    open_min_height_ma: float = 0.10  # 이 이상이면 연 덮개, 사이는 모호해 버린다
    band_ma: float = 0.03  # 윗변 띠·바닥 판 띠 두께
    min_band_points: int = 8
    end_quantile: float = 0.02  # 끝점 백분위 α
    min_edge_px: float = 15.0  # 점구름: 변의 화면 길이 하한(모델 px). 한 칸 오차가 6.7% 이하
    max_edge_width_ratio: float = 0.25  # 윗변 띠의 수직 폭(5~95 백분위) / 길이 상한(직선성)
    min_base_depth_ratio: float = 0.25  # 연 덮개: 바닥 판 띠의 앞뒤 폭 / 윗변 길이 하한(모니터·뒷면만 보임을 거른다)
    closed_max_tilt_deg: float = 10.0  # 닫힌 덮개 판과 바닥의 각 상한(평면성)
    max_plane_rms_ratio: float = 0.05  # 닫힌 덮개 판 평면 rms / 긴 변 상한
    aspect_tolerance: float = 0.10  # 닫힌 덮개 가로/세로 비가 목록 비에서 벗어날 수 있는 비율
    lid_aspect_tolerance: float = 0.5  # 연 덮개 가로/덮개 높이 비. 518 덮개 높이가 ±40% 흔들려(w1) 큰 어긋남만 거른다
    lid_plane_tol_ma: float = 0.02  # 덮개 평면에 속한 점으로 볼 거리
    occlusion_margin_ma: float = 0.05  # 끝점 바로 바깥 표면이 이만큼 더 가까우면 가림
    occlusion_probe_px: int = 3  # 끝점 바깥으로 살피는 픽셀 수
    incomplete_edge_fraction: float = 0.15  # 끝점 너머 이 비율(화면 길이)만큼 바깥까지 마스크가 이어지면 변 일부만 잡은 것
    border_margin_px: float = 2.0  # 끝점이 그림 경계에서 이 안이면 잘림(모델 px, 원본은 × 4)
    min_edge_image_px: float = 50.0  # 원본: 윗변 화면 길이 하한. 끝마다 ±1.5 px 면 길이 σ 약 4%
    edge_search_ma: float = 0.12  # 원본: 덮개 평면에서 점구름 띠 둘레로 윤곽 윗변을 찾는 범위
    edge_select_ma: float = 0.03  # 원본: 윤곽의 위쪽 끝에서 윗변 후보로 고르는 두께
    edge_inlier_px: float = 3.0  # 원본: 윤곽 직선 인라이어(원본 px → 덮개 거리). YOLO 마스크 경계 거칠기 ±2 px 의 1.5 배
    edge_run_gap: int = 10  # 원본: 윗변 토막을 이을 때 허용하는 윤곽 점 간격(경계가 잠깐 직선에서 벗어난 곳)
    min_edge_coverage: float = 0.6  # 원본: 직선 인라이어 수 / 윗변 화면 길이(px) 하한
    person_touch_px: int = 3  # 원본: 윗변 끝점에서 이 거리(px) 안에 사람 마스크가 있으면 닿음
    min_image_to_cloud_ratio: float = 0.85  # 원본: 윤곽 윗변이 점구름 띠 길이의 이 비율보다 짧으면 변 일부만 잡은 것
    min_incidence_deg: float = 10.0  # 원본: 광선과 덮개 평면이 이루는 각 하한(비스듬하면 평면 교차가 흔들림)
    tri_max_residual_ma: float = 0.03  # 삼각측량: 끝점과 각 광선의 거리 상한(넘으면 카메라끼리 안 맞음 → 한 대 값)
    tri_min_angle_deg: float = 5.0  # 삼각측량: 광선 사이 각 하한
    group_radius_ma: float = 0.20  # 같은 정물로 묶는 3D 거리(옆자리 노트북 간격 0.6 m 보다 충분히 작게)


@dataclass(frozen=True)
class ImageContext:
    """검출 하나의 원본(펴진) 그림 정보."""

    mask: np.ndarray  # (H, W) bool 검출 마스크
    image_size: tuple[int, int]  # (폭, 높이)
    person: np.ndarray | None = None  # (H, W) bool 사람 마스크(prepare 가 만든 팽창 마스크), 없으면 None


@dataclass(frozen=True)
class Measurement:
    """검출 하나. status·reason 은 근거로 낼 방법(length_method) 기준이고 방법별 결과는 record["methods"] 에 있다."""

    camera_id: str
    index: int
    status: str
    reason: str | None
    record: dict[str, Any]
    shape: str | None = None
    position: np.ndarray | None = None  # MapAnything world(묶기용)
    scales: Mapping[str, float] = field(default_factory=dict)  # 방법 → 배율(그 방법이 채택했을 때만)
    ends: Mapping[str, Any] | None = None  # 원본 윗변 끝점 광선(삼각측량용): center, dirs(2, 3), points(2, 3), footprint


class LaptopLidMeasurer:
    """연 덮개는 윗변(가로 변), 닫힌 덮개는 두 변. 모든 길이는 MapAnything 단위다."""

    @staticmethod
    def validate(meas: Mapping[str, Any], edges: Mapping[str, float], at: str) -> None:
        if meas.get("open_edge") not in edges:
            raise ValueError(f"{at}: measurement.open_edge 가 edges 에 없다: {meas.get('open_edge')!r}")
        closed = meas.get("closed_edges")
        if not (isinstance(closed, list) and len(closed) == 2 and all(e in edges for e in closed)):
            raise ValueError(f"{at}: measurement.closed_edges 는 edges 의 두 변 [긴 변, 짧은 변] 이다: {closed!r}")
        if edges[closed[0]] < edges[closed[1]]:
            raise ValueError(f"{at}: closed_edges 는 [긴 변, 짧은 변] 순서다")
        known = {f.name for f in fields(LidConfig)}
        extra = set(meas) - {"kind", "open_edge", "closed_edges"} - known
        if extra:
            raise ValueError(f"{at}: measurement 에 모르는 키가 있다: {sorted(extra)}")
        if meas.get("length_method", IMAGE_EDGE) not in METHODS:
            raise ValueError(f"{at}: measurement.length_method 는 {METHODS} 중 하나다: {meas.get('length_method')!r}")

    @staticmethod
    def config(meas: Mapping[str, Any]) -> LidConfig:
        known = {f.name for f in fields(LidConfig)}
        return replace(LidConfig(), **{k: v for k, v in meas.items() if k in known})

    def measure(self, view: View, depth_valid: np.ndarray, mask: np.ndarray, floor: Plane, obj: MeasuredObject,
                index: int, cfg: LidConfig, image: ImageContext | None = None) -> Measurement:
        """mask: 모델 해상도 정물 마스크. image 가 있으면 원본 윤곽 방법(IMAGE_EDGE)도 잰다."""
        rec: dict[str, Any] = {"mask_model_px": int(mask.sum())}
        methods = {m: {"status": REJECTED, "reason": "NOT_MEASURED"} for m in METHODS}
        rec["methods"] = methods
        primary = cfg.length_method

        def done(shape=None, position=None, scales=None, ends=None) -> Measurement:
            st = methods[primary]
            return Measurement(view.camera_id, index, st["status"], st.get("reason"), rec, shape, position,
                               dict(scales or {}), ends)

        def reject(reason: str, **extra: Any) -> Measurement:  # 두 방법 모두 버림
            rec.update(extra)
            for m in METHODS:
                methods[m] = {"status": REJECTED, "reason": reason}
            return done(rec.get("shape"))

        use = mask & depth_valid
        rec["points"] = int(use.sum())
        if use.sum() < cfg.min_points:
            return reject("TOO_FEW_POINTS")
        X, _ = view.points(use)
        med = np.median(X, axis=0)
        keep = np.linalg.norm(X - med, axis=1) <= cfg.cluster_radius_ma
        rec["cluster_drop"] = round(float(1 - keep.mean()), 3)
        if 1 - keep.mean() > cfg.max_cluster_drop or keep.sum() < cfg.min_points:
            return reject("MIXED_SURFACES")
        X = X[keep]
        n = floor.normal
        h = floor.distance(X)
        h2, h98 = np.percentile(h, [2, 98])
        rec["height_ma"] = [round(float(h2), 4), round(float(h98), 4)]
        span = float(h98 - h2)
        e_a, e_b = _plane_basis(n)
        if span <= cfg.closed_max_height_ma:
            return self._closed(view, mask, X, h, n, e_a, e_b, obj, cfg, rec, methods, reject, done, image)
        if span < cfg.open_min_height_ma:
            return reject("AMBIGUOUS_OPENING")
        return self._open(view, mask, X, h, h98, h2, e_a, e_b, obj, cfg, rec, methods, reject, done, image)

    def _open(self, view, mask, X, h, h98, h2, e_a, e_b, obj, cfg, rec, methods, reject, done, image) -> Measurement:
        rec["shape"] = OPEN
        top = X[h >= h98 - cfg.band_ma]
        if len(top) < cfg.min_band_points:
            return reject("TOO_FEW_EDGE_POINTS", edge_points=int(len(top)))
        P = np.column_stack([top @ e_a, top @ e_b])
        c2 = P.mean(axis=0)
        a2 = np.linalg.svd(P - c2, full_matrices=False)[2][0]
        t, r = (P - c2) @ a2, (P - c2) @ np.array([-a2[1], a2[0]])
        a3 = a2[0] * e_a + a2[1] * e_b
        C = top.mean(axis=0)
        L_pc, info = _edge_length(view, mask, C, a3, t, cfg)
        rec["edge"] = info
        rec["edge_points"] = int(len(top))
        if info.get("problem"):  # 잘림·가림: 두 방법 모두
            return reject(info["problem"])
        band_width = float(np.subtract(*np.percentile(r, [95, 5])))
        rec["edge_width_ratio"] = round(band_width / L_pc, 3)
        if band_width > cfg.max_edge_width_ratio * L_pc:  # 점구름 띠가 곧지 않음: 점구름 방법만(원본 윤곽은 직선을 따로 맞춘다)
            info["pc_problem"] = info.get("pc_problem") or "EDGE_NOT_STRAIGHT"
        # 바닥 판(자판): 높이 폭 아래 1/3 의 점이 윗변과 수직인 수평 방향으로 펼쳐져 있어야 한다. 세운 화면(모니터)이나
        # 덮개 뒷면만 보이면 아래쪽 점도 한 평면(수직)이라 펼쳐지지 않는다. 깊이 번짐으로 자판 면이 기울어도 1/3 띠는 잡는다
        base = X[h <= h2 + max(cfg.band_ma, (h98 - h2) / 3)]
        Pb = np.column_stack([base @ e_a, base @ e_b])
        depth_b = float(np.subtract(*np.percentile((Pb - c2) @ np.array([-a2[1], a2[0]]), [95, 5]))) if len(base) else 0.0
        rec["base_depth_ratio"] = round(depth_b / L_pc, 3)
        if len(base) < cfg.min_band_points or depth_b < cfg.min_base_depth_ratio * L_pc:
            return reject("BASE_NOT_SEEN")
        # 덮개 평면: 높이 폭 위쪽 절반의 점(바닥 판은 아래쪽)
        lid_pts = X[h >= h2 + 0.5 * (h98 - h2)]
        cl = lid_pts.mean(axis=0)
        nl = np.linalg.svd(lid_pts - cl, full_matrices=False)[2][2]
        Cp = C - ((C - cl) @ nl) * nl
        u = a3 - (a3 @ nl) * nl
        u /= np.linalg.norm(u)
        w = np.cross(nl, u)
        on_lid = X[np.abs((X - cl) @ nl) <= cfg.lid_plane_tol_ma]
        if np.median((on_lid - Cp) @ w) > 0:
            w = -w  # w 는 덮개 아래(경첩) 쪽이 음수
        lid_height = float(-np.percentile((on_lid - Cp) @ w, 2)) if len(on_lid) >= cfg.min_band_points else 0.0
        rec["lid_height_ma"] = round(lid_height, 4)
        edge = obj.measurement["open_edge"]
        scales: dict[str, float] = {}
        ends = None
        width = L_pc
        # 원본 윤곽 방법
        if image is not None:
            L_img, img_info, ends = _image_open_edge(view, image, Cp, nl, u, w, cfg)
            rec["image_edge"] = img_info
            if L_img is not None and not img_info.get("problem") and L_img < cfg.min_image_to_cloud_ratio * L_pc:
                img_info["problem"] = "IMG_EDGE_PARTIAL"  # 윤곽 직선이 점구름 띠보다 훨씬 짧다(윗변 가운데가 끊김 등)
            if img_info.get("problem"):
                methods[IMAGE_EDGE] = {"status": REJECTED, "reason": img_info["problem"]}
                ends = None
            else:
                width = L_img
                scales[IMAGE_EDGE] = obj.edges[edge] / L_img
                methods[IMAGE_EDGE] = {"status": ACCEPTED, "reason": None, "scale": round(scales[IMAGE_EDGE], 5),
                                       "length_ma": round(L_img, 5)}
        else:
            methods[IMAGE_EDGE] = {"status": REJECTED, "reason": "NO_IMAGE"}
        # 점구름 방법
        if info.get("pc_problem"):
            methods[POINT_CLOUD] = {"status": REJECTED, "reason": info["pc_problem"]}
        else:
            scales[POINT_CLOUD] = obj.edges[edge] / L_pc
            methods[POINT_CLOUD] = {"status": ACCEPTED, "reason": None, "scale": round(scales[POINT_CLOUD], 5),
                                    "length_ma": round(L_pc, 5)}
        # 모양: 덮개 가로 / 덮개 높이(경첩까지)가 목록 비와 맞아야 노트북 덮개(모니터는 1.7~1.8)
        long_e, short_e = obj.measurement["closed_edges"]
        want = obj.edges[long_e] / obj.edges[short_e]
        if lid_height > 0:
            aspect = width / lid_height  # 원본 윤곽을 채택했으면 그 길이, 아니면 점구름 길이
            rec["lid_aspect"] = round(aspect, 3)
            if abs(aspect / want - 1) > cfg.lid_aspect_tolerance:
                return reject("NOT_LAPTOP_SHAPE", lid_aspect_expected=round(want, 3))
        rec["position_ma"] = np.round(C, 4).tolist()
        return done(OPEN, C, scales, ends)

    def _closed(self, view, mask, X, h, n, e_a, e_b, obj, cfg, rec, methods, reject, done, image) -> Measurement:
        rec["shape"] = CLOSED
        # 윗면만(옆면·경계 너머 책상 번짐 점은 윗면보다 낮다)
        X = X[h >= np.percentile(h, 98) - cfg.band_ma / 2]
        rec["top_points"] = int(len(X))
        if len(X) < cfg.min_points:
            return reject("TOO_FEW_POINTS")
        C = X.mean(axis=0)
        normal = np.linalg.svd(X - C, full_matrices=False)[2][2]
        tilt = math.degrees(math.acos(min(1.0, abs(float(normal @ n)))))
        rec["tilt_deg"] = round(tilt, 2)
        if tilt > cfg.closed_max_tilt_deg:
            return reject("NOT_FLAT")
        P = np.column_stack([X @ e_a, X @ e_b])
        c2 = P.mean(axis=0)
        axes2 = _min_area_axes(P - c2, cfg.end_quantile)
        lengths, infos, pc_problem = [], [], None
        for a2 in axes2:
            t = (P - c2) @ a2
            L, info = _edge_length(view, mask, C, a2[0] * e_a + a2[1] * e_b, t, cfg)
            infos.append(info)
            if info.get("problem"):
                rec["edges"] = infos
                return reject(info["problem"])
            pc_problem = pc_problem or info.get("pc_problem")
            lengths.append(L)
        rec["edges"] = infos
        rms = float(np.sqrt(np.mean(((X - C) @ normal) ** 2)))
        rec["plane_rms_ratio"] = round(rms / max(lengths), 4)
        if rms > cfg.max_plane_rms_ratio * max(lengths):
            return reject("NOT_FLAT")
        long_e, short_e = obj.measurement["closed_edges"]
        want = obj.edges[long_e] / obj.edges[short_e]
        scales: dict[str, float] = {}

        def judge(method: str, L1: float, L2: float, problem: str | None) -> None:
            rec.setdefault("aspect", {})[method] = round(L1 / L2, 4)
            if problem is None and abs((L1 / L2) / want - 1) > cfg.aspect_tolerance:
                problem = "ASPECT_MISMATCH"
            if problem:
                methods[method] = {"status": REJECTED, "reason": problem}
                return
            s1, s2 = obj.edges[long_e] / L1, obj.edges[short_e] / L2
            scales[method] = 0.5 * (s1 + s2)
            methods[method] = {"status": ACCEPTED, "reason": None, "scale": round(scales[method], 5),
                               "edges_ma": {long_e: round(L1, 5), short_e: round(L2, 5)},
                               "edge_scales": {long_e: round(s1, 5), short_e: round(s2, 5)}}

        judge(POINT_CLOUD, max(lengths), min(lengths), pc_problem)
        # 원본 윤곽은 닫힌 판에 쓰지 않는다: 비스듬한 카메라에는 판 옆면(두께)이 보여 가까운 쪽 윤곽이 옆면 아래 모서리다.
        # 윗면 평면에 되투영하면 두 변이 두께 × 기울기만큼 길어진다(합성 −5~−8%). 두께를 목록에 재 두면 바로잡을 수 있다
        methods[IMAGE_EDGE] = {"status": REJECTED, "reason": "IMG_CLOSED_LID"}
        if methods[POINT_CLOUD]["reason"] == "ASPECT_MISMATCH":
            return reject("ASPECT_MISMATCH", aspect_expected=round(want, 4))
        rec["position_ma"] = np.round(C, 4).tolist()
        return done(CLOSED, C, scales, None)


# measurement.kind → 재는 쪽. 라바콘(꼭대기·바닥 끝점 높이)은 여기에 하나 더한다
MEASURERS: dict[str, LaptopLidMeasurer | StoolTopMeasurer] = {
    "LAPTOP_LID": LaptopLidMeasurer(), "STOOL_TOP": StoolTopMeasurer(),
}


def _min_area_axes(P: np.ndarray, alpha: float) -> np.ndarray:
    """바닥 위 2D 점 → 백분위 폭의 곱이 가장 작은 직교 두 축(0.5° 간격, 견고한 최소 면적 사각형).

    주성분 축은 원근 때문에 점 밀도가 한쪽으로 쏠리면 사각형 변에서 돌아가 변 길이를 길게 잰다(합성 확인).
    """
    best, best_area = None, np.inf
    for a in np.radians(np.arange(0.0, 90.0, 0.5)):
        u = np.array([math.cos(a), math.sin(a)])
        v = np.array([-u[1], u[0]])
        area = np.ptp(np.percentile(P @ u, [100 * alpha, 100 * (1 - alpha)])) * \
            np.ptp(np.percentile(P @ v, [100 * alpha, 100 * (1 - alpha)]))
        if area < best_area:
            best, best_area = np.vstack([u, v]), area
    return best


def _plane_basis(n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ref = np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0])
    e_a = np.cross(n, ref)
    e_a /= np.linalg.norm(e_a)
    return e_a, np.cross(n, e_a)


def _project(view: View, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    R, c = view.cam2world[:3, :3], view.cam2world[:3, 3]
    Xc = (np.atleast_2d(X) - c) @ R
    uv = (Xc @ view.K.T)[:, :2] / Xc[:, 2:3]
    return uv, Xc[:, 2]


def _edge_length(
    view: View, mask: np.ndarray, C: np.ndarray, a3: np.ndarray, t: np.ndarray, cfg: LidConfig
) -> tuple[float, dict]:
    """점구름 방법: 축 a3 위 좌표 t(점 = 모델 픽셀 중심)의 분포 → 변 길이(MapAnything 단위).

    끝점 α 백분위 폭 ÷ (1 − 2α) = 바깥 픽셀 중심 사이 길이(점이 변을 따라 고르게 깔렸다는 가정). 픽셀 중심은 변 끝에
    못 미친다: 변을 가로지르는 줄이 r 개이고 줄마다 끝 위상이 고르게 다르면 가장 바깥 중심이 끝에서 평균 1 / (r + 1) 칸
    안쪽이라 양 끝 2 / (r + 1) 칸을 더한다(한 줄이면 1 칸, 판 전체처럼 줄이 많으면 0 에 가깝다). r = 점 수 / 화면 길이(px),
    한 칸의 3D 길이 = 길이 / 화면 길이. 남는 치우침은 끝마다 ±0.5 칸 안이다(설계 문서 오차 예산).
    info["problem"](두 방법 모두 버림): 끝점이 그림 경계에 닿음(TRUNCATED), 끝점 바로 바깥(최대 occlusion_probe_px)에 더
    가까운 표면(END_OCCLUDED). info["pc_problem"](점구름 방법만 버림): 끝점에서 변 방향으로 k·k+1 칸(k = 화면 길이의
    incomplete_edge_fraction, 최소 3 칸) 바깥이 모두 아직 정물 마스크(EDGE_INCOMPLETE: 깊이가 흔들려 띠가 변 일부만 잡음),
    화면 길이 < min_edge_px(EDGE_TOO_SHORT_PX).
    """
    a = cfg.end_quantile
    lo, hi = np.percentile(t, [100 * a, 100 * (1 - a)])
    Lc = float(hi - lo) / (1 - 2 * a)
    mid = 0.5 * float(lo + hi)
    ends = np.array([C + a3 * (mid - Lc / 2), C + a3 * (mid + Lc / 2)])
    uv, z = _project(view, ends)
    px = float(np.linalg.norm(uv[1] - uv[0]))
    info: dict[str, Any] = {"length_centers_ma": round(Lc, 5), "edge_px": round(px, 2),
                            "end_px": np.round(uv, 2).tolist()}
    if not (np.all(z > 0) and px > 0):
        info["problem"] = "BEHIND_CAMERA"
        return float("nan"), info
    rows = max(len(t) / px, 1.0)
    L = Lc * (1 + 2 / (rows + 1) / px)
    info |= {"rows": round(rows, 2), "length_ma": round(L, 5)}
    h, w = view.depth.shape
    m = cfg.border_margin_px
    incomplete = False
    for i, (q, other) in enumerate(((uv[0], uv[1]), (uv[1], uv[0]))):
        if not (m <= q[0] <= w - 1 - m and m <= q[1] <= h - 1 - m):
            info["problem"] = "TRUNCATED"
            return L, info
        e = (q - other) / np.linalg.norm(q - other)
        perp = np.array([-e[1], e[0]])
        for k in range(1, cfg.occlusion_probe_px + 1):
            for s in (-1, 0, 1):
                u, v = np.round(q + e * k + perp * s).astype(int)
                if not (0 <= u < w and 0 <= v < h):
                    info["problem"] = "TRUNCATED"
                    return L, info
                d = float(view.depth[v, u])
                if np.isfinite(d) and d > 0 and d < z[i] - cfg.occlusion_margin_ma:
                    info["problem"] = "END_OCCLUDED"
                    return L, info
        k0 = max(3, int(round(cfg.incomplete_edge_fraction * px)))
        beyond = [np.round(q + e * k).astype(int) for k in (k0, k0 + 1)]
        incomplete |= all(0 <= u < w and 0 <= v < h and mask[v, u] for u, v in beyond)
    if incomplete:
        info["pc_problem"] = "EDGE_INCOMPLETE"
    elif px < cfg.min_edge_px:
        info["pc_problem"] = "EDGE_TOO_SHORT_PX"
    return L, info


# --------------------------------------------------------------------------- 원본 윤곽(IMAGE_EDGE)


def _rays(view: View, image: ImageContext, uv: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """원본(펴진) 그림 픽셀 → MapAnything world 광선(중심, 단위 방향). 점구름과 같은 규약(모델 픽셀·모델 K)."""
    model = prepared_to_model_px(uv, image.image_size, view.depth.shape)
    rc = np.linalg.solve(view.K, np.vstack([model[:, 0], model[:, 1], np.ones(len(model))]))
    d = (view.cam2world[:3, :3] @ rc).T
    return view.cam2world[:3, 3].copy(), d / np.linalg.norm(d, axis=1, keepdims=True)


def _contour(mask: np.ndarray) -> np.ndarray:
    """가장 큰 바깥 윤곽(경계 픽셀 좌표, 원본 px)."""
    import cv2

    cs, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cs:
        return np.zeros((0, 2))
    return max(cs, key=cv2.contourArea).reshape(-1, 2).astype(float)


def _on_plane(o: np.ndarray, d: np.ndarray, p0: np.ndarray, nrm: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    den = d @ nrm
    with np.errstate(divide="ignore", invalid="ignore"):
        t = ((p0 - o) @ nrm) / den
    ok = np.isfinite(t) & (t > 0) & (np.abs(den) > 1e-6)
    return o + d * t[:, None], ok


def _footprint(view: View, image: ImageContext, X: np.ndarray) -> tuple[float, np.ndarray]:
    """점 X 자리에서 원본 1 px 의 3D 길이(카메라 거리 × 픽셀 각)와 X 의 원본 픽셀."""
    uv_m, _ = _project(view, X[None])
    w, h = image.image_size
    hh, ww = view.depth.shape
    # 모델 픽셀 → 원본 픽셀(prepared_to_model_px 의 역)과 옆 픽셀의 광선 각
    probe = np.array([[0.0, 0.0], [1.0, 0.0]])
    q = prepared_to_model_px(probe, (w, h), (hh, ww))
    per = float(q[1, 0] - q[0, 0])  # 모델 px / 원본 px
    uv = (uv_m[0] - q[0]) / per
    o, d = _rays(view, image, np.array([uv, uv + [1.0, 0.0]]))
    ang = math.acos(min(1.0, float(d[0] @ d[1])))
    return float(np.linalg.norm(X - o)) * ang, uv


def _image_end_checks(image: ImageContext, ends_px: np.ndarray, cfg: LidConfig) -> str | None:
    w, h = image.image_size
    m = cfg.border_margin_px * 4
    for u, v in ends_px:
        if not (m <= u <= w - 1 - m and m <= v <= h - 1 - m):
            return "TRUNCATED"
        if image.person is not None:
            r = cfg.person_touch_px
            ui, vi = int(round(u)), int(round(v))
            if image.person[max(vi - r, 0):vi + r + 1, max(ui - r, 0):ui + r + 1].any():
                return "END_ON_PERSON"  # 윗변 끝이 사람 마스크에 닿음: 끝이 가렸을 수 있다
    return None


def _longest_run(indices: np.ndarray, n: int, gap: int) -> np.ndarray:
    """닫힌 윤곽(길이 n)의 점 번호 중 간격 gap 이하로 이어진 가장 큰 토막의 번호들."""
    idx = np.unique(indices)
    if len(idx) == 0:
        return idx
    d = np.diff(np.concatenate([idx, [idx[0] + n]]))
    breaks = np.nonzero(d > gap)[0]
    if len(breaks) == 0:
        return idx
    start = (breaks[0] + 1) % len(idx)  # 끊긴 곳 다음부터 돌면 둘레를 넘는 토막도 한 토막이다
    order = np.roll(idx, -start)
    dd = np.diff(np.concatenate([order, [order[0] + n]])) % n
    runs, cur = [], [order[0]]
    for k in range(1, len(order)):
        if dd[k - 1] <= gap:
            cur.append(order[k])
        else:
            runs.append(cur)
            cur = [order[k]]
    runs.append(cur)
    return np.array(max(runs, key=len))


def _image_open_edge(view: View, image: ImageContext, Cp: np.ndarray, nl: np.ndarray, u: np.ndarray, w: np.ndarray,
                     cfg: LidConfig) -> tuple[float | None, dict[str, Any], dict[str, Any] | None]:
    """연 덮개 윗변: 원본 마스크 윤곽을 덮개 평면에 되투영해 윗변 선(점구름 띠 Cp·u) 근처 점으로 직선을 맞추고 양 끝을 잰다.

    윤곽 픽셀은 경계 안쪽 픽셀이라 양 끝 반 칸씩(원본 1 px)의 3D 길이를 더한다. 끝점 광선은 삼각측량에 쓴다.
    """
    info: dict[str, Any] = {}
    foot, uvC = _footprint(view, image, Cp)
    o, dC = _rays(view, image, uvC[None])
    inc = math.degrees(math.asin(min(1.0, abs(float(dC[0] @ nl)))))
    info |= {"incidence_deg": round(inc, 2), "footprint_ma": round(foot, 5)}
    if inc < cfg.min_incidence_deg:
        info["problem"] = "IMG_GRAZING"
        return None, info, None
    cont = _contour(image.mask)
    if len(cont) < 10:
        info["problem"] = "IMG_EDGE_NOT_FOUND"
        return None, info, None
    o, d = _rays(view, image, cont)
    P, ok = _on_plane(o, d, Cp, nl)
    cu, cw = (P - Cp) @ u, (P - Cp) @ w
    # 윗변 = 덮개 평면에서 윤곽의 가장 위쪽 띠. 점구름 띠는 깊이 번짐으로 실제 윗변보다 몇 cm 낮게 잡히므로(w1 약 5 cm)
    # 띠 둘레(±edge_search_ma)의 윤곽 중 위쪽 끝(99 백분위)에서 edge_select_ma 안을 고른다
    cand = ok & (np.abs(cw) <= cfg.edge_search_ma) & (np.abs(cu) <= 0.4)
    if cand.sum() < 10:
        info["problem"] = "IMG_EDGE_NOT_FOUND"
        return None, info, None
    w_top = float(np.percentile(cw[cand], 99))
    info["top_offset_ma"] = round(w_top, 4)
    sel = cand & (cw >= w_top - cfg.edge_select_ma)
    if sel.sum() < 10:
        info["problem"] = "IMG_EDGE_NOT_FOUND"
        return None, info, None
    # 처음 후보(위쪽 띠)로 직선을 맞춘 뒤, 띠 방향이 실제 윗변과 조금 틀어져 띠 밖으로 나간 윗변 부분을 직선 거리로 다시
    # 고른다(후보 전체에서). 옆 변(모서리 근처)·바닥 판 윤곽은 직선에서 멀어 빠진다
    Q = np.column_stack([cu, cw])[cand]
    idx = np.nonzero(cand)[0]
    keep = sel[cand]
    tol = cfg.edge_inlier_px * foot
    for k, lim in enumerate((3 * tol, tol, tol, tol)):
        c = Q[keep].mean(axis=0)
        dvec = np.linalg.svd(Q[keep] - c, full_matrices=False)[2][0]
        r = np.abs((Q - c) @ np.array([-dvec[1], dvec[0]]))
        keep = (r <= lim) & (keep if k == 0 else True)
        if keep.sum() < 10:
            info["problem"] = "IMG_EDGE_NOT_FOUND"
            return None, info, None
    # 윗변은 윤곽을 따라 이어진 한 토막이다: 인라이어 중 윤곽 순서로 가장 긴 연속 토막만 쓴다(비스듬한 광선이 평면에
    # 멀리 떨어뜨린 다른 윤곽점이 우연히 직선 위에 오는 것을 뺀다)
    run = _longest_run(idx[keep], len(cont), gap=cfg.edge_run_gap)
    keep &= np.isin(idx, run)
    s = (Q - c) @ dvec
    s_in = np.where(keep, s, np.nan)
    i0, i1 = int(np.nanargmin(s_in)), int(np.nanargmax(s_in))
    L = float(s[i1] - s[i0]) + foot
    ends_px = cont[idx[[i0, i1]]]
    edge_px = float(np.linalg.norm(ends_px[1] - ends_px[0]))
    coverage = float(keep.sum()) / max(edge_px, 1.0)
    info |= {"length_ma": round(L, 5), "edge_image_px": round(edge_px, 1), "inliers": int(keep.sum()),
             "coverage": round(coverage, 2), "ends_px": np.round(ends_px, 1).tolist(),
             "line_rms_ma": round(float(np.sqrt(np.mean(r[keep] ** 2))), 5)}
    problem = _image_end_checks(image, ends_px, cfg)
    if problem is None and coverage < cfg.min_edge_coverage:
        problem = "IMG_EDGE_NOT_STRAIGHT"
    if problem is None and edge_px < cfg.min_edge_image_px:
        problem = "IMG_EDGE_TOO_SHORT"
    if problem:
        info["problem"] = problem
        return L, info, None
    ends = {"center": o, "dirs": d[idx[[i0, i1]]], "points": P[idx[[i0, i1]]], "footprint": foot}
    return L, info, ends


def _triangulate(group: Sequence[Measurement], cfg: LidConfig) -> tuple[float | None, dict[str, Any]]:
    """같은 노트북의 원본 윗변 끝점(카메라 2대 이상) → 끝점 삼각측량 길이. 끝점 짝은 덮개 평면 교점의 3D 거리로 맞춘다."""
    members = [m for m in group if m.ends is not None]
    cams = {m.camera_id for m in members}
    rec: dict[str, Any] = {"cameras": sorted(cams)}
    if len(cams) < 2:
        rec["status"] = "NOT_ENOUGH_VIEWS"
        return None, rec
    by_cam = {}
    for m in members:  # 카메라마다 하나(겹친 검출은 첫 것)
        by_cam.setdefault(m.camera_id, m)
    ref = by_cam[sorted(by_cam)[0]]
    rays: list[list[tuple[np.ndarray, np.ndarray]]] = [[], []]
    foot = []
    for m in by_cam.values():
        P = m.ends["points"]
        straight = np.linalg.norm(P[0] - ref.ends["points"][0]) + np.linalg.norm(P[1] - ref.ends["points"][1])
        swapped = np.linalg.norm(P[1] - ref.ends["points"][0]) + np.linalg.norm(P[0] - ref.ends["points"][1])
        order = (0, 1) if straight <= swapped else (1, 0)
        for k, j in enumerate(order):
            rays[k].append((m.ends["center"], m.ends["dirs"][j]))
        foot.append(m.ends["footprint"])
    pts, resid, angles = [], [], []
    for rs in rays:
        A, b = np.zeros((3, 3)), np.zeros(3)
        for o, d in rs:
            M = np.eye(3) - np.outer(d, d)
            A += M
            b += M @ o
        X = np.linalg.solve(A, b)
        pts.append(X)
        resid.append(max(float(np.linalg.norm((np.eye(3) - np.outer(d, d)) @ (X - o))) for o, d in rs))
        angles.append(max(math.degrees(math.acos(min(1.0, abs(float(d1 @ d2)))))
                          for i, (_, d1) in enumerate(rs) for _, d2 in rs[i + 1:]))
    L = float(np.linalg.norm(pts[1] - pts[0])) + float(np.mean(foot))
    rec |= {"length_ma": round(L, 5), "residual_ma": [round(r, 4) for r in resid],
            "angle_deg": [round(a, 2) for a in angles]}
    if max(resid) > cfg.tri_max_residual_ma:
        rec["status"] = "RAYS_DISAGREE"
        return None, rec
    if min(angles) < cfg.tri_min_angle_deg:
        rec["status"] = "NARROW_ANGLE"
        return None, rec
    rec["status"] = "OK"
    return L, rec


# --------------------------------------------------------------------------- 묶기·근거


def floor_plane(run: MapAnythingRun, cfg: WorldFrameConfig = WorldFrameConfig()) -> tuple[Plane | None, list]:
    """`fix_world_frame` 과 같은 점 고르기·난수로 찾은 바닥(같은 실행·같은 사람 마스크면 같은 평면)."""
    rng = np.random.default_rng(cfg.seed)
    pts = run.all_points()
    pts = pts[np.isfinite(pts).all(axis=1)]
    if len(pts) < 100:
        return None, []
    if len(pts) > cfg.max_points:
        pts = pts[rng.choice(len(pts), cfg.max_points, replace=False)]
    c2w = run.cam2world()
    ids = sorted(c2w)
    centers = np.array([np.asarray(c2w[c])[:3, 3] for c in ids])
    return find_floor(pts, centers, _up_prior([c2w[c] for c in ids]), cfg, rng)


def group_measurements(ms: Sequence[Measurement], radius: float) -> list[list[Measurement]]:
    """어느 방법이든 배율을 낸 측정을 3D 위치로 묶는다(거리 radius 안이면 같은 정물, 단일 연결)."""
    ok = [m for m in ms if m.scales and m.position is not None]
    parent = list(range(len(ok)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(ok)):
        for j in range(i + 1, len(ok)):
            if np.linalg.norm(ok[i].position - ok[j].position) <= radius:
                parent[root(i)] = root(j)
    groups: dict[int, list[Measurement]] = {}
    for i, m in enumerate(ok):
        groups.setdefault(root(i), []).append(m)
    return sorted(groups.values(), key=lambda g: (g[0].camera_id, g[0].index))


def aggregate(obj: MeasuredObject, values: Sequence[float]) -> tuple[float | None, int, dict[str, Any]]:
    """독립 표본 값(묶음마다 하나) → (배율, 표본 수, 기록). 못 정하면 배율 None 과 사유."""
    values = np.asarray(values, dtype=float)
    rec: dict[str, Any] = {"samples": len(values), "min_samples": obj.min_samples, "tolerance": obj.agreement_tolerance,
                           "max_outlier_fraction": obj.max_outlier_fraction,
                           "sample_scales": [round(float(v), 5) for v in values]}
    if len(values) < obj.min_samples:
        rec["rejected"] = f"TOO_FEW_SAMPLES: 독립 표본 {len(values)}개 < {obj.min_samples}개"
        return None, 0, rec
    med = float(np.median(values))
    out = np.abs(values / med - 1) > obj.agreement_tolerance
    rec |= {"median_all": round(med, 6), "spread_all": round(float(np.max(np.abs(values / med - 1))), 4),
            "excluded": int(out.sum())}
    if out.sum() > obj.max_outlier_fraction * len(values):
        rec["rejected"] = (f"SPREAD: 중앙값에서 {obj.agreement_tolerance:.0%} 넘게 벗어난 표본 {int(out.sum())}/{len(values)}개 "
                           f"> {obj.max_outlier_fraction:.0%}")
        return None, 0, rec
    kept = values[~out]
    if len(kept) < obj.min_samples:
        rec["rejected"] = f"TOO_FEW_SAMPLES: 벗어난 표본을 빼면 {len(kept)}개 < {obj.min_samples}개"
        return None, 0, rec
    s = float(np.median(kept))
    spread = float(np.max(np.abs(kept / s - 1)))
    mad = 1.4826 * float(np.median(np.abs(kept - s))) / s
    rec |= {"scale": round(s, 6), "spread": round(spread, 4), "robust_sigma": round(mad, 4),
            "median_standard_error": round(1.2533 * mad / math.sqrt(len(kept)), 4)}
    if spread > obj.agreement_tolerance:
        rec["rejected"] = f"SPREAD: 남은 표본끼리 {spread:.1%} > {obj.agreement_tolerance:.0%}"
        return None, 0, rec
    return s, int(len(kept)), rec


def _group_value(obj: MeasuredObject, group: Sequence[Measurement], method: str, cfg: LidConfig
                 ) -> tuple[float | None, dict[str, Any]]:
    vals = [m.scales[method] for m in group if method in m.scales]
    rec: dict[str, Any] = {}
    if method == IMAGE_EDGE:
        L, tri = _triangulate([m for m in group if method in m.scales], cfg)
        rec["triangulation"] = tri
        if L is not None:
            rec |= {"source": "TRIANGULATED", "single_view": [round(v, 5) for v in vals]}
            return obj.edges[obj.measurement["open_edge"]] / L, rec
    if not vals:
        return None, rec
    rec |= {"source": "SINGLE_VIEW_MEDIAN", "single_view": [round(v, 5) for v in vals]}
    return float(np.median(vals)), rec


def measure_object(
    obj: MeasuredObject,
    points_run: MapAnythingRun,
    floor: Plane,
    detections: Mapping[str, Sequence[Detection]],
    image_sizes: Mapping[str, Sequence[int]],
    person_masks: Mapping[str, np.ndarray] | None = None,
) -> tuple[float | None, int, dict[str, Any]]:
    """한 정물: 카메라별 검출 → 측정(두 방법) → 묶기 → 방법별 근거. 근거로 내는 것은 length_method 의 값이다.

    points_run 은 사람 마스크를 적용하지 않은 실행(정물 앞 사람 점이 남아야 끝점 가림을 본다). image_sizes 는 camera_id →
    펴진 그림 [폭, 높이], person_masks 는 camera_id → 펴진 그림 해상도 사람 마스크(있으면 윗변 끝점이 닿을 때만 버린다).
    """
    measurer = MEASURERS[obj.measurement["kind"]]
    if isinstance(measurer, StoolTopMeasurer):
        return _measure_stool_run(obj, points_run, floor, detections, image_sizes, person_masks)
    cfg = measurer.config(obj.measurement)
    ms: list[Measurement] = []
    for view in points_run.views:
        dets = detections.get(view.camera_id, ())
        w, h = (int(x) for x in image_sizes[view.camera_id])
        person = (person_masks or {}).get(view.camera_id)
        for i, det in enumerate(dets):
            if det.mask.shape != (h, w):
                raise ValueError(f"{view.camera_id}: 검출 마스크 크기 {det.mask.shape[::-1]} 가 펴진 그림 {w}×{h} 와 다르다")
            ys, xs = np.nonzero(det.mask)
            if len(xs) and (xs.min() == 0 or ys.min() == 0 or xs.max() == w - 1 or ys.max() == h - 1):
                rec = {"note": "마스크가 그림 경계에 닿음",
                       "methods": {m: {"status": REJECTED, "reason": "TRUNCATED"} for m in METHODS}}
                ms.append(Measurement(view.camera_id, i, REJECTED, "TRUNCATED", rec))
                continue
            mask = mask_to_model_res(det.mask, view.depth.shape, min_fraction=cfg.mask_min_fraction)
            m = measurer.measure(view, view.valid, mask, floor, obj, i, cfg, ImageContext(det.mask, (w, h), person))
            m.record["confidence"] = round(det.confidence, 3)
            ms.append(m)
    groups = group_measurements(ms, cfg.group_radius_ma)
    by_method: dict[str, Any] = {}
    result: tuple[float | None, int] = (None, 0)
    for method in METHODS:
        values, group_recs = [], []
        for g in groups:
            v, grec = _group_value(obj, g, method, cfg)
            members = [m for m in g if method in m.scales]
            if v is None:
                continue
            values.append(v)
            sv = np.array([m.scales[method] for m in members])
            group_recs.append({"members": [f"{m.camera_id}#{m.index}" for m in members],
                               "shape": sorted({m.shape for m in members}), "scale": round(v, 5),
                               "member_spread": round(float(np.max(np.abs(sv / np.median(sv) - 1))), 4),
                               "position_ma": np.round(np.mean([m.position for m in members], axis=0), 4).tolist(),
                               **grec})
        scale, count, agg = aggregate(obj, values)
        reasons: dict[str, int] = {}
        for m in ms:
            st = m.record["methods"][method]
            if st["status"] == REJECTED:
                reasons[st["reason"]] = reasons.get(st["reason"], 0) + 1
        by_method[method] = {
            "status": STATUS_OK if scale is not None else STATUS_REJECTED, "reason": agg.get("rejected"),
            "accepted": sum(m.record["methods"][method]["status"] == ACCEPTED for m in ms),
            "rejected_by_reason": dict(sorted(reasons.items())), "groups": group_recs, "aggregation": agg,
        }
        if method == cfg.length_method:
            result = (scale, count)
    primary = by_method[cfg.length_method]
    rec = {
        "id": obj.id, "method": cfg.length_method, "status": primary["status"], "reason": primary["reason"],
        "detections": len(ms), "accepted": primary["accepted"], "rejected_by_reason": primary["rejected_by_reason"],
        "groups": primary["groups"], "aggregation": primary["aggregation"], "by_method": by_method,
        "config": cfg.__dict__.copy(),
        "measurements": [{"camera_id": m.camera_id, "index": m.index, "status": m.status, "reason": m.reason, **m.record}
                         for m in ms],
    }
    return result[0], result[1], rec


def _measure_stool_run(obj: MeasuredObject, run: MapAnythingRun, floor: Plane,
                       detections: Mapping[str, Sequence[Detection]], image_sizes: Mapping[str, Sequence[int]],
                       person_masks: Mapping[str, np.ndarray] | None) -> tuple[float | None, int, dict[str, Any]]:
    """MapAnything 모델 K 를 준비 영상 해상도로 옮겨 공통 스툴 측정기에 전달한다."""
    from simai_ai.calibration.world_frame.stool_tops import measure_stools
    from simai_ai.geometry.rig import RigCamera

    cameras = {}
    for view in run.views:
        w, h = image_sizes[view.camera_id]
        # prepared_to_model_px 는 아핀 변환이다. 역행렬로 모델 K → 준비 영상 K.
        p = prepared_to_model_px(np.array([[0., 0.], [1., 0.], [0., 1.]]), (w, h), view.depth.shape)
        A = np.array([[p[1, 0] - p[0, 0], 0, p[0, 0]], [0, p[2, 1] - p[0, 1], p[0, 1]], [0, 0, 1.]])
        K = np.linalg.solve(A, view.K)
        R = view.cam2world[:3, :3].T
        t = -R @ view.cam2world[:3, 3]
        cameras[view.camera_id] = RigCamera.from_calibration({
            "camera_id": view.camera_id, "image_size": [w, h], "K": K.tolist(), "R": R.tolist(), "t": t.tolist(),
            "distortion": {"model": "NONE", "coeffs": []},
        })
    return measure_stools(obj, cameras, detections, normal=floor.normal, offset=-floor.offset, person_masks=person_masks)


def build_evidence(
    objects: Sequence[MeasuredObject],
    run_with_masks: MapAnythingRun,
    points_run: MapAnythingRun,
    detections: Mapping[str, Mapping[str, Sequence[Detection]]],
    image_sizes: Mapping[str, Sequence[int]],
    *,
    input_provenance: Mapping[str, Any] | None,
    catalog_record: Mapping[str, Any],
    detectors: Mapping[str, Any],
    created_at: int,
    person_masks: Mapping[str, np.ndarray] | None = None,
    floor_cfg: WorldFrameConfig = WorldFrameConfig(),
) -> dict[str, Any]:
    """정물들 → `fix --scale-evidence` 입력(`simai.scale_evidence`). 바닥은 `fix` 와 같은 사람 마스크 적용 실행에서 찾는다.

    detections: 정물 id → camera_id → 검출. 근거를 못 내면 estimates 가 비고 사유가 objects 에 남는다.
    """
    floor, tried = floor_plane(run_with_masks, floor_cfg)
    doc: dict[str, Any] = {
        "format": EVIDENCE_FORMAT, "format_version": EVIDENCE_VERSION, "created_at": created_at,
        "cameras_json_sha256": run_with_masks.record["cameras_json"]["sha256"],
        "mapanything": {"run_dir": run_with_masks.record["run_dir"], "raw_npz_sha256": run_with_masks.record["raw_npz_sha256"]},
        "catalog": dict(catalog_record), "detectors": dict(detectors), "estimates": [], "objects": [],
        "note": "배율 = 목록 길이 / MapAnything 길이. 같은 MapAnything 실행(cameras_json_sha256)에서만 쓴다",
    }
    if input_provenance is not None:
        doc["input_provenance"] = dict(input_provenance)
    if floor is None:
        doc["floor"] = {"status": "FLOOR_NOT_FOUND", "search": tried}
        for o in objects:
            doc["objects"].append({"id": o.id, "status": STATUS_REJECTED, "reason": "FLOOR_NOT_FOUND"})
        return doc
    centers = {v.camera_id: v.cam2world[:3, 3] for v in run_with_masks.views}
    doc["floor"] = {"normal_ma": np.round(floor.normal, 6).tolist(), "offset_ma": round(floor.offset, 5),
                    "inliers": floor.inliers, "rms_ma": round(floor.rms, 4),
                    "camera_heights_ma": {c: round(float(floor.distance(x[None])[0]), 4) for c, x in sorted(centers.items())}}
    for o in objects:
        scale, count, rec = measure_object(o, points_run, floor, detections.get(o.id, {}), image_sizes, person_masks)
        heights = doc["floor"]["camera_heights_ma"]
        for mrec in rec["by_method"].values():  # 방법마다 그 배율로 본 카메라 높이(비교용)
            s = (mrec["aggregation"] or {}).get("scale") or (mrec["aggregation"] or {}).get("median_all")
            if s:
                mrec["camera_heights_m_at_median"] = {c: round(s * hh, 3) for c, hh in heights.items()}
        if scale is not None:
            doc["estimates"].append({"basis": "MEASURED_OBJECT", "scale": round(scale, 6), "sample_count": count,
                                     "label": o.id})
            rec["camera_heights_m"] = {c: round(scale * hh, 3) for c, hh in heights.items()}
        doc["objects"].append(rec)
    return doc


def draw_diagnostics(image_bgr: np.ndarray, detections: Sequence[Detection], measurements: Sequence[Mapping[str, Any]]
                     ) -> np.ndarray:
    """진단 그림(커밋하지 않음): 검출 윤곽(채택 초록·버림 빨강), 번호·배율 또는 버림 사유, 원본 윗변 끝점."""
    import cv2

    out = image_bgr.copy()
    if any("corners_px" in m for m in measurements):
        from simai_ai.calibration.world_frame.stool_tops import draw_stools

        return draw_stools(image_bgr, measurements)
    recs = {m["index"]: m for m in measurements}
    for i, det in enumerate(detections):
        m = recs.get(i, {})
        ok = m.get("status") == ACCEPTED
        color = (0, 200, 0) if ok else (0, 0, 255)
        cs, _ = cv2.findContours(det.mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        cv2.drawContours(out, cs, -1, color, 2)
        ys, xs = np.nonzero(det.mask)
        if not len(xs):
            continue
        meth = (m.get("methods") or {})
        label = f"#{i} " + (f"{meth.get(IMAGE_EDGE, {}).get('scale')}" if ok else str(m.get("reason")))
        pc = meth.get(POINT_CLOUD, {})
        label += f" pc:{pc.get('scale') or pc.get('reason')}"
        cv2.putText(out, label, (int(xs.min()), max(int(ys.min()) - 6, 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        for u, v in (m.get("image_edge") or {}).get("ends_px", []):
            cv2.circle(out, (int(round(u)), int(round(v))), 6, (255, 255, 0), 2)
    return out


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()
