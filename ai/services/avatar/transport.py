"""I6 전송. 네트워크는 송신 스레드만 소유하고 사람별 포즈 한 개만 보관한다."""

import json
import logging
import threading
import uuid
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener

from services.worker.transport import HttpTransport, NoRedirect
from simai_ai.hmr.diagnostics import exception_summary
from simai_ai.hmr.stream import DEFAULT_MAX_AGE_MS

log = logging.getLogger(__name__)


class AvatarUpload:
    def __init__(self, base_url, token):
        self.http = HttpTransport(base_url, token)
        self._token = token

    def __call__(self, world_id, epoch, version, glb):
        try:
            uri = self._upload(world_id, epoch, version, glb)
        except Exception as error:
            log.warning("아바타 자산 업로드 실패: world_id=%s epoch=%s version=%s %s",
                        world_id, epoch, version, exception_summary(error, secrets=(self._token,)))
            raise
        log.info("아바타 자산 업로드 성공: world_id=%s epoch=%s version=%s bytes=%s",
                 world_id, epoch, version, glb.size_bytes)
        return uri

    def png(self, world_id, epoch, version, file_name, asset):
        if file_name not in {"albedo.png", "surface_source.png"}:
            raise ValueError("지원하지 않는 아바타 PNG 이름")
        try:
            uri = self._upload(world_id, epoch, version, asset, file_name=file_name)
        except Exception as error:
            log.warning("아바타 PNG 업로드 실패: world_id=%s version=%s file=%s %s",
                        world_id, version, file_name, exception_summary(error, secrets=(self._token,)))
            raise
        log.info("아바타 PNG 업로드 성공: world_id=%s version=%s file=%s bytes=%s",
                 world_id, version, file_name, asset.size_bytes)
        return uri

    def _upload(self, world_id, epoch, version, glb, *, file_name=None):
        body = {"purpose": "AVATAR_ASSET", "world_id": world_id,
                "stream_epoch": epoch, "appearance_version": version,
                "content_type": "image/png" if file_name else "model/gltf-binary",
                "size_bytes": glb.size_bytes, "sha256": glb.sha256.removeprefix("sha256:")}
        if file_name:
            body["file_name"] = file_name
        response = self.http.post("/api/uploads/sign", body, 15)
        if response.status != 200 or not response.body:
            raise ValueError(f"아바타 업로드 서명 실패 HTTP={response.status}")
        signed = response.body
        if urlsplit(signed["upload_url"]).scheme != "https" or signed.get("method") != "PUT":
            raise ValueError("아바타 업로드는 HTTPS PUT이어야 한다")
        uri = signed["object_uri"]
        suffix = f"/{world_id}/e{epoch}/v{version}/{file_name or 'mesh.glb'}"
        if not uri.startswith("s3://") or not uri.endswith(suffix):
            raise ValueError("아바타 객체 키에 World ID·epoch·version이 없다")
        request = Request(signed["upload_url"], data=glb.data, method="PUT", headers=signed["headers"])
        with build_opener(NoRedirect()).open(request, timeout=30) as result:
            if result.status not in (200, 201, 204):
                raise ValueError("아바타 업로드 실패")
        return uri


def websocket_connector(base_url, token):
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
        raise ValueError("아바타 EC2 주소는 HTTPS여야 한다")
    url = urlunsplit(("wss", parts.netloc, "/ws/worker/avatar", "", ""))

    def connect():
        from websockets.sync.client import connect as ws_connect
        return ws_connect(url, additional_headers={"Authorization": f"Bearer {token}"},
                          open_timeout=10, close_timeout=1, ping_interval=10, ping_timeout=10,
                          max_queue=1, compression="deflate")
    return connect


