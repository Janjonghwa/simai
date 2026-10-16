# EXP-02 E-GATE-0 (S15P31A101-86): 라이브 추론 FPS 확인용. RunPod GPU 에서 실행한다.
"""노트북(live_client.py)이 WebSocket 으로 보낸 프레임 → YOLO26x-Pose + Fast SAM 3D Body → 오버레이 JPEG 를 돌려준다.

  (a) RunPod HTTPS 프록시(443) — SSH·UDP 가 막힌 망(SSAFY)용. 프록시로 노출된 HTTP 포트에서 토큰 인증으로 연다.
      RunPod : envs/fast_sam3db/bin/python setup/hmr/live_server.py --host 0.0.0.0 --port 8888
      노트북 : .env 에 LIVE_URL=wss://<POD_ID>-8888.proxy.runpod.net/ , LIVE_TOKEN=... 후
               uv run --with opencv-python --with numpy --with websockets==14.1 ai/experiments/hmr/live_client.py --source 0
  (b) SSH 터널 — 기본 --host 127.0.0.1 --port 8765, 토큰 불필요
      노트북 : ssh -N -L 8765:localhost:8765 -L 8766:localhost:8766 simai-runpod 후 live_client.py --url ws://127.0.0.1:8765/
      브라우저: http://localhost:8766 (터널일 때만)
  토큰: 환경변수 LIVE_TOKEN 또는 /workspace/.simai-private/live.env. 127.0.0.1 이 아닌 주소로 열 때는 필수.

- 최신 프레임만 처리한다(처리 중 들어온 이전 프레임은 버리고 dropped 로 센다·응답한다). 실시간 관제와 같은 방식.
- K 는 첫 프레임에 MoGe2 로 한 번 추정해 고정한다(고정 카메라 가정). --fov-every-frame 이면 매 프레임 추정.
- 프레임은 메모리에만 두고 디스크에 저장하지 않는다. 종료 시 지연 통계만 runs/exp02/<run_id>/live_stats.json 에 남긴다.
"""
import argparse
import contextlib
import io
import json
import os
import struct
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--preset", default="opt"); ap.add_argument("--env", nargs="*", default=[])
ap.add_argument("--variant", default="dinov3")
ap.add_argument("--inference-type", default="full", choices=["full", "body"])
ap.add_argument("--bbox-thr", type=float, default=0.5)
ap.add_argument("--fov-every-frame", action="store_true")
ap.add_argument("--warmup", type=int, default=3, help="시작 시 1명·다인원 사진으로 미리 돌리는 횟수 (첫 프레임·인원 변화·compile 지연 제거)")
ap.add_argument("--verbose", action="store_true", help="Fast 저장소의 프레임별 디버그 출력을 그대로 둔다 (기본: 추론 중 출력 버림)")
ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8765)
ap.add_argument("--http-port", type=int, default=8766)
ap.add_argument("--run-id", default=None, help="지정하면 종료 시 통계를 runs/exp02/<run_id>/live_stats.json 에 기록")
a = ap.parse_args()

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fs3db_common as C  # noqa: E402
env = C.apply_preset(a.preset, a.env)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402

# MHR70 순서 (sam_3d_body/metadata/mhr70.py): 5/6 어깨, 7/8 팔꿈치, 62/41 손목(좌/우), 9/10 엉덩이, 11/12 무릎, 13/14 발목
BONES = [(5, 7), (7, 62), (6, 8), (8, 41), (5, 6), (5, 9), (6, 10), (9, 10), (9, 11), (11, 13), (10, 12), (12, 14)]


SEND_LOCK = threading.Lock()   # 추론 스레드(결과)와 수신 스레드(버림 응답)가 같은 연결에 쓴다


def pack(meta, payload):
    """WebSocket 바이너리 메시지 1개 = [4B meta 길이][meta JSON][JPEG]."""
    j = json.dumps(meta).encode()
    return struct.pack("!I", len(j)) + j + payload


def unpack(msg):
    (n,) = struct.unpack("!I", msg[:4])
    return json.loads(msg[4:4 + n]), msg[4 + n:]


def send_msg(ws, meta, payload):
    with SEND_LOCK:
        ws.send(pack(meta, payload))


def load_token():
    tok = os.environ.get("LIVE_TOKEN")
    f = Path("/workspace/.simai-private/live.env")
    if not tok and f.exists():
        for line in f.read_text().splitlines():
            if line.startswith("LIVE_TOKEN="):
                tok = line.split("=", 1)[1].strip()
    return tok


