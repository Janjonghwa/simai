"""S15P31A101-377: GPU 없는 Perception.step 조사 전용 하네스.

ai/에서 uv run --offline --no-sync python -B -m experiments.profile_perception --help
합성 관측은 SIM 상한이다. 입력 생성·대기·보고서 쓰기는 step 측정에서 제외한다.
제품 코드/보정은 바꾸지 않으며 이미지나 합성 World 상태를 저장하지 않는다.
"""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import pstats
import statistics
import subprocess
import sys
import threading
import time
import traceback

import numpy as np

from services.tier0.perception import Perception
from simai_ai.contact import SurfaceSet
from simai_ai.geometry.distortion import distort
from simai_ai.geometry.rig import CameraRig
from simai_ai.providers.files import FileCalibrationProvider
from simai_ai.tracking.local_tracker import Observation2DFrame, PoseDetection, TrackedDetection

TMP_ROOT = Path("C:/Users/SSAFY/simai-local/codex-tmp/377")
DEFAULT_RECORDING = Path("C:/Users/SSAFY/Desktop/simai/S15P31A101/ai/experiments/recordings/rec-20261029-015512z-lobby-railing-pose")
DEFAULT_CALIBRATION = Path("C:/Users/SSAFY/simai-local/session9/fastrot266/calibration.json")
EPOCH = 1_790_000_000_000
# COCO17: 코, 눈, 귀, 어깨, 팔꿈치, 손목, 골반, 무릎, 발목. 미터/+Z.
POSE = np.array([
    [0, 0, 1.66], [-.035, 0, 1.70], [.035, 0, 1.70], [-.075, 0, 1.67], [.075, 0, 1.67],
    [-.19, 0, 1.43], [.19, 0, 1.43], [-.29, 0, 1.18], [.29, 0, 1.18],
    [-.32, 0, .94], [.32, 0, .94], [-.13, 0, .94], [.13, 0, .94],
    [-.14, 0, .52], [.14, 0, .52], [-.14, 0, .08], [.14, 0, .08],
])


def output_path(path: Path) -> Path:
    """측정/임시 산출물은 사용자 지정 tmp/377 내부만 허용한다."""
    resolved = path.resolve()
    if not resolved.is_relative_to(TMP_ROOT.resolve()):
        raise ValueError(f"출력은 {TMP_ROOT} 아래여야 한다")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def project(camera, xyz):
    camera_xyz = camera.to_camera(xyz)
    normalized = camera_xyz[:, :2] / camera_xyz[:, 2:3]
    pixels = camera.ideal_pixels(distort(normalized, camera.distortion_model, camera.distortion_coeffs))
    w, h = camera.image_size
    visible = (camera_xyz[:, 2] > .1) & (pixels[:, 0] >= 30) & (pixels[:, 0] < w - 30)
    visible &= (pixels[:, 1] >= 30) & (pixels[:, 1] < h - 30)
    return pixels, visible


def origins(rig):
    """4대 모두 전신이 보이고 1m 이상 떨어진 6개 위치를 결정적으로 고른다."""
    candidates = []
    for x in np.arange(1., 9., .5):
        for y in np.arange(1., 9., .5):
            xyz = POSE + [x, y, 0]
            if all(project(cam, xyz)[1].all() for cam in rig.cameras.values()):
                candidates.append((float(x), float(y)))
    if len(candidates) < 6:
        raise RuntimeError("4대 공통 전신 시야에 합성 위치가 부족하다")
    center = np.mean(candidates, axis=0)
    selected = [min(candidates, key=lambda p: np.linalg.norm(np.array(p) - center))]
    while len(selected) < 6:
        point = max(candidates, key=lambda p: min(math.dist(p, s) for s in selected))
        if min(math.dist(point, s) for s in selected) < 1.:
            raise RuntimeError("합성 사람 간격 1m 확보 실패")
        selected.append(point)
    return selected