class AvatarSender:
    def __init__(self, connect, *, clock_ms, monotonic, source_id="worker-01", stream_epoch=None,
                 max_age_ms=DEFAULT_MAX_AGE_MS, log_secrets=()):
        self.connect, self.clock_ms, self.monotonic, self.source_id = connect, clock_ms, monotonic, source_id
        self.epoch = clock_ms() if stream_epoch is None else stream_epoch
        self.max_age_ms = max_age_ms
        self.sequence = self.dropped = 0
        self._lock = threading.Lock()
        self._poses, self._assets = {}, {}
        self._seen = {}
        self._context = {}
        self._socket = None
        self._retry_at, self._backoff, self._announce_at = 0., 1., 0.
        self._connections = self._sent_batches = 0
        # 실제로 소켓에 쓴 포즈 배치·자산 알림 누계(계측, S15P31A101-412)
        self.sent_batches_total = self.sent_assets_total = 0
        self._report_at = monotonic() + 60
        self._log_secrets = log_secrets

    def asset(self, payload):
        with self._lock:
            self._assets[payload["world_id"]] = payload
            self._seen[payload["world_id"]] = self.clock_ms()
            self._announce_at = 0.

    def poses(self, poses, **context):
        with self._lock:
            if self._context != context:
                self.dropped += len(self._poses)
                self._poses.clear()
            self._context = context
            for pose in poses:
                wid = pose["world_id"]
                self.dropped += int(wid in self._poses)
                self._poses[wid] = pose

    def discard_poses(self):
        with self._lock:
            self.dropped += len(self._poses)
            self._poses.clear()

    def retain(self, active):
        with self._lock:
            self._assets = {k: v for k, v in self._assets.items() if k in active}
            self._seen = {k: self.clock_ms() for k in active}
            gone = set(self._poses) - active
            for wid in gone:
                del self._poses[wid]
            self.dropped += len(gone)

    def _send(self, kind, payload):
        msg = {"schema_version": "1.0.0", "msg_type": kind, "msg_id": str(uuid.uuid4()),
               "sent_at": self.clock_ms(), "source_id": self.source_id, "payload": payload}
        raw = json.dumps(msg, allow_nan=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > 512 * 1024:
            raise ValueError("I6 메시지 크기 상한 초과")
        self._socket.send(raw)

    def tick(self):
        now = self.monotonic()
        if now >= self._report_at:
            log.info("아바타 1분 송신 집계: pose_batches=%s pose_batches_total=%s asset_msgs_total=%s "
                     "dropped_poses=%s connected=%s",
                     self._sent_batches, self.sent_batches_total, self.sent_assets_total, self.dropped,
                     self._socket is not None)
            self._sent_batches = 0
            self._report_at = now + 60
        if now < self._retry_at:
            return
        try:
            if self._socket is None:
                self._socket = self.connect()
                self.discard_poses()  # 연결 전 포즈는 재생하지 않는다
                self._announce_at = 0.
                self._connections += 1
                log.info("아바타 %s: epoch=%s", "연결됨" if self._connections == 1 else "재연결됨", self.epoch)
            # I6는 단방향이다. 수신 대기 없이 close/keepalive 실패를 확인한다.
            # 자산 확정 전이나 AdmissionGate 차단 중에도 서버 재기동을 감지한다.
            try:
                self._socket.recv(timeout=0)
            except TimeoutError:
                pass
            with self._lock:
                self._assets = {k: v for k, v in self._assets.items()
                                if self.clock_ms() - self._seen.get(k, 0) <= 30_000}
                assets = list(self._assets.values()) if now >= self._announce_at else []
                self._announce_at = now + 10 if assets else self._announce_at
                poses, self._poses = list(self._poses.values()), {}
                context, dropped = dict(self._context), self.dropped
            for asset in assets:
                self._send("avatar_asset", asset)
                self.sent_assets_total += 1
            fresh = [p for p in poses if 0 <= self.clock_ms() - p["capture_time_ms"] <= self.max_age_ms]
            for start in range(0, len(fresh), 64):
                group = fresh[start:start + 64]
                sequence = self.sequence
                self.sequence += 1  # 전송 중 잃은 묶음도 순번 빈칸으로 남긴다
                self._send("avatar_pose_batch", {"stream_epoch": self.epoch, "batch_sequence": sequence,
                           "dropped_poses": dropped, **context, "poses": group})
                self._sent_batches += 1
                self.sent_batches_total += 1
            self._backoff = 1.
        except Exception as error:
            log.warning("아바타 연결 끊김/접속 실패: %s retry_s=%s",
                        exception_summary(error, secrets=self._log_secrets), self._backoff)
            self.close()
            self.discard_poses()
            self._retry_at = now + self._backoff
            self._backoff = min(10., self._backoff * 2)

    def close(self):
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
