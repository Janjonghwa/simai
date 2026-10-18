"""측정값 계산 (명세 3.3 카메라 사양, 3.9 촬영 시각, 12.7 camera_to_gateway_gap).

받은 압축 프레임 하나하나의 관측(Observation)에서 보고서 수치를 만든다. 입력·시계와 분리한 순수 계산이라
카메라 없이 시험한다. 모르는 값은 null 로 둔다(0 이나 기본값으로 채우지 않는다).

- 프레임률: 도착 기준(노트북 시계)과 PTS 기준(카메라 RTP 시계)을 따로 낸다.
- 도착 간격: 노트북 단조 시계로 잰다(NTP 보정으로 벽시계가 뛰어도 흔들리지 않게).
- camera_to_gateway_gap: 도착 간격이 공칭 간격 × GAP_FACTOR 를 넘은 횟수·최대. 기준은 녹화 manifest·영상 중계와 같다.
- PTS: 간격 분포, 역행(뒤로 감)·반복, 빈칸(카메라가 보내지 않았거나 잃은 프레임), PTS 없는 패킷.
- 키프레임 주기: 키프레임 PTS 간격(PTS 가 모자라면 도착 시각).
- RTCP: Sender Report 가 왔는지, SR 마다 카메라 시각–RTP 대응이 얼마나 흔들리는지, 노트북 수신 시각과의 차이.
  촬영 시각 출처는 SR 이 충분히 오고 대응이 일관될 때만 CAMERA_STREAM 후보로 적는다. 확정은 G0 LED 시험이다(3.9).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from simai_recorder.capture_time import CaptureTimeModel, ReceiveTimeEstimator
from simai_recorder.manifest import GAP_FACTOR
from simai_recorder.sources import StreamInfo

# 연속 SR 사이 카메라 시각–RTP 대응의 흔들림 허용치. 촬영 시각 불확실성 기준(25ms, 3.9)보다 충분히 작게 잡는다.
SR_STEP_TOLERANCE_MS = 5.0
# 대응이 일관된지 보려면 SR 사이 간격이 최소 2개는 있어야 한다. 그보다 적으면 판단하지 않는다(모르면 후보로 올리지 않음).
MIN_SENDER_REPORTS = 3
# 카메라 시계가 설정돼 있는지(1970 년 같은 값이 아닌지) 보는 느슨한 기준. 오프셋 자체는 그대로 보고한다.
PLAUSIBLE_CLOCK_DIFF_MS = 24 * 3600 * 1000

RECEIVE_ESTIMATED = "RECEIVE_ESTIMATED"
CAMERA_STREAM_CANDIDATE = "CAMERA_STREAM_CANDIDATE"


@dataclass(frozen=True)
class Observation:
    """받은 압축 프레임 하나. received_mono_s 는 노트북 단조 시계(초), received_at_ms 는 노트북 UTC ms."""

    received_mono_s: float
    received_at_ms: int
    pts: int | None
    keyframe: bool
    size: int
    camera_time_us: int | None = None
    sender_report: bool = False


def pct(values: Sequence[float], q: float) -> float | None:
    """가장 가까운 순위 백분위(아래쪽). 값이 없으면 None."""
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q / 100 * len(s)))], 3)


def _r(v: float | None, nd: int = 3) -> float | None:
    return None if v is None else round(v, nd)


def stream_section(info: StreamInfo | None) -> dict[str, Any] | None:
    if info is None:
        return None
    return {
        "codec": info.codec,
        "profile": info.profile,
        "width": info.width,
        "height": info.height,
        "nominal_fps_declared": info.nominal_fps,
        "pts_timebase": None if info.pts_timebase is None else f"{info.pts_timebase.numerator}/{info.pts_timebase.denominator}",
        "extradata_present": bool(info.extradata),
    }


def summarize(obs: Sequence[Observation], info: StreamInfo | None) -> dict[str, Any]:
    """관측 목록 → 보고서의 metrics·rtcp·timestamp_source 절."""
    tb_ms = float(info.pts_timebase * 1000) if info is not None and info.pts_timebase is not None else None
    out: dict[str, Any] = {"frames": len(obs)}
    warnings: list[str] = []
    if len(obs) < 2:
        warnings.append("프레임이 2개 미만이라 간격·프레임률을 계산하지 않았다")
        out.update(_empty_metrics())
        out["rtcp"] = rtcp_section(obs, tb_ms)
        out.update(timestamp_source(out["rtcp"]))
        out["warnings"] = warnings
        return out

    span_s = obs[-1].received_mono_s - obs[0].received_mono_s
    arrival_iv = [(b.received_mono_s - a.received_mono_s) * 1000 for a, b in zip(obs, obs[1:])]

    # PTS (카메라 RTP 시계)
    with_pts = [o for o in obs if o.pts is not None]
    no_pts = len(obs) - len(with_pts)
    pts_deltas = [] if tb_ms is None else [(b.pts - a.pts) * tb_ms for a, b in zip(with_pts, with_pts[1:])]
    positive = [d for d in pts_deltas if d > 0]
    nominal_ms = statistics.median(positive) if positive else None
    if nominal_ms is None and info is not None and info.nominal_fps:
        nominal_basis = "declared_fps"
        nominal_ms = 1000 / info.nominal_fps
    else:
        nominal_basis = "pts" if nominal_ms is not None else None
    regressions = [d for d in pts_deltas if d < 0]
    repeats = sum(1 for d in pts_deltas if d == 0)
    gap_threshold = round(nominal_ms * GAP_FACTOR, 1) if nominal_ms else None
    # 빈칸: 공칭 간격 × GAP_FACTOR 를 넘는 PTS 증가. 역행 바로 다음의 증가는 순서가 바뀐 것을 되돌린 것이라 빈칸으로 세지 않는다.
    pts_gaps = [d for k, d in enumerate(pts_deltas)
                if nominal_basis == "pts" and gap_threshold is not None and d > gap_threshold
                and not (k > 0 and pts_deltas[k - 1] < 0)]
    missing = sum(max(0, round(d / nominal_ms) - 1) for d in pts_gaps) if pts_gaps else 0
    pts_span_ms = sum(positive) if positive else None

    # 키프레임
    keys = [i for i, o in enumerate(obs) if o.keyframe]
    key_pts = [obs[i].pts for i in keys if obs[i].pts is not None]
    if tb_ms is not None and len(key_pts) >= 2:
        key_iv_s = [(b - a) * tb_ms / 1000 for a, b in zip(key_pts, key_pts[1:]) if b > a]
        key_basis = "pts"
    else:
        key_iv_s = [obs[b].received_mono_s - obs[a].received_mono_s for a, b in zip(keys, keys[1:])]
        key_basis = "arrival" if len(keys) >= 2 else None

    # 수신 시각 − 추정 촬영 시각 (녹화·중계와 같은 추정기, 고정 지연 0). Wi-Fi 도착 흔들림이 프레임마다 얼마나 남는지.
    receive_delay: list[float] = []
    if info is not None:
        est = ReceiveTimeEstimator(CaptureTimeModel(), info.pts_timebase)
        receive_delay = [o.received_at_ms - est.estimate(o.received_at_ms, o.pts).estimated_capture_time_ms
                         for o in obs if o.pts is not None]

    total_bytes = sum(o.size for o in obs)
    gaps = [a for a in arrival_iv if gap_threshold is not None and a > gap_threshold]
    out.update({
        "duration_s": _r(span_s, 2),
        "fps_by_arrival": _r((len(obs) - 1) / span_s) if span_s > 0 else None,
        "fps_by_pts": _r(1000 / nominal_ms) if nominal_ms and nominal_basis == "pts" else None,
        "nominal_interval_ms": _r(nominal_ms),
        "nominal_interval_basis": nominal_basis,
        "arrival_interval_ms": {"p50": pct(arrival_iv, 50), "p95": pct(arrival_iv, 95), "p99": pct(arrival_iv, 99),
                                "max": _r(max(arrival_iv))},
        "camera_to_gateway_gap": {"threshold_ms": gap_threshold, "count": len(gaps) if gap_threshold else None,
                                  "max_ms": _r(max(arrival_iv), 1)},
        "pts": {
            "packets_without_pts": no_pts,
            "interval_ms": {"p50": pct(positive, 50), "p95": pct(positive, 95),
                            "max": _r(max(positive)) if positive else None},
            "regressions": len(regressions) if tb_ms is not None else None,
            "max_regression_ms": _r(-min(regressions)) if regressions else None,
            "repeats": repeats if tb_ms is not None else None,
            "gaps": len(pts_gaps) if nominal_basis == "pts" else None,
            "missing_frames_estimate": missing if nominal_basis == "pts" else None,
        },
        "keyframes": len(keys),
        "packets_before_first_keyframe": keys[0] if keys else len(obs),
        "keyframe_interval_s": {"p50": pct(key_iv_s, 50), "max": _r(max(key_iv_s)) if key_iv_s else None,
                                "basis": key_basis if key_iv_s else None},
        "bitrate_kbps_by_arrival": _r(total_bytes * 8 / span_s / 1000, 1) if span_s > 0 else None,
        "bitrate_kbps_by_pts": _r(total_bytes * 8 / pts_span_ms, 1) if pts_span_ms else None,
        "receive_minus_estimated_capture_ms": {"p50": pct(receive_delay, 50), "p95": pct(receive_delay, 95),
                                               "p99": pct(receive_delay, 99),
                                               "max": _r(max(receive_delay)) if receive_delay else None},
    })
    if not keys:
        warnings.append("키프레임이 오지 않았다")
    if no_pts:
        warnings.append(f"PTS 없는 패킷 {no_pts}개")
    if regressions:
        warnings.append(f"PTS 역행 {len(regressions)}회")
    if pts_gaps:
        warnings.append(f"PTS 빈칸 {len(pts_gaps)}회(약 {missing}프레임)")
    if gaps:
        warnings.append(f"도착 끊김(camera_to_gateway_gap) {len(gaps)}회")
    out["rtcp"] = rtcp_section(obs, tb_ms)
    out.update(timestamp_source(out["rtcp"]))
    out["warnings"] = warnings
    return out


def _empty_metrics() -> dict[str, Any]:
    return {"duration_s": None, "fps_by_arrival": None, "fps_by_pts": None, "nominal_interval_ms": None,
            "nominal_interval_basis": None, "arrival_interval_ms": None, "camera_to_gateway_gap": None, "pts": None,
            "keyframes": None, "packets_before_first_keyframe": None, "keyframe_interval_s": None,
            "bitrate_kbps_by_arrival": None, "bitrate_kbps_by_pts": None, "receive_minus_estimated_capture_ms": None}


def rtcp_section(obs: Sequence[Observation], tb_ms: float | None) -> dict[str, Any]:
    """RTCP Sender Report 분석. SR 이 없으면 present=false 이고 나머지는 null (지어내지 않는다)."""
    sr_count = sum(1 for o in obs if o.sender_report)
    timed = [o for o in obs if o.camera_time_us is not None]
    sec: dict[str, Any] = {
        "present": sr_count > 0 or bool(timed),
        "sender_reports": sr_count,
        "packets_with_camera_time": len(timed),
        "sr_interval_s_p50": None,
        "camera_minus_receive_ms": None,
        "camera_clock_plausible": None,
        "mapping_consistency_ms": None,
    }
    if not timed:
        return sec
    srs = [o for o in obs if o.sender_report]
    sr_iv = [b.received_mono_s - a.received_mono_s for a, b in zip(srs, srs[1:])]
    sec["sr_interval_s_p50"] = pct(sr_iv, 50)
    # 카메라 시각 − 노트북 수신 시각 = 시계 오프셋 − 전송 지연 (+ SR 흔들림)
    diff = [o.camera_time_us / 1000 - o.received_at_ms for o in timed]
    sec["camera_minus_receive_ms"] = {"p5": pct(diff, 5), "p50": pct(diff, 50), "p95": pct(diff, 95),
                                      "spread_p95_minus_p5": _r(pct(diff, 95) - pct(diff, 5), 1)}
    sec["camera_clock_plausible"] = abs(statistics.median(diff)) < PLAUSIBLE_CLOCK_DIFF_MS
    if tb_ms is None:
        return sec
    # SR 하나가 정하는 대응: (카메라 시각 − PTS). 같은 SR 아래 패킷은 같은 값이고, SR 이 바뀔 때 생기는 차이가 흔들림이다.
    # SR 표시가 PTS 없는 패킷에 붙으면 다음 PTS 있는 패킷으로 넘긴다.
    mappings: list[float] = []
    pending = False
    for o in obs:
        pending = pending or o.sender_report
        if o.camera_time_us is None or o.pts is None:
            continue
        if not mappings or pending:
            mappings.append(o.camera_time_us / 1000 - o.pts * tb_ms)
        pending = False
    steps = [b - a for a, b in zip(mappings, mappings[1:])]
    abs_steps = [abs(s) for s in steps]
    sec["mapping_consistency_ms"] = {
        "sender_reports_used": len(mappings),
        "step_p50": pct(abs_steps, 50),
        "step_p95": pct(abs_steps, 95),
        "step_max": _r(max(abs_steps), 1) if abs_steps else None,
        "drift_total": _r(mappings[-1] - mappings[0], 1) if len(mappings) >= 2 else None,
    }
    return sec


def timestamp_source(rtcp: dict[str, Any]) -> dict[str, Any]:
    """촬영 시각 출처 제안. 근거가 모자라면 늘 RECEIVE_ESTIMATED (명세 3.9 기본값)."""
    mc = rtcp.get("mapping_consistency_ms") or {}
    used = mc.get("sender_reports_used") or 0
    if not rtcp["present"]:
        reason = "RTCP Sender Report 가 오지 않았다"
    elif not rtcp["packets_with_camera_time"]:
        reason = "SR 은 왔지만 프레임별 카메라 시각(PRFT)이 없다"
    elif rtcp["camera_clock_plausible"] is not True:
        reason = "카메라 시계가 노트북 시계와 하루 이상 다르다(시계 미설정)"
    elif used < MIN_SENDER_REPORTS:
        reason = f"대응을 판단할 SR 이 {used}개뿐이다(최소 {MIN_SENDER_REPORTS}개)"
    elif mc["step_p95"] is None or mc["step_p95"] > SR_STEP_TOLERANCE_MS:
        reason = (f"연속 SR 의 카메라 시각–RTP 대응이 p95 {mc['step_p95']}ms 흔들린다"
                  f"(허용 {SR_STEP_TOLERANCE_MS}ms)")
    else:
        return {"timestamp_source_recommendation": CAMERA_STREAM_CANDIDATE,
                "timestamp_source_reason": (f"SR {used}개의 카메라 시각–RTP 대응이 p95 {mc['step_p95']}ms 안에서 일관된다. "
                                            "CAMERA_STREAM 전환은 G0 LED 시험으로 확정한다")}
    return {"timestamp_source_recommendation": RECEIVE_ESTIMATED, "timestamp_source_reason": reason}
