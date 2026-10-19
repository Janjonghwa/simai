"""실행 도구(`python -m simai_ai.calibration.intrinsics.cli`)를 별도 프로세스로 끝까지 돌린다.

- 입력 영상은 시험 안에서 합성 교실(SIM, 직선만) 위로 어두운 사각형(지나가는 사람 대신)이 움직이는 H.264 파일로 만든다.
  실제 카메라·사람 영상은 쓰지 않는다. 결과 지표는 상한이다(is_upper_bound).
- 학습 모델(torch·anycalib·geocalib)과 가중치는 없으므로 기록에 사용 불가와 고전 기준 대체가 남아야 한다.
- 도구가 이미지·영상을 디스크에 쓰지 않는지: 임시 폴더(TMP·TEMP·TMPDIR)를 빈 폴더로 돌려 실행 뒤에도 비어 있는지,
  작업 폴더·출력 폴더에 기록 JSON 말고 새 파일이 없는지 본다.
- RTSP 계정이 출력·기록에 없는지: 닫힌 로컬 포트로 접속을 시도해(카메라 없음) 실패시키고 확인한다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

USER, PASSWORD = "simai-cli-op", "Zq9:pa55@word"


def bgr(gray):
    g = np.clip(np.asarray(gray, dtype=float) * 255.0, 0, 255).astype(np.uint8)
    return np.repeat(g[:, :, None], 3, axis=2)


def write_video(path: Path, images, fps: int = 5) -> None:
    import av

    h, w = images[0].shape[:2]
    with av.open(str(path), "w") as out:
        stream = out.add_stream("libx264", rate=fps)
        stream.width, stream.height, stream.pix_fmt = w, h, "yuv420p"
        stream.options = {"qp": "0", "preset": "ultrafast"}
        for img in images:
            for packet in stream.encode(av.VideoFrame.from_ndarray(img, format="bgr24")):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)


def passing_people(base, n):
    out = []
    h, w = base.shape[:2]
    for k in range(n):
        img = base.copy()
        x0, y0 = (k * 131) % (w - 90), (k * 71) % (h - 180)
        img[y0 : y0 + 180, x0 : x0 + 90] = 25
        out.append(img)
    return out


def listing(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def run_cli(args, cwd: Path, tmpdir: Path, extra_env=None):
    env = dict(os.environ, PYTHONUTF8="1", TMP=str(tmpdir), TEMP=str(tmpdir), TMPDIR=str(tmpdir))
    for key in [k for k in env if k.startswith(("RTSP_", "SIMAI_ANYCALIB", "SIMAI_GEOCALIB"))]:
        del env[key]
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "simai_ai.calibration.intrinsics.cli", *args],
        check=False,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )


def test_cli_files_end_to_end_without_learned_backends(tmp_path, wide_case):
    work = tmp_path / "work"
    tmpdir = tmp_path / "tmpdir"
    outdir = tmp_path / "out"
    for d in (work, tmpdir, outdir):
        d.mkdir()
    for cid in ("cam1", "cam2"):
        write_video(work / f"{cid}.mp4", passing_people(bgr(wide_case.image(cid)), 50))  # 10 초
    write_video(work / "cam3.mp4", [bgr(wide_case.image("cam1"))] * 2)  # 프레임 부족
    (work / "cam4.mp4").write_bytes(b"\x00\x01not a video" * 100)  # 디코딩 불가
    before = listing(work)
    out = outdir / "rec.json"
    proc = run_cli(
        [*(f"--file={c}={c}.mp4" for c in ("cam1", "cam2", "cam3", "cam4")),
         "--frames-source", "SIM", "--duration", "8", "--interval", "1", "--min-frames", "5", "--out", str(out)],
        work,
        tmpdir,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # 디스크: 임시 폴더는 비어 있고, 작업·출력 폴더에는 기록 JSON 말고 새 파일이 없다
    assert listing(tmpdir) == set()
    assert listing(work) == before
    assert listing(outdir) == {"rec.json"}

    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["format"] == "simai.intrinsics_estimate" and rec["format_version"] == "0.2.0"
    assert rec["input_provenance"]["frames"]["source"] == "SIM" and rec["input_provenance"]["is_upper_bound"] is True
    by_id = {c["camera_id"]: c for c in rec["cameras"]}
    for cid in ("cam1", "cam2"):
        cam = by_id[cid]
        assert cam["status"] == "OK" and cam["method"] == "PLUMB_LINE_VP", cam
        assert cam["capture"]["frames_used"] == 8 and cam["capture"]["sample_span_s"] == [0.0, 7.0]
        assert cam["source"] == {"kind": "FILE", "file": f"{cid}.mp4"}
        backend = cam["backend"]
        assert backend["order"] == ["anycalib", "geocalib", "plumbline"] and backend["fallback"] is True
        assert [a["outcome"] for a in backend["attempts"]] == ["UNAVAILABLE", "UNAVAILABLE", "OK"]
        assert "가중치" in backend["attempts"][0]["reason"]
    est = {e["camera_id"]: e for e in rec["estimates"]}
    for cid in ("cam1", "cam2"):
        truth = wide_case.cameras[cid]["K"][0][0]
        assert abs(est[cid]["camera"]["K"][0][0] - truth) / truth < 0.01
        assert est[cid]["method"] == "PLUMB_LINE_VP" and "model" not in est[cid]
    assert by_id["cam3"]["status"] == "FAILED" and "최소 5장" in " ".join(by_id["cam3"]["reasons"])
    assert by_id["cam4"]["status"] == "FAILED" and by_id["cam4"]["capture"]["frames_used"] == 0
    assert set(est) == {"cam1", "cam2"}
    assert rec["shared_lens"]["common"]["status"] == "OK"
    assert rec["run"]["images_stored"] is False
    assert set(rec["run"]["unavailable_backends"]) == {"anycalib", "geocalib"}
    assert "기본 백엔드 anycalib 대신 plumbline 사용" in proc.stdout


def test_cli_rtsp_failure_never_shows_credentials(tmp_path):
    """닫힌 로컬 포트(카메라 없음)로 RTSP 를 열어 실패시킨다. 계정은 .env 에서 읽고, 출력·기록 어디에도 남지 않는다."""
    work = tmp_path / "work"
    tmpdir = tmp_path / "tmpdir"
    work.mkdir()
    tmpdir.mkdir()
    (work / ".env").write_text(f"RTSP_USER_CAM_X={USER}\nRTSP_PASS_CAM_X={PASSWORD}\n", encoding="utf-8")
    (work / "classroom.toml").write_text(
        '[session]\nscenario = "t"\n\n[[cameras]]\ncamera_id = "cam_X"\nkind = "rtsp"\nhost = "127.0.0.1"\nport = 1\n'
        'path = "stream1"\n\n[[cameras]]\ncamera_id = "cam_Y"\nkind = "rtsp"\nhost = "127.0.0.1"\nport = 1\n',
        encoding="utf-8",
    )
    out = work / "rec.json"
    proc = run_cli(
        ["--session", "classroom.toml", "--env-file", ".env", "--duration", "2", "--interval", "1",
         "--open-timeout", "3", "--read-timeout", "3", "--out", str(out)],
        work,
        tmpdir,
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr
    text = proc.stdout + proc.stderr + out.read_text(encoding="utf-8")
    for secret in (USER, PASSWORD, "pa55", "Zq9"):
        assert secret not in text
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["input_provenance"]["frames"]["source"] == "LIVE"
    by_id = {c["camera_id"]: c for c in rec["cameras"]}
    assert by_id["cam_X"]["status"] == "FAILED" and by_id["cam_X"]["reasons"][0].startswith("입력을 열지 못했다(")
    assert by_id["cam_X"]["source"] == {"kind": "RTSP", "host": "127.0.0.1", "port": 1, "path": "stream1"}
    assert by_id["cam_Y"]["status"] == "FAILED" and "RTSP_USER_CAM_Y" in by_id["cam_Y"]["reasons"][0]
    assert rec["estimates"] == [] and "shared_lens" not in rec
    assert listing(tmpdir) == set()


def test_cli_config_errors(tmp_path):
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    mixed = run_cli(["--rtsp", "a=10.0.0.1", "--file", "b=b.mp4"], tmp_path, tmpdir)
    assert mixed.returncode == 2 and "섞지 않는다" in mixed.stderr
    leaked = run_cli(["--rtsp", f"{USER}:{PASSWORD}@10.0.0.1"], tmp_path, tmpdir)
    assert leaked.returncode == 2 and PASSWORD not in leaked.stderr + leaked.stdout and USER not in leaked.stderr
    bad_backend = run_cli(["--file", "a=a.mp4", "--backends", "anycalib,magic"], tmp_path, tmpdir)
    assert bad_backend.returncode == 2
    half_weights = run_cli(["--file", "a=a.mp4", "--anycalib-weights", "x.pt"], tmp_path, tmpdir)
    assert half_weights.returncode == 2 and "sha256" in half_weights.stderr


def test_cli_session_host_with_credentials_is_refused(tmp_path):
    """반례(리뷰 F3): 세션 TOML host 에 계정이 들어 있으면 실행하지 않고(종료 2), 출력·파일 어디에도 값이 남지 않는다."""
    work = tmp_path / "work"
    tmpdir = tmp_path / "tmpdir"
    work.mkdir()
    tmpdir.mkdir()
    (work / "s.toml").write_text(
        f'[[cameras]]\ncamera_id = "cam_X"\nkind = "rtsp"\nhost = "{USER}:{PASSWORD}@127.0.0.1"\nport = 1\n',
        encoding="utf-8",
    )
    before = listing(work)
    proc = run_cli(["--session", "s.toml", "--out", str(work / "rec.json")], work, tmpdir)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    for secret in (USER, PASSWORD, "pa55"):
        assert secret not in proc.stdout + proc.stderr
    assert listing(work) == before and listing(tmpdir) == set()
    via_arg = run_cli(["--rtsp", f"cam_X={USER}:{PASSWORD}@127.0.0.1"], work, tmpdir)
    assert via_arg.returncode == 2 and PASSWORD not in via_arg.stdout + via_arg.stderr
