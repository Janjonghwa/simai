"""순수 병합 판정(S15P31A101-661). 표준 라이브러리만."""

from simai_ai.tracking.identity_carry import pure_merge_absorbed, split_marks


def test_new_alias_with_revision_rise_is_pure_merge():
    assert pure_merge_absorbed("w1", 0, (), None, 1, ("w2",), None) == ("w2",)
    assert pure_merge_absorbed("w1", 1, ("w2",), None, 2, ("w2", "w3", "w4"), None) == ("w3", "w4")


def test_not_pure_merge_cases():
    assert pure_merge_absorbed("w1", 1, (), None, 1, ("w2",), None) is None            # 차수 그대로
    assert pure_merge_absorbed("w1", 0, (), None, 1, (), None) is None                 # 별칭 변화 없음(분할·맞바꿈)
    assert pure_merge_absorbed("w1", 0, ("w2",), None, 1, ("w3",), None) is None       # 별칭이 빠짐
    assert pure_merge_absorbed("w1", 0, (), None, 1, ("w2",), "w9") is None            # split_from 새로 생김
    assert pure_merge_absorbed("w1", 0, (), None, 1, ("w2",), None, {("w1", 1)}) is None   # 같은 차수 분할과 겹침


def test_split_marks_pair_source_with_revision():
    tracks = [{"world_id": "w5", "identity_revision": 3, "split_from": "w1"}, {"world_id": "w1", "identity_revision": 3},
              {"world_id": "w6", "identity_revision": True, "split_from": "w2"}]
    assert split_marks(tracks) == {("w1", 3)}
