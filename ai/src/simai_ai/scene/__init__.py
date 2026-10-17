"""정적 3D Scene (EXP-01 채택분을 옮길 자리)과 BEV 평면도.

- `bev`: 보정과 카메라별 프레임으로 BEV 평면도(보조 화면, 명세 4.1)를 만든다. numpy 가 필요하다.
- `room_outline`: BEV 의 대략적 방 외곽(inferred)과 바깥으로 넓히기(명세 6.3 방 형상 제한용).
- `bev_export`: BEV 를 PNG + JSON(World 대응 정보)으로 저장한다.

하위 모듈은 numpy 를 쓰므로 여기서 미리 import 하지 않는다.
"""
