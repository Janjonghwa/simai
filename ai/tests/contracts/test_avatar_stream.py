"""EXP-02 아바타 스트림 계약(avatar_pose_batch·avatar_asset·avatar_frame)의 스키마 밖 규칙 시험 (S15P31A101-249).

스키마가 못 보는 규칙(회전 길이 ≤ π, 관절 수 = 자산 joint_count, 관절 0 = 0 회전, 한 묶음 안 world_id 유일,
표면 비율 합 1, EC2 가 포즈를 바꾸지 않고 다시 싣기)을 예시로 확인하고, 깨는 반례를 막는지 본다.
송신(-227 워커)·중계(-138 EC2) 구현의 계약 시험이 같은 규칙을 쓴다(docs/design/avatar-pose-stream.md).
"""

import copy
import json
import math

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from referencing import Registry, Resource

from simai_ai.contracts import models
from simai_ai.providers import contract
from simai_ai.providers.testing import load_example

PI_TOL = 1e-4  # 예시는 소수 넷째 자리로 반올림한다


def _registry_and(name: str):
    registry = Registry()
    target = None
    for path in contract.contracts_dir().rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        if path.name == f"{name}.schema.json":
            target = schema
    return Draft202012Validator(target, registry=registry)


def _schema_errors(name: str, doc: dict) -> list[str]:
    return [e.message for e in _registry_and(name).iter_errors(doc)]


def _rotation_violations(where: str, pose: dict, joint_count: int | None) -> list[str]:
    out = []
    rots = pose["joint_rotations"]
    if joint_count is not None and len(rots) != joint_count:
        out.append(f"{where}: joint_rotations 길이 {len(rots)} != joint_count {joint_count}")
    if rots and rots[0] is not None and any(abs(c) > PI_TOL for c in rots[0]):
        out.append(f"{where}: 관절 0 회전이 0 이 아니다(몸 방향은 root_rotation)")
    for i, r in enumerate([pose["root_rotation"], *rots]):
        if r is not None and math.hypot(*r) > math.pi + PI_TOL:
            out.append(f"{where}: 회전 {i - 1 if i else 'root'} 의 길이 {math.hypot(*r):.4f} > π")
    return out


def violations(batch: dict | None = None, assets: list[dict] = (), frame: dict | None = None) -> list[str]:
    """스키마 밖 규칙 위반 목록. 빈 목록이면 통과."""
    out = []
    joints, revisions, owner = {}, {}, {}
    for a in assets:
        p = a["payload"]
        key = (p["world_id"], p["stream_epoch"], p["appearance_version"])
        joints[key] = p["skeleton"]["joint_count"]
        revisions[key] = p["identity_revision"]
        files = [p["mesh"]["object_uri"], p["surface_source"]["mask_uri"], (p["texture"] or {}).get("object_uri")]
        for uri in filter(None, files):
            if owner.setdefault(uri, key) != key:
                out.append(f"asset {p['world_id']}: 파일 주소 {uri} 를 다른 자산 {owner[uri]} 과 같이 쓴다(덮어쓰기)")
        if p["skeleton"]["pelvis_joint_index"] >= p["skeleton"]["joint_count"]:
            out.append(f"asset {p['world_id']}: pelvis_joint_index 가 joint_count 밖")
        ver, prev = p["appearance_version"], p["previous_appearance_version"]
        if (ver == 0 and prev is not None) or (ver > 0 and (prev is None or prev >= ver)):
            out.append(f"asset {p['world_id']}: previous_appearance_version {prev} 이 버전 {ver} 앞이 아니다")
        for key in ("ratios", "face_ratios"):
            ratios = p["surface_source"][key]
            if abs(sum(ratios.values()) - 1.0) > 1e-6:
                out.append(f"asset {p['world_id']}: 표면 비율({key}) 합 {sum(ratios.values())} != 1")
    if batch is not None:
        epoch = batch["payload"]["stream_epoch"]
        poses = batch["payload"]["poses"]
        ids = [p["world_id"] for p in poses]
        if len(ids) != len(set(ids)):
            out.append("batch: 같은 world_id 가 한 묶음에 둘 이상")
        for p in poses:
            key = (p["world_id"], epoch, p["appearance_version"])
            out += _rotation_violations(f"batch {p['world_id']}", p["pose"], joints.get(key))
            if key in revisions and revisions[key] > p["identity_revision"]:
                out.append(f"batch {p['world_id']}: 자산 identity_revision {revisions[key]} 이 포즈 {p['identity_revision']} 보다 크다")
    if frame is not None:
        avatars = frame["payload"]["avatars"]
        ids = [a["world_id"] for a in avatars]
        if len(ids) != len(set(ids)):
            out.append("frame: 같은 world_id 가 한 메시지에 둘 이상")
        relayed = {p["world_id"]: p for p in batch["payload"]["poses"]} if batch else {}
        paths = {}
        for a in avatars:
            if a["asset"]:
                key = (a["world_id"], a["asset"]["stream_epoch"], a["appearance_version"])
                for url in filter(None, (a["asset"]["mesh_url"], a["asset"]["texture_url"], a["asset"]["surface_source_url"])):
                    if paths.setdefault(url, key) != key:
                        out.append(f"frame {a['world_id']}: 경로 {url} 를 다른 자산 {paths[url]} 과 같이 쓴다")
                    prefix = f"/api/avatars/{key[0]}/epochs/{key[1]}/versions/{key[2]}/"
                    if not url.startswith(prefix):
                        out.append(f"frame {a['world_id']}: 경로 {url} 가 자산 키 {prefix} 와 맞지 않는다")
                if a["asset"]["identity_revision"] > a["identity_revision"]:
                    out.append(f"frame {a['world_id']}: 자산 identity_revision 이 포즈보다 크다")
                if batch and a["asset"]["stream_epoch"] != batch["payload"]["stream_epoch"]:
                    out.append(f"frame {a['world_id']}: 포즈 묶음과 다른 stream_epoch 의 자산")
            count = a["asset"]["joint_count"] if a["asset"] else None
            if a["asset"] and a["asset"]["pelvis_joint_index"] >= count:
                out.append(f"frame {a['world_id']}: pelvis_joint_index 가 joint_count 밖")
            out += _rotation_violations(f"frame {a['world_id']}", a["pose"], count)
            src = relayed.get(a["world_id"])
            if src and src["capture_time_ms"] == a["capture_time_ms"] and src["pose"] != a["pose"]:
                out.append(f"frame {a['world_id']}: EC2 가 같은 촬영 시각의 포즈를 바꿔 실었다")
            for key in ("surface_ratios", "face_ratios"):
                if a["asset"] and abs(sum(a["asset"][key].values()) - 1.0) > 1e-6:
                    out.append(f"frame {a['world_id']}: 표면 비율({key}) 합 != 1")
    return out


