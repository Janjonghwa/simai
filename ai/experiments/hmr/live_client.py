# EXP-02 E-GATE-0 (S15P31A101-86): 라이브 추론 확인용. 노트북에서 실행한다 (서버: live_server.py).
"""웹캠·C110 RTSP 프레임을 WebSocket 으로 RunPod live_server 에 보내고, 결과 값(2D 관절·메시)을 받아
웹캠 창(실시간 영상 + 마지막 결과 오버레이)과 Open3D 3D 메시 창에 띄운다. 선행 PoC human_sam3d_g3_lite/live/client_win.py 와 같은 화면.

  uv run --with opencv-python --with numpy --with websockets==14.1 --with open3d ai/experiments/hmr/live_client.py --source 0 --url ws://127.0.0.1:8765/
  ... --source rtsp                         # .env 의 C110 (stream1)
  ... --source rtsp --rtsp-path stream2     # C110 720p
  ... (--url 생략)                           # .env 의 LIVE_URL·LIVE_TOKEN (RunPod HTTPS 프록시 443)
  ... --no-3d                               # Open3D 창 없이

키 (웹캠 창):  q 종료 · m 손 포함(full)/몸만(body) 전환 · v 메시 정점 오버레이 · s 스냅샷(jpg + ply, ai/experiments/hmr/runs/live_snapshots)
키 (3D 창):    R 정면 시점 · F 사람 따라가기 on/off
e2e = 노트북에서 프레임을 받은 시각 → 결과를 받은 시각 (같은 시계). 화면의 영상은 실시간이고 오버레이·메시는 마지막 결과다.
응답을 기다리는 프레임이 --max-inflight 보다 적을 때만 최신 프레임을 보낸다. 서버는 처리 중에 들어온 이전 프레임을 버리고 버림 응답을 준다.
프레임은 저장하지 않는다(스냅샷 s 를 누를 때만).
"""
import argparse
import io
import json
import os
import struct
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote, urlsplit

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[3]
SNAP = Path(__file__).resolve().parent / "runs" / "live_snapshots"
PALETTE = np.array([[0.65, 0.74, 0.86], [0.90, 0.55, 0.45], [0.45, 0.80, 0.50], [0.95, 0.75, 0.35], [0.75, 0.55, 0.90]])
FLIP = np.array([1.0, -1.0, -1.0])   # 카메라 좌표(OpenCV: x 오른쪽, y 아래, z 앞) → Open3D 표시(y 위, z 뒤)

ap = argparse.ArgumentParser()
ap.add_argument("--source", default="0", help="웹캠 번호 | rtsp(.env 사용) | 영상 파일·URL")
ap.add_argument("--rtsp-path", default=None, help="기본 .env 의 RTSP_PATH (예: stream1, stream2)")
ap.add_argument("--url", default=None, help="기본 .env 의 LIVE_URL, 없으면 ws://127.0.0.1:8765/ (SSH 터널)")
ap.add_argument("--mode", default="full", choices=["full", "body"], help="full = 손 포함, body = 몸만(빠름)")
ap.add_argument("--width", type=int, default=1280); ap.add_argument("--height", type=int, default=720)
ap.add_argument("--max-width", type=int, default=1280, help="전송 전 축소 폭 (0 = 원본)")
ap.add_argument("--quality", type=int, default=85)
ap.add_argument("--max-inflight", type=int, default=2, help="응답(결과·버림)을 기다리는 프레임 상한. 1 = 지연 최소, 2 = GPU 가 놀지 않음")
ap.add_argument("--send-fps", type=float, default=15.0, help="서버로 보내는 최대 fps")
ap.add_argument("--no-3d", action="store_true")
a = ap.parse_args()


def load_env():
    e = {}
    f = REPO / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1); e[k.strip()] = v.strip().strip('"')
    return {**e, **{k: v for k, v in os.environ.items() if k.startswith(("RTSP_", "LIVE_"))}}


