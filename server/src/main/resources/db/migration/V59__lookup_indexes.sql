-- 서버 전수 점검(S15P31A101-632)에서 찾은 조회·FK 열 색인. 모두 행이 적은 표라 잠금 시간이 짧다.

-- 계정의 살아 있는 갱신 토큰 폐기(RefreshTokenRepository.revokeAllActive)와 만료 행 정리(RefreshTokenCleaner)
CREATE INDEX idx_refresh_token_user_id ON refresh_token (user_id);
CREATE INDEX idx_refresh_token_expires_at ON refresh_token (expires_at);

-- 이벤트별 운영자 확인·종료 조회(이벤트 상세, 재전송)
CREATE INDEX idx_event_ack_event_id ON event_ack (event_id);
CREATE INDEX idx_event_close_event_id ON event_close (event_id);

-- 1초마다 도는 대여 만료(JobLeaseService.expireDue). 끝난 대여는 쌓이기만 하므로 ACTIVE 만 담는다
CREATE INDEX idx_job_lease_active_expires ON job_lease (lease_expires_at) WHERE status = 'ACTIVE';

-- 현장의 최신 구성 묶음(ORDER BY issued_at DESC LIMIT 1)
CREATE INDEX idx_config_bundle_site_issued ON config_bundle (site_id, issued_at DESC);
