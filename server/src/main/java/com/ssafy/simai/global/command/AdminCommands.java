package com.ssafy.simai.global.command;

import com.ssafy.simai.global.exception.BusinessException;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;
import org.springframework.stereotype.Component;

/** 이름으로 관리 명령을 골라 실행하고 종료 코드를 돌려준다. */
@Component
public class AdminCommands {

    private final Map<String, AdminCommand> commands;

    AdminCommands(List<AdminCommand> commands) {
        this.commands = commands.stream().collect(Collectors.toMap(AdminCommand::name, Function.identity()));
    }

    /** args 는 "admin" 뒤의 인자다. 예: [create-user, --login, admin, --role, ADMIN] */
    public int execute(List<String> args, CommandIo io) {
        AdminCommand command = args.isEmpty() ? null : commands.get(args.getFirst());
        if (command == null) {
            io.err().println("사용법:");
            commands.values().forEach(c -> io.err().println("  " + c.usage()));
            return AdminCommand.USAGE;
        }
        try {
            return command.run(args.subList(1, args.size()), io);
        } catch (CommandUsageException e) {
            io.err().println(e.getMessage());
            io.err().println("사용법: " + command.usage());
            return AdminCommand.USAGE;
        } catch (BusinessException e) {
            io.err().println(e.getErrorCode().message());
            return AdminCommand.FAILED;
        }
    }
}