class State:
    def __init__(self):
        self.cv = threading.Condition()
        self.latest = None          # (meta, jpeg bytes, recv_time)
        self.dropped = 0; self.received = 0
        self.out_jpeg = None; self.out_seq = 0
        self.done = deque(maxlen=60)  # 최근 처리 완료 시각 (추론 FPS)
        self.rows = []               # 종료 시 통계용
        self.client_e2e = None
        self.meta_npz = b""


S = State()


def draw_mesh(vis, V, faces, K, alpha=0.55):
    """메시 삼각형을 반투명하게 칠한다. 카메라를 향한 면만 그리고(뒷면 제거) 면 방향으로 밝기를 4단계로 나눈다.
    깊이 정렬 없이 cv2.fillPoly 로 한 번에 칠하므로 빠르다(대략적인 확인용, 정밀 렌더는 pyrender)."""
    z = np.clip(V[:, 2], 1e-3, None)
    uv = np.stack([K[0, 0] * V[:, 0] / z + K[0, 2], K[1, 1] * V[:, 1] / z + K[1, 2]], 1)
    tri3 = V[faces]; n = np.cross(tri3[:, 1] - tri3[:, 0], tri3[:, 2] - tri3[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-9
    c = np.einsum("ij,ij->i", n, -tri3.mean(1) / (np.linalg.norm(tri3.mean(1), axis=1, keepdims=True) + 1e-9))  # 시선과 면 방향의 cos
    front = c > 0
    layer = vis.copy(); tri = np.round(uv[faces]).astype(np.int32)
    for lo, hi, col in ((0.0, 0.35, (90, 60, 30)), (0.35, 0.6, (160, 110, 60)), (0.6, 0.8, (210, 160, 100)), (0.8, 1.01, (245, 210, 160))):
        sel = front & (c >= lo) & (c < hi)
        if sel.any():
            cv2.fillPoly(layer, list(tri[sel]), col, lineType=cv2.LINE_8)
    return cv2.addWeighted(layer, alpha, vis, 1 - alpha, 0)


def overlay(img, out, K, st, info, faces=None):
    vis = img.copy()
    for p in out:
        if faces is not None:
            vis = draw_mesh(vis, p["pred_vertices"] + p["pred_cam_t"][None], faces, K)
        k2 = p["pred_keypoints_2d"]
        for i, j in BONES:
            cv2.line(vis, tuple(map(int, k2[i, :2])), tuple(map(int, k2[j, :2])), (0, 255, 0), 2)
    lines = [f"infer {info['fps']:.1f} fps | total {st['process_one_image_ms']:.0f} ms (det {st.get('detect_ms', 0):.0f} / hmr {st.get('hmr_ms', 0):.0f}"
             f" / fov {st.get('fov_ms', 0):.0f})",
             f"people {st['n_people']} | dropped {S.dropped}/{S.received} | gpu peak {st['peak_alloc_mb']:.0f} MB | preset {a.preset}",
             f"e2e (client) {S.client_e2e:.0f} ms" if S.client_e2e else "e2e (client) -"]
    for i, t in enumerate(lines):
        y = 28 + 26 * i
        cv2.putText(vis, t, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(vis, t, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 1, cv2.LINE_AA)
    return vis


def npz(d):
    b = io.BytesIO(); np.savez(b, **d)
    return b.getvalue()


def links():
    """MHR70 골격 연결과 색 (공식 메타데이터 pose_info). [i, j, r, g, b]"""
    from sam_3d_body.metadata.mhr70 import mhr_names, pose_info
    out = []
    for v in pose_info["skeleton_info"].values():
        i, j = (n.replace("_", "-") for n in v["link"])
        if i in mhr_names and j in mhr_names:
            out.append([mhr_names.index(i), mhr_names.index(j), *v["color"]])
    return np.array(out, np.int32)


def result_arrays(out, K, st):
    """클라이언트 표시용 결과. verts 는 카메라 좌표(미터, cam_t 더함) float16 — 1명 약 110 KB."""
    P = len(out)
    return {"kp2d": np.stack([p["pred_keypoints_2d"][:, :2] for p in out]).astype(np.float32) if P else np.zeros((0, 70, 2), np.float32),
            "verts": np.stack([p["pred_vertices"] + p["pred_cam_t"][None] for p in out]).astype(np.float16) if P else np.zeros((0, 0, 3), np.float16),
            "bbox": np.stack([np.asarray(p.get("bbox", np.zeros(4)), np.float32).reshape(4) for p in out]) if P else np.zeros((0, 4), np.float32),
            "K": np.asarray(K, np.float32),
            "timing": np.array([st.get("detect_ms") or 0, st.get("hmr_ms") or 0, st["process_one_image_ms"], st["peak_alloc_mb"]], np.float32)}


def quiet(fn):
    """Fast 저장소는 프레임마다 단계별 print 를 약 100줄 낸다. 끄는 설정이 없어 추론 중 stdout 을 버린다(코드 수정 없음)."""
    def w(*args, **kw):
        with open(os.devnull, "w", encoding="utf-8") as dn, contextlib.redirect_stdout(dn):
            return fn(*args, **kw)
    return w


def infer_loop(conn_holder):
    print("[live] loading models ...", flush=True)
    est, comps, timed, info = C.build(a.variant, detector="yolo_pose", detector_model=str(C.YOLO), fov="moge2")
    run = C.run_one if a.verbose else quiet(C.run_one)
    if a.warmup:
        t0 = time.perf_counter()
        for stem in ("t1_front_taichi", "t3_multi_f050"):   # 1명, 5명 → 인원별 첫 실행 비용을 미리 치른다
            wimg = cv2.imread(str(C.INPUTS / f"{stem}.jpg"))
            for itype in ("full", "body"):   # 클라이언트가 m 키로 모드를 바꿔도 첫 프레임이 느리지 않게 둘 다
                for _ in range(a.warmup):
                    run(est, timed, wimg, inference_type=itype, bbox_thr=a.bbox_thr, hand_box_source="yolo_pose")
        print(f"[live] warmup {time.perf_counter() - t0:.1f} s", flush=True)
    S.meta_npz = npz({"faces": est.faces.astype(np.int32), "links": links()})
    print("[live] ready", flush=True)
    K_fixed = None
    while True:
        with S.cv:
            while S.latest is None:
                S.cv.wait(timeout=0.5)   # 타임아웃이 없으면 SIGINT·SIGTERM 이 대기 중에 처리되지 않는다
            meta, jpg, t_recv = S.latest; S.latest = None
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        H, W = img.shape[:2]
        itype = meta.get("mode") or a.inference_type
        try:
            if K_fixed is None or a.fov_every_frame:
                out, st = run(est, timed, img, inference_type=itype, bbox_thr=a.bbox_thr, hand_box_source="yolo_pose")
                if out:
                    f = float(np.asarray(out[0]["focal_length"]).reshape(-1)[0])
                    K_fixed = np.array([[f, 0, W / 2], [0, f, H / 2], [0, 0, 1]], np.float32)
                    print(f"[live] K fixed: f={f:.1f}px ({W}x{H})", flush=True)
                K = K_fixed if K_fixed is not None else np.array([[W, 0, W / 2], [0, W, H / 2], [0, 0, 1]], np.float32)
            else:
                K = K_fixed
                out, st = run(est, timed, img, cam_int=K, inference_type=itype, bbox_thr=a.bbox_thr, hand_box_source="yolo_pose")
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache(); print("[live] OOM, frame skipped", flush=True); continue
        now = time.perf_counter()
        S.done.append(now)
        fps = (len(S.done) - 1) / (S.done[-1] - S.done[0]) if len(S.done) > 1 else 0.0
        reply = meta.get("reply", "jpeg")
        if reply == "data":   # 결과 값만 보낸다. 그리기·3D 창은 클라이언트가 한다 (선행 PoC live/client_win.py 방식)
            payload = npz(result_arrays(out, K, st))
        else:
            t_draw = time.perf_counter(); vis = overlay(img, out, K, st, {"fps": fps}, est.faces); st["draw_ms"] = round((time.perf_counter() - t_draw) * 1000, 1)
            payload = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
            with S.cv:
                S.out_jpeg = payload; S.out_seq += 1; S.cv.notify_all()
        server_ms = (time.perf_counter() - t_recv) * 1000
        row = {"frame_id": meta.get("frame_id"), "n_people": st["n_people"], "process_one_image_ms": st["process_one_image_ms"],
               "detect_ms": st.get("detect_ms"), "hmr_ms": st.get("hmr_ms"), "fov_ms": st.get("fov_ms"),
               "server_ms": round(server_ms, 1), "draw_ms": st.get("draw_ms"), "mode": itype, "infer_fps": round(fps, 2), "peak_alloc_mb": st["peak_alloc_mb"]}
        S.rows.append(row)
        if len(S.rows) <= 5 or len(S.rows) % 10 == 0:
            print(f"[live] #{len(S.rows)} {row}", flush=True)
        if len(S.rows) % 50 == 0:
            write_stats()   # 강제 종료돼도 통계가 남게 주기적으로 저장
        c = conn_holder.get("conn")
        if c is not None:
            try:
                send_msg(c, {**row, "t_capture_ms": meta.get("t_capture_ms"), "size": f"{W}x{H}", "reply": reply}, payload)
            except Exception:  # noqa: BLE001 — 연결이 막 끊긴 경우
                pass


def serve_frames(conn_holder):
    import hmac
    from websockets.sync.server import serve
    token = load_token()
    if a.host != "127.0.0.1" and not token:
        raise SystemExit("외부 주소로 열려면 LIVE_TOKEN 이 필요하다")

    def check(conn, req):   # 프록시 주소는 누구나 접근할 수 있으므로 토큰이 맞지 않으면 핸드셰이크 전에 거절
        if token and not hmac.compare_digest(req.headers.get("Authorization", ""), f"Bearer {token}"):
            return conn.respond(401, "unauthorized")
        return None

    def handler(ws):
        if conn_holder.get("conn") is not None:
            ws.close(1013, "busy"); return
        conn_holder["conn"] = ws; S.done.clear(); print("[live] client connected", flush=True)   # 추론 fps 는 연결마다 새로 잰다
        while not S.meta_npz:   # 모델 로드·워밍업이 끝나야 faces 를 보낼 수 있다
            time.sleep(0.2)
        send_msg(ws, {"type": "meta"}, S.meta_npz)   # 메시 면(faces)·골격 연결(links)은 연결당 한 번만 보낸다
        try:
            for msg in ws:
                meta, jpg = unpack(msg)
                if meta.get("last_e2e_ms") is not None:
                    S.client_e2e = meta["last_e2e_ms"]
                with S.cv:
                    S.received += 1
                    old = S.latest
                    if old is not None:
                        S.dropped += 1
                    S.latest = (meta, jpg, time.perf_counter()); S.cv.notify_all()
                if old is not None:   # 버린 프레임도 응답해 클라이언트가 대기 중 프레임 수를 정확히 알게 한다
                    send_msg(ws, {"frame_id": old[0].get("frame_id"), "dropped": True}, b"")
        except Exception as e:  # noqa: BLE001
            print(f"[live] connection error {type(e).__name__}", flush=True)
        finally:
            conn_holder["conn"] = None; print("[live] client disconnected", flush=True)

    with serve(handler, a.host, a.port, process_request=check, max_size=None, compression=None) as srv:
        print(f"[live] websocket on {a.host}:{a.port} (token {'on' if token else 'off'})", flush=True)
        srv.serve_forever()


PAGE = b"""<!doctype html><meta charset=utf-8><title>simai live HMR</title>
<body style="margin:0;background:#111"><img src="/stream" style="max-width:100vw;max-height:100vh;display:block;margin:auto"></body>"""


class Http(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path != "/stream":
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(PAGE); return
        self.send_response(200); self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f"); self.end_headers()
        seq = -1
        try:
            while True:
                with S.cv:
                    while S.out_seq == seq:
                        S.cv.wait(timeout=5)
                    seq, jpg = S.out_seq, S.out_jpeg
                if jpg is None:
                    continue
                self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


def write_stats():
    if not a.run_id or not S.rows:
        return
    rows = S.rows[5:] or S.rows  # 처음 몇 프레임(로드 직후·K 추정)은 제외
    res = {"run_id": a.run_id, "preset": a.preset, "inference_type": a.inference_type, "warmup": a.warmup, "preset_env": env, "env": C.env_summary(), "frames": len(S.rows),
           "received": S.received, "dropped": S.dropped,
           "process_one_image_ms": C.pct([r["process_one_image_ms"] for r in rows]),
           "server_ms": C.pct([r["server_ms"] for r in rows]),
           "infer_fps_mean": round(float(np.mean([r["infer_fps"] for r in rows])), 2), "rows": S.rows}
    C.write_json(C.OUT / a.run_id / "live_stats.json", res)
    print(f"[live] stats → {C.OUT / a.run_id}/live_stats.json", flush=True)


def main():
    import signal
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    holder = {"conn": None}
    threading.Thread(target=serve_frames, args=(holder,), daemon=True).start()
    hs = ThreadingHTTPServer(("127.0.0.1", a.http_port), Http)
    threading.Thread(target=hs.serve_forever, daemon=True).start()
    print(f"[live] browser on 127.0.0.1:{a.http_port}", flush=True)
    try:
        infer_loop(holder)
    except KeyboardInterrupt:
        pass
    finally:
        write_stats()


if __name__ == "__main__":
    main()
