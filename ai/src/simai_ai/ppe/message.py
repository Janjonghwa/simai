"""I1 `ppe_state` 메시지 만들기 (contracts/messages/ppe_state 1.0.0, 명세 12.3).

착용·미착용 상태에만 근거를 싣고(같은 상태의 근거 필수), UNKNOWN 은 근거 없이 보낸다. 위치·이미지는 싣지 않는다.
멱등 키는 (source_id, stream_epoch, state_sequence) 이고 순번은 PPE 서비스가 매긴다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .association import ITEMS, S_WORN, Sample
from .config import POLICY_VERSION
from .state import ItemView, PpeJudge

SCHEMA_VERSION = "1.0.0"
MAX_PEOPLE = 64


def _evidence(s: Sample) -> dict[str, Any]:
    return {
        "camera_id": s.camera_id,
        "stream_epoch": s.stream_epoch,
        "frame_sequence": s.frame_sequence,
        "capture_time_ms": s.capture_time_ms,
        "local_track_id": s.local_track_id,
        "candidate_state": "WORN" if s.kind == S_WORN else "NOT_WORN",
        "confidence": round(float(s.confidence), 4),
        "region_quality": round(float(s.region_quality), 3),
    }


def _item(v: ItemView) -> dict[str, Any]:
    return {
        "state": v.state,
        "reason": v.reason,
        "valid_cells": v.valid_cells,
        "support_ratio": v.support_ratio,
        "last_evidence_at_ms": v.last_evidence_at,
        "expires_at_ms": v.expires_at,
        "evidence": [_evidence(s) for s in v.evidence],
    }


def build_ppe_state(judge: PpeJudge, views: Mapping[str, Mapping[str, ItemView]], *, msg_id: str, sent_at: int,
                    source_id: str, stream_epoch: int, state_sequence: int, model_version: str,
                    checkpoint_sha256: str, input_provenance: Mapping[str, Any],
                    policy_version: str = POLICY_VERSION) -> dict[str, Any]:
    """judge.evaluate() 결과로 봉투까지 갖춘 ppe_state 문서를 만든다. 64명을 넘으면 ValueError(계약 상한)."""
    if len(views) > MAX_PEOPLE:
        raise ValueError(f"ppe_state 한 메시지에 사람은 {MAX_PEOPLE}명까지다: {len(views)}")
    people = []
    for wid in sorted(views):
        track = judge.tracks[wid]
        people.append({
            "world_id": wid,
            "identity_revision": track.identity_revision,
            "capture_time_ms": track.last_seen_ms,
            **{item: _item(views[wid][item]) for item in ITEMS},
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "msg_type": "ppe_state",
        "msg_id": msg_id,
        "sent_at": sent_at,
        "source_id": source_id,
        "payload": {
            "stream_epoch": stream_epoch,
            "state_sequence": state_sequence,
            "model_version": model_version,
            "checkpoint_sha256": "sha256:" + checkpoint_sha256.removeprefix("sha256:"),
            "policy_version": policy_version,
            "input_mode": "FULL_FRAME",
            "input_provenance": dict(input_provenance),
            "people": people,
        },
    }