@pytest.fixture
def docs():
    return {
        "batch": load_example("avatar_pose_batch.json"),
        "asset": load_example("avatar_asset.json"),
        "textured": load_example("avatar_asset.textured.json"),
        "frame": load_example("avatar_frame.json"),
    }


def test_examples_keep_rules_outside_schema(docs):
    assert violations(docs["batch"], [docs["asset"], docs["textured"]], docs["frame"]) == []
    assert violations(frame=load_example("avatar_frame.asset_pending.json")) == []


def _bound(assets, batch, pose):
    """EC2 가 포즈에 묶는 자산: 같은 world_id·stream_epoch·appearance_version 이고 자산 차수 ≤ 포즈 차수인 것만
    (없으면 None → avatar_frame asset null, 뷰어 캡슐). 설계 'ID 교정 뒤 외형'."""
    key = (pose["world_id"], batch["payload"]["stream_epoch"], pose["appearance_version"])
    found = [
        a for a in assets
        if (a["payload"]["world_id"], a["payload"]["stream_epoch"], a["payload"]["appearance_version"]) == key
        and a["payload"]["identity_revision"] <= pose["identity_revision"]
    ]
    return found[0] if found else None


def test_examples_bind_poses_to_assets(docs):
    """예시 묶음의 모든 포즈가 예시 자산을 가리킨다(그래야 위 관절 수 검사가 실제로 돈다)."""
    assets = [docs["asset"], docs["textured"]]
    assert all(_bound(assets, docs["batch"], p) for p in docs["batch"]["payload"]["poses"])


def test_worker_restart_does_not_bind_old_epoch_asset(docs):
    """워커가 재시작하면 새 stream_epoch 에서 appearance_version 을 0 부터 다시 센다.
    이전 epoch 의 같은 (world_id, 버전) 자산(다른 사람·다른 체형일 수 있음)에 묶지 않는다."""
    batch = copy.deepcopy(docs["batch"])
    batch["payload"]["stream_epoch"] += 60_000
    batch["payload"]["batch_sequence"] = 0
    old = [docs["asset"], docs["textured"]]
    assert all(_bound(old, batch, p) is None for p in batch["payload"]["poses"])