def open_source():
    if a.source.isdigit():
        cap = cv2.VideoCapture(int(a.source), cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, a.width); cap.set(cv2.CAP_PROP_FRAME_HEIGHT, a.height)
        return cap, f"webcam {a.source}"
    if a.source == "rtsp":
        e = load_env()
        path = a.rtsp_path or e.get("RTSP_PATH", "stream1")
        url = f"rtsp://{quote(e['RTSP_USER'], safe='')}:{quote(e['RTSP_PASS'], safe='')}@{e['RTSP_HOST']}:{e.get('RTSP_PORT', '554')}/{path}"
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        return cv2.VideoCapture(url, cv2.CAP_FFMPEG), f"rtsp {e['RTSP_HOST']}/{path}"   # 계정은 출력하지 않는다
    return cv2.VideoCapture(a.source), a.source


class Grabber:
    """카메라 버퍼에 프레임이 쌓이지 않게 계속 읽고 최신 것만 둔다."""

    def __init__(self, cap, pace=0.0):
        self.cap, self.frame, self.t, self.n, self.ok, self.pace = cap, None, 0.0, 0, True, pace
        self.lock = threading.Lock(); self.times = deque(maxlen=60)
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while self.ok:
            t_prev = time.perf_counter()
            ok, f = self.cap.read()
            if not ok:
                self.ok = False; break
            if self.pace:   # 영상 파일은 원래 속도로 재생
                time.sleep(max(0.0, self.pace - (time.perf_counter() - t_prev)))
            now = time.perf_counter()
            with self.lock:
                self.frame, self.t = f, now; self.n += 1; self.times.append(now)

    def get(self):
        with self.lock:
            return self.frame, self.t, self.n

    def fps(self):
        t = self.times
        return (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 else 0.0


class Viewer:
    """Open3D 3D 메시 창 (선행 PoC 와 같은 방식)."""

    def __init__(self):
        import open3d as o3d
        self.o3d = o3d
        self.vis = o3d.visualization.VisualizerWithKeyCallback()
        self.vis.create_window("SAM 3D Body · 3D", 760, 720)
        opt = self.vis.get_render_option()
        opt.background_color = np.array([0.12, 0.12, 0.14]); opt.mesh_show_back_face = True
        self.mesh = o3d.geometry.TriangleMesh(); self.added = False
        self.vis.add_geometry(o3d.geometry.TriangleMesh.create_coordinate_frame(size=0.3))
        self.vis.register_key_callback(ord("R"), lambda v: self.front() or False)
        self.vis.register_key_callback(ord("F"), self._toggle_follow)
        self.center = np.array([0, 0, -3.0]); self.follow = True

    def _toggle_follow(self, _v):
        self.follow = not self.follow; print(f"[viewer] follow {'ON' if self.follow else 'OFF'}")
        return False

    def front(self):
        vc = self.vis.get_view_control()
        vc.set_lookat(self.center); vc.set_front([0, 0, 1]); vc.set_up([0, 1, 0]); vc.set_zoom(0.45)

    def update(self, verts, faces):
        o3d = self.o3d
        if not len(verts):
            self.mesh.clear()
        else:
            nv = verts.shape[1]
            V = np.concatenate([v.astype(np.float64) * FLIP for v in verts])
            F = np.concatenate([faces + i * nv for i in range(len(verts))])
            Cc = np.concatenate([np.tile(PALETTE[i % len(PALETTE)], (nv, 1)) for i in range(len(verts))])
            self.mesh.vertices = o3d.utility.Vector3dVector(V); self.mesh.triangles = o3d.utility.Vector3iVector(F)
            self.mesh.vertex_colors = o3d.utility.Vector3dVector(Cc); self.mesh.compute_vertex_normals()
            self.center = V.mean(0)
            if not self.added:
                self.vis.add_geometry(self.mesh, reset_bounding_box=True); self.front(); self.added = True
                return
            if self.follow:
                self.vis.get_view_control().set_lookat(self.center)
        if self.added:
            self.vis.update_geometry(self.mesh)

    def poll(self):
        ok = self.vis.poll_events(); self.vis.update_renderer()
        return ok


def pack(meta, payload):
    j = json.dumps(meta).encode()
    return struct.pack("!I", len(j)) + j + payload


def unpack(msg):
    (n,) = struct.unpack("!I", msg[:4])
    return json.loads(msg[4:4 + n]), msg[4 + n:]


def draw(frame, r, links, show_verts, scale):
    """마지막 결과를 실시간 프레임 위에 그린다. 결과 좌표는 전송 크기 기준이라 scale 로 화면 크기에 맞춘다."""
    for i, kp in enumerate(r["kp2d"]):
        kp = kp * scale
        if show_verts and len(r["verts"]):
            v = r["verts"][i].astype(np.float32)[::6]; K = r["K"]; h, w = frame.shape[:2]
            uv = np.stack([K[0, 0] * v[:, 0] / v[:, 2] + K[0, 2], K[1, 1] * v[:, 1] / v[:, 2] + K[1, 2]], 1) * scale
            uv = uv.astype(np.int32); ok = (uv[:, 0] >= 0) & (uv[:, 0] < w) & (uv[:, 1] >= 0) & (uv[:, 1] < h)
            frame[uv[ok, 1], uv[ok, 0]] = (230, 200, 160)
        for l in links:
            p, q = kp[l[0]].astype(int), kp[l[1]].astype(int)
            cv2.line(frame, tuple(p), tuple(q), (int(l[4]), int(l[3]), int(l[2])), 3)
        for p in kp[:21].astype(int):
            cv2.circle(frame, tuple(p), 4, (0, 140, 255), -1)
    for b in (r["bbox"] * scale).astype(int):
        if b[2] > b[0]:
            cv2.rectangle(frame, tuple(b[:2]), tuple(b[2:]), (0, 255, 0), 1)


def main():
    from websockets.sync.client import connect
    cap, desc = open_source()
    if not cap.isOpened():
        raise SystemExit(f"입력을 열 수 없음: {desc}")
    is_file = not a.source.isdigit() and a.source != "rtsp" and Path(a.source).exists()
    g = Grabber(cap, pace=1.0 / (cap.get(cv2.CAP_PROP_FPS) or 25) if is_file else 0.0)
    e = load_env()
    url = a.url or e.get("LIVE_URL") or "ws://127.0.0.1:8765/"
    hdr = {"Authorization": f"Bearer {e['LIVE_TOKEN']}"} if e.get("LIVE_TOKEN") and "proxy.runpod.net" in url else {}
    t_conn = time.perf_counter()
    s = connect(url, additional_headers=hdr, max_size=None, compression=None, open_timeout=30)
    target = "RunPod HTTPS 프록시" if "proxy.runpod.net" in url else urlsplit(url).netloc
    print(f"[client] {desc} → {target} (연결 {1000 * (time.perf_counter() - t_conn):.0f} ms), 서버 준비 대기...", flush=True)
    _, body0 = unpack(s.recv(timeout=300))   # 서버가 모델 로드·워밍업을 끝내면 faces·links 를 보낸다
    m0 = np.load(io.BytesIO(body0)); faces, links = m0["faces"], m0["links"]
    print(f"[client] ready (faces {len(faces)}, links {len(links)})", flush=True)

    t0 = time.perf_counter()
    sent = {}                     # frame_id → (캡처 시각, 전송 축소 배율)
    st = {"e2e": None, "res": None, "new": False, "inflight": 0, "closed": False, "mode": a.mode}
    e2es = deque(maxlen=1000); done_t = deque(maxlen=200); lock = threading.Lock()

    def receiver():
        try:
            for msg in s:
                meta, body = unpack(msg)
                now = time.perf_counter()
                with lock:
                    item = sent.pop(meta.get("frame_id"), None)
                    st["inflight"] = len(sent)
                    if meta.get("dropped") or item is None:   # 서버가 버린 프레임: 자리만 비운다
                        continue
                    tc, scale = item
                    st["e2e"] = (now - tc) * 1000; e2es.append(st["e2e"]); done_t.append(now)
                    r = dict(np.load(io.BytesIO(body))); r["meta"] = meta; r["scale"] = scale
                    st["res"], st["new"] = r, True
        except Exception:  # noqa: BLE001 — 연결 종료·네트워크 오류
            pass
        st["closed"] = True

    threading.Thread(target=receiver, daemon=True).start()
    view = None if a.no_3d else Viewer()
    win = "SAM 3D Body live (q quit, m mode, v verts, s snapshot)"
    show_verts = False; last_n = 0; fid = 0; t_sent = 0.0
    while g.ok:
        if st["closed"]:
            print("[client] 서버 연결이 끊김: RunPod 에서 live_server.py 가 실행 중인지, 주소·터널·토큰을 확인", flush=True)
            break
        f, tc, n = g.get()
        if f is None:
            time.sleep(0.01); continue
        with lock:
            can_send = n != last_n and st["inflight"] < a.max_inflight and time.perf_counter() - t_sent >= 1.0 / a.send_fps
        if can_send:
            last_n = n; t_sent = time.perf_counter()
            fs, scale = f, 1.0
            if a.max_width and f.shape[1] > a.max_width:
                scale = f.shape[1] / a.max_width
                fs = cv2.resize(f, (a.max_width, int(f.shape[0] / scale)), interpolation=cv2.INTER_AREA)
            enc = cv2.imencode(".jpg", fs, [cv2.IMWRITE_JPEG_QUALITY, a.quality])[1]
            fid += 1
            meta = {"frame_id": fid, "t_capture_ms": round((tc - t0) * 1000, 1), "last_e2e_ms": st["e2e"], "mode": st["mode"], "reply": "data"}
            with lock:
                sent[fid] = (tc, scale); st["inflight"] = len(sent)
            try:
                s.send(pack(meta, enc.tobytes()))
            except Exception:  # noqa: BLE001
                break
        frame = f.copy()
        r = st["res"]
        if r is not None:
            if st["new"] and view is not None:
                view.update(r["verts"].astype(np.float32), faces)
            st["new"] = False
            draw(frame, r, links, show_verts, r["scale"])
        now = time.perf_counter()
        fps = sum(1 for t in done_t if now - t < 3.0) / 3.0
        tm = r["timing"] if r is not None else np.zeros(4)
        hud = (f"{st['mode']}  result {fps:.1f} FPS  e2e {st['e2e'] or 0:.0f} ms  det {tm[0]:.0f}  hmr {tm[1]:.0f} ms  "
               f"people {len(r['kp2d']) if r is not None else 0}  peak {tm[3]:.0f} MB  cam {g.fps():.0f} fps")
        cv2.rectangle(frame, (0, 0), (frame.shape[1], 34), (0, 0, 0), -1)
        cv2.putText(frame, hud, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
        cv2.imshow(win, frame)
        if view is not None and not view.poll():
            break
        k = cv2.waitKey(1) & 0xFF
        if k in (ord("q"), 27):
            break
        if k == ord("m"):
            st["mode"] = "body" if st["mode"] == "full" else "full"; print("[client] mode", st["mode"], flush=True)
        if k == ord("v"):
            show_verts = not show_verts
        if k == ord("s") and r is not None:
            SNAP.mkdir(parents=True, exist_ok=True); ts = time.strftime("%Y%m%d_%H%M%S")
            cv2.imwrite(str(SNAP / f"snap_{ts}.jpg"), frame)
            if view is not None:
                for i, v in enumerate(r["verts"]):
                    m = view.o3d.geometry.TriangleMesh(view.o3d.utility.Vector3dVector(v.astype(np.float64)), view.o3d.utility.Vector3iVector(faces))
                    view.o3d.io.write_triangle_mesh(str(SNAP / f"snap_{ts}_p{i}.ply"), m)
                view.vis.capture_screen_image(str(SNAP / f"snap_{ts}_3d.png"), do_render=True)
            print("[client] snapshot", SNAP / f"snap_{ts}*", flush=True)
    g.ok = False; s.close(); cap.release(); cv2.destroyAllWindows()
    if view is not None:
        view.vis.destroy_window()
    if e2es:
        v = np.array(e2es)
        print(f"[client] e2e ms p50 {np.percentile(v, 50):.0f} p95 {np.percentile(v, 95):.0f} (n={len(v)}), camera {g.fps():.1f} fps", flush=True)


if __name__ == "__main__":
    main()
