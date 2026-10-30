"""기존 실험 진입점. 구현 정본: simai_ai.scene.texture_provenance."""
from simai_ai.scene import texture_provenance as _impl

# 기존 실험·시험이 쓰는 비공개 도우미도 같은 구현을 참조한다.
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
