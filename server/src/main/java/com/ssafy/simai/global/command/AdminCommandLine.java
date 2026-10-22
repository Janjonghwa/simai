package com.ssafy.simai.global.command;

import java.util.Arrays;
import org.springframework.boot.Banner;
import org.springframework.boot.WebApplicationType;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.context.ConfigurableApplicationContext;

/**
 * 첫 인자가 admin 이면 웹 서버 없이 띄워 관리 명령 하나를 실행하고 끝낸다.
 * 평소 기동(인자 없음)에는 아무 일도 하지 않는다.
 */
public final class AdminCommandLine {

    static final String ENTRY = "admin";

    private AdminCommandLine() {
    }

    public static boolean matches(String[] args) {
        return args.length > 0 && ENTRY.equals(args[0]);
    }

    /** 종료 코드를 돌려준다. 명령 인자는 Spring 설정으로 넘기지 않는다(--login 같은 옵션이 속성이 되지 않게). */
    public static int run(Class<?> application, String[] args) {
        try (ConfigurableApplicationContext context = new SpringApplicationBuilder(application)
                .web(WebApplicationType.NONE)
                // 명령에 필요한 빈(저장소·서비스)만 만든다. JWT 키처럼 웹 서버만 쓰는 설정이 없어도 뜬다
                .lazyInitialization(true)
                .bannerMode(Banner.Mode.OFF)
                .logStartupInfo(false)
                .run()) {
            return context.getBean(AdminCommands.class)
                    .execute(Arrays.asList(args).subList(1, args.length), CommandIo.system());
        }
    }
}