# 반례 -----------------------------------------------------------------------


def test_rotation_longer_than_pi_passes_schema_but_is_caught(docs):
    """성분마다 [-π, π] 라 스키마는 통과하지만 회전각(길이)이 π 를 넘는다(도·라디안 혼동 일부)."""
    batch = copy.deepcopy(docs["batch"])
    batch["payload"]["poses"][0]["pose"]["joint_rotations"][1] = [3.0, 3.0, 0.0]
    assert _schema_errors("avatar_pose_batch", batch) == []
    assert any("> π" in v for v in violations(batch, [docs["asset"], docs["textured"]]))


def test_rotation_exactly_pi_is_allowed_and_beyond_schema_bound_is_rejected(docs):
    batch = copy.deepcopy(docs["batch"])
    rots = batch["payload"]["poses"][0]["pose"]["joint_rotations"]
    rots[1] = [3.1416, 0.0, 0.0]
    assert _schema_errors("avatar_pose_batch", batch) == []
    assert violations(batch, [docs["asset"], docs["textured"]]) == []
    rots[1] = [3.1417, 0.0, 0.0]
    assert _schema_errors("avatar_pose_batch", batch)
    with pytest.raises(ValidationError):
        models.AvatarPoseBatch.model_validate(batch)


def test_joint_count_mismatch_is_caught(docs):
    """관절 하나가 빠지면 뒤 관절이 모두 한 칸씩 밀린다. 스키마는 길이를 모른다."""
    batch = copy.deepcopy(docs["batch"])
    batch["payload"]["poses"][0]["pose"]["joint_rotations"].pop()
    assert _schema_errors("avatar_pose_batch", batch) == []
    assert any("joint_count" in v for v in violations(batch, [docs["asset"], docs["textured"]]))


def test_nonzero_joint0_is_caught(docs):
    """몸 방향을 관절 0 과 root_rotation 에 겹쳐 실으면 뷰어가 두 번 돌린다."""
    batch = copy.deepcopy(docs["batch"])
    batch["payload"]["poses"][1]["pose"]["joint_rotations"][0] = [0.0, 0.0, 0.5]
    assert any("관절 0" in v for v in violations(batch, [docs["asset"], docs["textured"]]))


def test_duplicate_person_in_one_batch_is_caught(docs):
    batch = copy.deepcopy(docs["batch"])
    batch["payload"]["poses"].append(copy.deepcopy(batch["payload"]["poses"][0]))
    assert _schema_errors("avatar_pose_batch", batch) == []
    assert any("같은 world_id" in v for v in violations(batch, [docs["asset"], docs["textured"]]))


def test_relay_must_not_change_pose(docs):
    frame = copy.deepcopy(docs["frame"])
    frame["payload"]["avatars"][0]["pose"]["joint_rotations"][2] = [0.0, 0.0, 0.0]
    assert any("바꿔" in v for v in violations(docs["batch"], [docs["asset"], docs["textured"]], frame))


def test_surface_ratios_must_sum_to_one(docs):
    asset = copy.deepcopy(docs["textured"])
    asset["payload"]["surface_source"]["ratios"]["UNOBSERVED"] = 0.30
    assert _schema_errors("avatar_asset", asset) == []
    assert any("표면 비율" in v for v in violations(assets=[asset]))


def test_face_ratios_must_sum_to_one_and_have_no_interpolation(docs):
    """얼굴은 관측·미관측만 있다. 비율 합이 1 이 아니면 어딘가를 지어낸 것이다."""
    asset = copy.deepcopy(docs["textured"])
    asset["payload"]["surface_source"]["face_ratios"] = {"OBSERVED": 0.55, "UNOBSERVED": 0.30}
    assert _schema_errors("avatar_asset", asset) == []
    assert any("face_ratios" in v for v in violations(assets=[asset]))
    asset["payload"]["surface_source"]["face_ratios"]["INTERPOLATED"] = 0.15
    assert _schema_errors("avatar_asset", asset)
    with pytest.raises(ValidationError):
        models.AvatarAsset.model_validate(asset)


def test_observed_face_needs_no_consent_or_blur(docs):
    """2026-10-23 결정: 동의 기록 없이 관측 얼굴 픽셀을 그대로(OBSERVED) 쓸 수 있다."""
    payload = docs["textured"]["payload"]
    assert payload["face_policy"] == "OBSERVED" and "consent_ref" not in payload
    assert payload["surface_source"]["face_ratios"]["OBSERVED"] > 0
    assert _schema_errors("avatar_asset", docs["textured"]) == []


