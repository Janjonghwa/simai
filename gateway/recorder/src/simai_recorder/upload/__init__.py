"""관제 녹화의 작업 구간을 관제가 끝난 뒤 S3 에 올린다 (S15P31A101-668).

설계 docs/design/S15P31A101-668-recording-s3-playback.md, 계약 contracts/messages/recording_windows·recording_segment 1.0.0.

    plan.py    녹화 폴더 읽기(촬영 시각·키프레임·바이트 위치)와 작업 구간 → 카메라·epoch 별 키프레임 조각
    mux.py     조각을 재인코딩 없이 mp4 로 포장(PyAV, PTS = 촬영 시각 − 조각 시작)
    client.py  EC2(기기 토큰) 구간 조회·업로드 서명·조각 등록과 S3 PUT, 재시도
    runner.py  녹화 폴더마다 이어 올리기(upload-state.json)·관제 중 멈춤·동시 업로드

게이트웨이 노트북에는 ffmpeg 실행 파일이 없다(설치 스크립트·번들 어디에도 없음). 녹화 도구가 이미 쓰는 PyAV(휠에 FFmpeg 포함)로 포장한다.
"""
