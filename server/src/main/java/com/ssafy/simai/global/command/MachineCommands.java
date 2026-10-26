package com.ssafy.simai.global.command;

import com.ssafy.simai.domain.machine.MachineKind;
import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.machine.MachineTokenService.IssuedToken;
import java.util.Arrays;
import java.util.List;
import java.util.Set;
import org.springframework.stereotype.Component;

/**
 * 기기·워커 토큰 관리 명령(S15P31A101-222, 명세 14·12.5). 토큰 원문은 여기서 한 번만 출력한다. 그 주체의 비밀 환경 변수에 넣고 따로 두지 않는다.
 * <pre>
 * admin device register --id laptop-01 --kind LAPTOP
 * admin device rotate --id laptop-01
 * admin device revoke --id laptop-01
 * admin worker register --id worker-01 --job-types vlm_rationale,tbm_report
 * admin worker rotate --id worker-01
 * admin worker revoke --id worker-01
 * </pre>
 */
final class MachineCommands {

    private MachineCommands() {
    }

    @Component
    static class DeviceCommand implements AdminCommand {

        private final MachineTokenService tokens;

        DeviceCommand(MachineTokenService tokens) {
            this.tokens = tokens;
        }

        @Override
        public String name() {
            return "device";
        }

        @Override
        public String usage() {
            return "admin device register --id <기기 ID> --kind LAPTOP|WATCH | rotate --id <기기 ID> | revoke --id <기기 ID>";
        }

        @Override
        public int run(List<String> args, CommandIo io) {
            String action = action(args);
            List<String> rest = args.subList(1, args.size());
            return switch (action) {
                case "register" -> {
                    CommandOptions options = CommandOptions.parse(rest, Set.of("id", "kind"));
                    yield printIssued(io, tokens.registerDevice(options.require("id"), options.require("kind")), "등록");
                }
                case "rotate" -> printIssued(io, tokens.rotate(MachineKind.DEVICE, id(rest)), "토큰을 바꿈");
                case "revoke" -> printRevoked(io, tokens.revoke(MachineKind.DEVICE, id(rest)));
                default -> throw new CommandUsageException(usage());
            };
        }
    }

    @Component
    static class WorkerCommand implements AdminCommand {

        private final MachineTokenService tokens;

        WorkerCommand(MachineTokenService tokens) {
            this.tokens = tokens;
        }

        @Override
        public String name() {
            return "worker";
        }

        @Override
        public String usage() {
            return "admin worker register --id <워커 ID> --job-types <유형,…> | rotate --id <워커 ID> | revoke --id <워커 ID>";
        }

        @Override
        public int run(List<String> args, CommandIo io) {
            String action = action(args);
            List<String> rest = args.subList(1, args.size());
            return switch (action) {
                case "register" -> {
                    CommandOptions options = CommandOptions.parse(rest, Set.of("id", "job-types"));
                    List<String> jobTypes = Arrays.stream(options.require("job-types").split(","))
                            .map(String::strip).filter(s -> !s.isEmpty()).toList();
                    yield printIssued(io, tokens.registerWorker(options.require("id"), jobTypes), "등록");
                }
                case "rotate" -> printIssued(io, tokens.rotate(MachineKind.WORKER, id(rest)), "토큰을 바꿈");
                case "revoke" -> printRevoked(io, tokens.revoke(MachineKind.WORKER, id(rest)));
                default -> throw new CommandUsageException(usage());
            };
        }
    }

    private static String action(List<String> args) {
        if (args.isEmpty()) {
            throw new CommandUsageException("register·rotate·revoke 중 하나를 적는다");
        }
        return args.getFirst();
    }

    private static String id(List<String> rest) {
        return CommandOptions.parse(rest, Set.of("id")).require("id");
    }

    private static int printIssued(CommandIo io, IssuedToken issued, String what) {
        io.out().printf("%s: id=%s%n", what, issued.id());
        // 원문은 이번 한 번만 보여 준다. DB 에는 해시만 있어 다시 볼 수 없다(잃어버리면 rotate)
        io.out().printf("토큰(이번 한 번만 표시): %s%n", issued.token());
        return AdminCommand.OK;
    }

    private static int printRevoked(CommandIo io, boolean revoked) {
        io.out().println(revoked ? "폐기했다. 이 토큰으로는 더 이상 인증할 수 없다" : "이미 폐기돼 있다");
        return AdminCommand.OK;
    }
}