def test_pelvis_index_must_be_inside_skeleton(docs):
    """pelvis 관절이 뼈대 밖이면 뷰어가 Tier 0 pelvis 에 맞출 관절이 없다(경계: joint_count - 1 은 허용)."""
    asset = copy.deepcopy(docs["asset"])
    asset["payload"]["skeleton"]["pelvis_joint_index"] = asset["payload"]["skeleton"]["joint_count"] - 1
    assert violations(assets=[asset]) == []
    asset["payload"]["skeleton"]["pelvis_joint_index"] += 1
    assert _schema_errors("avatar_asset", asset) == []
    assert any("pelvis_joint_index" in v for v in violations(assets=[asset]))
    frame = copy.deepcopy(docs["frame"])
    frame["payload"]["avatars"][0]["asset"]["pelvis_joint_index"] = 127
    assert any("pelvis_joint_index" in v for v in violations(frame=frame))


@pytest.mark.parametrize(
    ("version", "previous", "ok"),
    [(0, None, True), (0, 0, False), (3, 2, True), (3, 0, True), (3, None, False), (3, 3, False), (3, 4, False)],
)
def test_previous_appearance_version_comes_before(docs, version, previous, ok):
    """첫 버전(0)은 앞 버전이 없고(null), 그 밖에는 앞 버전이 현재보다 작다(덮어쓰기·역행 금지)."""
    asset = copy.deepcopy(docs["textured"])
    asset["payload"].update(appearance_version=version, previous_appearance_version=previous)
    assert _schema_errors("avatar_asset", asset) == []
    assert (not any("previous_appearance_version" in v for v in violations(assets=[asset]))) is ok


def test_merge_survivor_keeps_last_confirmed_appearance(docs):
    """병합·분할 뒤 같은 world_id 로 남은 사람은 교정 전 자산(마지막 확정 외형)을 계속 가리킬 수 있다(명세 20.3).
    교정 뒤에 만든 자산이 교정 전 포즈에 묶이는 반대 방향(자산 차수 > 포즈 차수)은 다른 사람일 수 있어 막는다."""
    batch, frame = copy.deepcopy(docs["batch"]), copy.deepcopy(docs["frame"])
    for p in batch["payload"]["poses"]:
        p["identity_revision"] += 1
    for a in frame["payload"]["avatars"]:
        a["identity_revision"] += 1
        a["pose"] = next(p["pose"] for p in batch["payload"]["poses"] if p["world_id"] == a["world_id"])
    assets = [docs["asset"], docs["textured"]]
    assert violations(batch, assets, frame) == []
    newer = copy.deepcopy(docs["textured"])
    newer["payload"]["identity_revision"] = 9
    assert any("identity_revision" in v for v in violations(docs["batch"], [docs["asset"], newer]))
    frame["payload"]["avatars"][0]["asset"]["identity_revision"] = 9
    assert any("identity_revision" in v for v in violations(frame=frame))


def test_newer_identity_asset_is_not_bound_to_older_pose(docs):
    """자산 차수 > 포즈 차수면 묶지 않는다(None → asset null). 같으면 묶고, 포즈 차수가 더 크면(교정 뒤 유지) 묶는다."""
    batch = docs["batch"]
    pose = next(p for p in batch["payload"]["poses"] if p["world_id"] == "w-0012")
    asset = copy.deepcopy(docs["textured"])
    asset["payload"]["identity_revision"] = pose["identity_revision"]
    assert _bound([asset], batch, pose) is asset
    asset["payload"]["identity_revision"] = pose["identity_revision"] - 1
    assert _bound([asset], batch, pose) is asset
    asset["payload"]["identity_revision"] = pose["identity_revision"] + 1
    assert _bound([asset], batch, pose) is None


def test_split_off_id_does_not_inherit_original_asset(docs):
    """분할로 갈라져 나간 새 world_id 는 원래 ID 의 자산을 물려받지 않는다(자기 자산이 생길 때까지 캡슐)."""
    batch = copy.deepcopy(docs["batch"])
    original = next(p for p in batch["payload"]["poses"] if p["world_id"] == "w-0012")
    split_off = dict(original, world_id="w-0099", identity_revision=original["identity_revision"])
    assets = [docs["asset"], docs["textured"]]
    assert _bound(assets, batch, original) is docs["textured"]
    assert _bound(assets, batch, split_off) is None