class SyntheticFeed:
    source = "SIM"

    def __init__(self, rig, people, ticks, seed, noise_px, missing):
        self.camera_ids = tuple(sorted(rig.cameras))
        self.index = 0
        self.frames = []
        self.positions = origins(rig)[:people]
        self.visible_counts = []
        rng = np.random.default_rng(seed)
        for tick in range(ticks):
            batch = []
            for cid in self.camera_ids:
                camera = rig.cameras[cid]
                detections = []
                for person, (x, y) in enumerate(self.positions):
                    phase = tick * .1 + person
                    xyz = POSE + [x + .08 * math.sin(phase), y + .08 * math.cos(phase), 0]
                    pixels, visible = project(camera, xyz)
                    pixels += rng.normal(0, noise_px, pixels.shape)
                    valid = visible & (rng.random(17) >= missing)
                    # 낮은 발목/손목 신뢰도도 포함. bbox는 누락 전 투영 경계다.
                    kps = tuple((float(u), float(v), .9 if j < 7 else .75) if valid[j] else None
                                for j, (u, v) in enumerate(pixels))
                    bbox = (*pixels.min(axis=0).tolist(), *pixels.max(axis=0).tolist())
                    detection = PoseDetection(bbox, .9, kps)
                    detections.append(TrackedDetection(str(person + 1), detection))
                self.visible_counts.append(sum(kp is not None for d in detections for kp in d.detection.keypoints_2d))
                batch.append(Observation2DFrame(cid, 0, tick, EPOCH + tick * 100, "SIM", 0., tuple(detections)))
            self.frames.append(batch)

    def poll(self, now_ms):
        batch = self.frames[self.index]
        self.index += 1
        return batch

    def camera_connected(self, camera_id):
        return camera_id in self.camera_ids

    def provenance(self):
        return {"source": "SIM", "camera_ids": list(self.camera_ids)}

    def close(self):
        pass


class MediaLoad:
    """CPU H.264 네 스트림(각25fps) + JPEG 한 스레드(총25fps).

    각 decoder thread_count=1, BGR 변환은 문맥을 재사용한다. 최신 그림만 유지한다.
    JPEG는 카메라를 순회하며 full resolution quality=85로 인코딩하고 즉시 폐기한다.
    """

    def __init__(self, recording, mode, jpeg_fps):
        self.recording, self.mode, self.jpeg_fps = recording, mode, jpeg_fps
        self.stop = threading.Event()
        self.ready = [threading.Event() for _ in range(4)]
        self.latest = [None] * 4
        self.counts = [0] * 4
        self.jpeg_count = 0
        self.errors = []
        self.threads = []

    def decode(self, i, path):
        try:
            import av
            from av.video.reformatter import VideoReformatter

            deadline = time.perf_counter()
            reformatter = VideoReformatter()
            while not self.stop.is_set():
                with av.open(str(path), format="h264") as container:
                    stream = container.streams.video[0]
                    stream.codec_context.thread_count = 1
                    for frame in container.decode(stream):
                        if self.stop.is_set():
                            return
                        self.latest[i] = reformatter.reformat(frame, format="bgr24", threads=1).to_ndarray()
                        self.counts[i] += 1
                        self.ready[i].set()
                        deadline += .04
                        self.stop.wait(max(0., deadline - time.perf_counter()))
        except Exception:
            self.errors.append(traceback.format_exc())
            self.ready[i].set()

    def jpeg(self):
        try:
            import cv2

            deadline = time.perf_counter()
            while not self.stop.is_set():
                frame = self.latest[self.jpeg_count % 4]
                ok, _data = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if not ok:
                    raise RuntimeError("JPEG 인코딩 실패")
                self.jpeg_count += 1
                deadline += 1. / self.jpeg_fps
                self.stop.wait(max(0., deadline - time.perf_counter()))
        except Exception:
            self.errors.append(traceback.format_exc())

    def __enter__(self):
        if self.mode == "base":
            return self
        paths = sorted(self.recording.glob("cam_*/epoch_0000.h264"))
        if len(paths) != 4:
            raise ValueError("H.264 카메라 4대 필요")
        if self.mode in ("decode", "media"):
            for i, path in enumerate(paths):
                thread = threading.Thread(target=self.decode, args=(i, path), name=f"profile-decode-{i}")
                thread.start()
                self.threads.append(thread)
            for event in self.ready:
                if not event.wait(15):
                    self.__exit__(None, None, None)
                    raise RuntimeError("디코딩 준비 시간 초과")
            if self.errors:
                self.__exit__(None, None, None)
                raise RuntimeError(self.errors)
        else:
            # JPEG 단독 대조군: 실제 녹화의 첫 그림 4개만 측정 전에 디코딩한다.
            import av

            for i, path in enumerate(paths):
                with av.open(str(path), format="h264") as container:
                    container.streams.video[0].codec_context.thread_count = 1
                    self.latest[i] = next(container.decode(video=0)).to_ndarray(format="bgr24")
        if self.mode in ("jpeg", "media"):
            thread = threading.Thread(target=self.jpeg, name="profile-jpeg")
            thread.start()
            self.threads.append(thread)
        return self

    def __exit__(self, *_args):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=15)
        if any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("부하 스레드 종료 실패")


