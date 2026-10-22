package com.ssafy.simai.domain.user.service;

import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.domain.user.repository.AppUserRepository;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.security.PasswordPolicy;
import java.time.Clock;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** 계정 생성은 관리 명령(admin create-user)으로만 한다. 계정을 만드는 API 는 두지 않는다. */
@Service
public class UserService {

    private final AppUserRepository users;
    private final PasswordEncoder passwordEncoder;
    private final Clock clock;

    UserService(AppUserRepository users, PasswordEncoder passwordEncoder, Clock clock) {
        this.users = users;
        this.passwordEncoder = passwordEncoder;
        this.clock = clock;
    }

    @Transactional
    public AppUser create(String loginId, String rawPassword, Role role, String displayLabel) {
        if (PasswordPolicy.tooShort(rawPassword)) {
            throw new BusinessException(ErrorCode.User.WEAK_PASSWORD);
        }
        if (PasswordPolicy.tooLong(rawPassword)) {
            throw new BusinessException(ErrorCode.User.PASSWORD_TOO_LONG);
        }
        if (users.existsByLoginId(loginId)) {
            throw new BusinessException(ErrorCode.User.DUPLICATE_LOGIN_ID);
        }
        try {
            // flush 해서 login_id UNIQUE 위반을 여기서 받는다(위 확인과 저장 사이에 같은 아이디가 먼저 들어온 경우)
            return users.saveAndFlush(
                    new AppUser(loginId, passwordEncoder.encode(rawPassword), role, displayLabel, clock.instant()));
        } catch (DataIntegrityViolationException e) {
            throw new BusinessException(ErrorCode.User.DUPLICATE_LOGIN_ID);
        }
    }
}
