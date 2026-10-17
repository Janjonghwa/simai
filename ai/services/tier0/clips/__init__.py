"""이벤트 클립 링 버퍼와 클립 업로드 (명세 5.1, 11.5, 14, FR-59, D-G3-19).

판정 로직은 없다. 클립 대상은 Risk Engine 생명주기 전이의 출처·등급(7.3 표)으로만 고른다.

- ring_buffer: 카메라별 압축 패킷 링 버퍼(메모리 전용, 최근 60초, 키프레임 경계, 바이트 상한)
- trigger: 클립 대상 전이(LIVE·CRITICAL·HIGH 발생 순간)
- media: 디코딩 없는 재다중화(H.264 Annex B)와 선택 사항인 업로드 직전 얼굴 흐림 단계(기본 꺼짐, 사용자 결정)
- record: media_clip 기록(11.3)
- upload: 임시 서명 주소 업로드, 다시 시도, EC2 단절 중 Network Volume 임시 저장
- service: 조립(EventClipService)
"""