def profile_table(profile):
    rows = []
    for (filename, line, function), (primitive, calls, own, cumulative, _callers) in pstats.Stats(profile).stats.items():
        rows.append({"function": f"{filename}:{line}:{function}", "calls": calls,
                     "self_ms": own * 1000, "cumulative_ms": cumulative * 1000})
    return {"self": sorted(rows, key=lambda r: r["self_ms"], reverse=True)[:25],
            "cumulative": sorted(rows, key=lambda r: r["cumulative_ms"], reverse=True)[:25]}


def benchmark(args):
    if args.switch_interval_ms is not None:
        sys.setswitchinterval(args.switch_interval_ms / 1000.)
    provider = FileCalibrationProvider(args.calibration)
    rig = CameraRig.from_provider(provider)
    total = args.warmup + args.ticks
    feed = SyntheticFeed(rig, args.people, total, args.seed, args.noise_px, args.missing)
    provenance = {"frames": feed.provenance(), "calibration": provider.provenance(),
                  "tracks": {"source": "SIM", "provider_version": "perception-profile-377"},
                  "is_upper_bound": True}
    perception = Perception(rig, feed, session="profile377", scene_version="profile377", input_provenance=provenance)
    surfaces = SurfaceSet.floor_only()
    timings, cpu_timings, tracks = [], [], []
    profile = cProfile.Profile() if args.cprofile else None
    spy = None
    with MediaLoad(args.recording, args.mode, args.jpeg_fps) as load:
        # py-spy는 이 하네스 시작부터 포함하므로 warmup/입력 생성은 스택으로 구분한다.
        print(json.dumps({"ready_pid": os.getpid(), "people": args.people, "mode": args.mode}), flush=True)
        run_start = time.perf_counter()
        for tick in range(total):
            if args.spy_output and tick == args.warmup:
                command = [str(Path(sys.executable).parent / "py-spy.exe"), "record", "--native", "--gil",
                           "--pid", str(os.getpid()), "--duration", str(max(1, int(args.ticks * .1) - 1)),
                           "--rate", str(args.spy_rate), "--format", "raw", "--output", str(output_path(args.spy_output))]
                spy = subprocess.Popen(command)
            if profile and tick >= args.warmup:
                profile.enable()
            cpu_start = time.thread_time_ns()
            start = time.perf_counter_ns()
            perceived = perception.step(EPOCH + tick * 100, surfaces)
            elapsed = (time.perf_counter_ns() - start) / 1e6
            cpu_elapsed = (time.thread_time_ns() - cpu_start) / 1e6
            if profile:
                profile.disable()
            if perceived is None:
                raise AssertionError("step이 새 World 결과를 내지 않았다")
            if tick >= args.warmup:
                timings.append(elapsed)
                cpu_timings.append(cpu_elapsed)
                tracks.append(len(perceived.frame.tracks))
            if args.pace:
                time.sleep(max(0., run_start + (tick + 1) * .1 - time.perf_counter()))
        if profile:
            profile.disable()
        duration = time.perf_counter() - run_start
    if load.errors:
        raise RuntimeError(load.errors)
    if spy is not None and spy.wait(timeout=30) != 0:
        raise RuntimeError("py-spy record 실패")
    if not tracks or min(tracks) < args.people:
        raise AssertionError(f"요청 인원보다 적은 출력 트랙: {tracks}")
    versions = {name: importlib.metadata.version(name) for name in ("numpy", "av", "opencv-python-headless")}
    result = {"people": args.people, "mode": args.mode, "ticks": args.ticks, "warmup": args.warmup,
              "seed": args.seed, "noise_px": args.noise_px, "missing": args.missing, "pace": args.pace,
              "median_ms": statistics.median(timings), "p95_ms": float(np.percentile(timings, 95)),
              "thread_cpu_median_ms": statistics.median(cpu_timings), "max_ms": max(timings),
              "over_100ms": sum(t > 100 for t in timings), "over_200ms": sum(t > 200 for t in timings),
              "tracks_min": min(tracks), "tracks_max": max(tracks), "step_ms": timings,
              "positions_m": feed.positions, "visible_joints_per_camera_median": statistics.median(feed.visible_counts),
              "media_duration_s": duration, "decoded": load.counts, "jpeg_encoded": load.jpeg_count,
              "jpeg_target_fps": args.jpeg_fps, "python": platform.python_version(), "platform": platform.platform(),
              "switch_interval_ms": sys.getswitchinterval() * 1000.,
              "spy_rate": args.spy_rate if args.spy_output else None,
              "cpu": platform.processor(), "logical_cpu_count": os.cpu_count(), "versions": versions,
              "calibration_sha256": hashlib.sha256(args.calibration.read_bytes()).hexdigest(),
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "input_provenance": provenance, "started_at": int(time.time() * 1000 - duration * 1000)}
    if profile:
        path = output_path(args.cprofile)
        profile.dump_stats(path)
        result["cprofile"] = profile_table(profile)
    output_path(args.output).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("people", "mode", "median_ms", "p95_ms", "thread_cpu_median_ms", "tracks_min", "tracks_max", "decoded", "jpeg_encoded")}), flush=True)
    if args.fail_above_ms and result["p95_ms"] > args.fail_above_ms:
        return 2  # 알려진 지연 증상을 잡는 재현 판정. 조사 결과 저장 후 실패한다.
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--recording", type=Path, default=DEFAULT_RECORDING)
    parser.add_argument("--people", type=int, choices=range(1, 7), default=5)
    parser.add_argument("--ticks", type=int, default=120)
    parser.add_argument("--warmup", type=int, default=12)
    parser.add_argument("--seed", type=int, default=377)
    parser.add_argument("--noise-px", type=float, default=2.)
    parser.add_argument("--missing", type=float, default=.05)
    parser.add_argument("--mode", choices=("base", "decode", "jpeg", "media"), default="base")
    parser.add_argument("--jpeg-fps", type=float, default=25.)
    parser.add_argument("--pace", action="store_true", help="측정 밖에서 10Hz 주기 대기")
    parser.add_argument("--cprofile", type=Path)
    parser.add_argument("--spy-output", type=Path, help="자신의 실제 PID에 py-spy record --native --gil 부착")
    parser.add_argument("--spy-rate", type=int, default=20, help="native 수집 오버헤드를 낮추기 위한 표본/s")
    parser.add_argument("--switch-interval-ms", type=float, help="GIL 인과 대조군 전용; 제품 설정 변경 아님")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fail-above-ms", type=float, default=0.)
    args = parser.parse_args()
    if args.ticks < 1 or args.warmup < 1 or not 0 <= args.missing < 1 or args.noise_px < 0 or args.jpeg_fps <= 0:
        parser.error("ticks/warmup/jpeg-fps는 양수, missing은 [0,1), noise-px는 0 이상")
    if args.spy_output and not args.pace:
        parser.error("py-spy는 --pace와 함께 사용한다")
    if args.spy_rate <= 0:
        parser.error("spy-rate는 양수여야 한다")
    if args.switch_interval_ms is not None and args.switch_interval_ms <= 0:
        parser.error("switch-interval-ms는 양수여야 한다")
    return benchmark(args)


if __name__ == "__main__":
    raise SystemExit(main())
