package com.ssafy.simai.global.command;

import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.domain.user.service.UserService;
import java.util.List;
import java.util.Set;
import org.springframework.stereotype.Component;

/**
 * admin create-user --login {아이디} --role {ADMIN|FOREMAN|VIEWER} [--label {표시 이름}]
 * 비밀번호는 두 번 입력받는다.
 */
@Component
class CreateUserCommand implements AdminCommand {

    private final UserService userService;

    CreateUserCommand(UserService userService) {
        this.userService = userService;
    }

    @Override
    public String name() {
        return "create-user";
    }

    @Override
    public String usage() {
        return "admin create-user --login <아이디> --role <ADMIN|FOREMAN|VIEWER> [--label <표시 이름>]";
    }

    @Override
    public int run(List<String> args, CommandIo io) {
        CommandOptions options = CommandOptions.parse(args, Set.of("login", "role", "label"));
        String loginId = options.require("login");
        Role role = parseRole(options.require("role"));

        String password = io.secrets().read("비밀번호: ");
        String confirm = io.secrets().read("비밀번호 확인: ");
        if (password == null || !password.equals(confirm)) {
            io.err().println("비밀번호가 서로 다르다");
            return FAILED;
        }

        AppUser user = userService.create(loginId, password, role, options.get("label").orElse(null));
        io.out().printf("계정을 만들었다: user_id=%d, login=%s, role=%s%n", user.getId(), user.getLoginId(), user.getRole());
        return OK;
    }

    private static Role parseRole(String value) {
        try {
            return Role.valueOf(value);
        } catch (IllegalArgumentException e) {
            throw new CommandUsageException("--role 은 ADMIN·FOREMAN·VIEWER 중 하나: " + value);
        }
    }
}
