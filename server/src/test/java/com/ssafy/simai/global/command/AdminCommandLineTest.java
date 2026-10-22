package com.ssafy.simai.global.command;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.SimaiApplication;
import org.junit.jupiter.api.Test;

/** 관리 명령은 웹 서버(보안 필터 체인 포함) 없이, JWT 키 없이도 떠야 한다. */
class AdminCommandLineTest {

    @Test
    void admin_으로_시작할_때만_관리_명령이다() {
        assertThat(AdminCommandLine.matches(new String[] {"admin", "create-user"})).isTrue();
        assertThat(AdminCommandLine.matches(new String[] {})).isFalse();
        assertThat(AdminCommandLine.matches(new String[] {"--server.port=8081"})).isFalse();
    }

    @Test
    void 웹_서버와_JWT_키_없이_떠서_종료_코드를_돌려준다() {
        // test 프로필(시험 JWT 키)을 켜지 않는다. 하위 명령이 없으니 사용법을 보이고 끝난다
        int code = AdminCommandLine.run(SimaiApplication.class, new String[] {"admin"});

        assertThat(code).isEqualTo(AdminCommand.USAGE);
    }
}