def test_viewer_paths_match_asset_key(docs):
    """mesh·texture·지도 경로가 모두 /api/avatars/<world_id>/epochs/<epoch>/versions/<ver>/ 로 자산 키와 맞아야 한다."""
    assert violations(frame=docs["frame"]) == []
    for field, bad in [
        ("mesh_url", "/api/avatars/w-0012/versions/3/mesh.glb"),  # epoch 없는 옛 형식
        ("texture_url", "/api/avatars/w-0012/epochs/1760342000001/versions/3/albedo.png"),  # 다른 epoch
        ("surface_source_url", "/api/avatars/w-0012/epochs/1760342000000/versions/2/surface_source.png"),  # 다른 버전
        ("mesh_url", "/api/avatars/w-0013/epochs/1760342000000/versions/3/mesh.glb"),  # 다른 사람
    ]:
        frame = copy.deepcopy(docs["frame"])
        frame["payload"]["avatars"][0]["asset"][field] = bad
        assert _schema_errors("avatar_frame", frame) == [], field
        assert any("자산 키" in v for v in violations(frame=frame)), (field, bad)


def test_assets_of_two_epochs_with_same_version_do_not_share_files(docs):
    """워커 재시작 뒤 같은 (world_id, 버전) 자산이 둘 생겨도 파일 주소·REST 경로가 epoch 로 갈려 덮어쓰지 않는다."""
    old = docs["asset"]
    new = copy.deepcopy(old)
    new["payload"]["stream_epoch"] = old["payload"]["stream_epoch"] + 60_000
    assert any("덮어쓰기" in v for v in violations(assets=[old, new]))
    e = str(new["payload"]["stream_epoch"])
    new["payload"]["mesh"]["object_uri"] = old["payload"]["mesh"]["object_uri"].replace(str(old["payload"]["stream_epoch"]), e)
    assert new["payload"]["mesh"]["object_uri"] != old["payload"]["mesh"]["object_uri"]
    assert violations(assets=[old, new]) == []
    batch = copy.deepcopy(docs["batch"])
    batch["payload"]["stream_epoch"] = new["payload"]["stream_epoch"]
    pose = next(p for p in batch["payload"]["poses"] if p["world_id"] == old["payload"]["world_id"])
    assert _bound([old, new], batch, pose) is new
    assert _bound([old, new], docs["batch"], pose) is old


def test_viewer_paths_carry_epoch(docs):
    for a in docs["frame"]["payload"]["avatars"]:
        assert f"/epochs/{a['asset']['stream_epoch']}/" in a["asset"]["mesh_url"]
    frame = copy.deepcopy(docs["frame"])
    a0, a1 = frame["payload"]["avatars"]
    a1["asset"]["mesh_url"] = a0["asset"]["mesh_url"]
    assert any("같이 쓴다" in v for v in violations(frame=frame))


def test_people_per_message_are_capped(docs):
    batch = copy.deepcopy(docs["batch"])
    one = batch["payload"]["poses"][0]
    batch["payload"]["poses"] = [dict(one, world_id=f"w-{i:04d}") for i in range(64)]
    assert _schema_errors("avatar_pose_batch", batch) == []
    batch["payload"]["poses"].append(dict(one, world_id="w-0064"))
    assert _schema_errors("avatar_pose_batch", batch)
    with pytest.raises(ValidationError):
        models.AvatarPoseBatch.model_validate(batch)


def test_textured_viewer_asset_needs_surface_map(docs):
    frame = copy.deepcopy(docs["frame"])
    asset = frame["payload"]["avatars"][0]["asset"]
    assert asset["texture_url"] and asset["surface_source_url"]
    asset["surface_source_url"] = None
    assert _schema_errors("avatar_frame", frame)


def test_unknown_joint_is_null_but_unknown_root_is_rejected(docs):
    """모르는 관절은 null(뷰어는 기준 회전), 몸 방향을 모르면 포즈를 보내지 않는다(null 불가)."""
    batch = copy.deepcopy(docs["batch"])
    pose = batch["payload"]["poses"][0]["pose"]
    assert None in pose["joint_rotations"]
    pose["root_rotation"] = None
    assert _schema_errors("avatar_pose_batch", batch)
    with pytest.raises(ValidationError):
        models.AvatarPoseBatch.model_validate(batch)


def test_pending_asset_means_capsule_not_guess(docs):
    """EC2 가 자산을 아직 못 받았으면 asset null 이고, 자산 없이 관절 수를 추측하지 않는다."""
    frame = load_example("avatar_frame.asset_pending.json")
    avatar = frame["payload"]["avatars"][0]
    assert avatar["asset"] is None
    assert not any("joint_count" in v for v in violations(frame=frame))
