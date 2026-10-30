"""기존 실험 진입점. 구현 정본: simai_ai.scene.intrinsics_check."""
from simai_ai.scene import intrinsics_check as _impl

# 기존 실험·시험이 쓰는 비공개 도우미도 같은 구현을 참조한다.
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})

if __name__ == "__main__":
    raise SystemExit(_impl.main())
