"""처리기가 객체 저장소를 읽고 쓰는 주입점 (명세 14 객체 저장소, S15P31A101-239·-248).

객체 저장소는 비공개이고 임시 서명 주소로만 받고 올린다(14). 워커는 대여에 묶인 워커 전용 서명 API
(`POST /api/worker/jobs/{job_id}/objects/sign`, lease_id·op GET|PUT, S15P31A101-248)로 주소를 받는다.
- GET: 그 작업 input 에 있는 객체 주소(s3://)만.
- PUT: 키는 EC2 가 정한다(그 작업 전용). 처리기는 이름만 주고, 올린 객체의 s3:// 주소를 돌려받아 결과에 싣는다.
실제 HTTP 서명 클라이언트는 EC2 API 가 확정되면 붙인다(clips 업로드의 UploadSigner 주입과 같은 방식). 다른 작업
(vlm_rationale·static_reconstruct·incident_summary)도 이 주입점을 쓴다.
"""

from __future__ import annotations

from typing import Protocol

from .handlers import JobContext


class ObjectStoreError(Exception):
    """서명·전송 실패. retryable 이면 새 대여로 다시 해 볼 만하다(일시 단절, 서명 만료 등)."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class ObjectStore(Protocol):
    def get(self, ctx: JobContext, uri: str) -> bytes:
        """input 에 있는 s3:// 객체의 바이트."""
        ...

    def put(self, ctx: JobContext, name: str, content: bytes, content_type: str) -> str:
        """이 작업 전용 키에 올리고 그 s3:// 주소를 돌려준다. name 은 작업 안에서 객체를 가리는 이름이다."""
        ...
