package com.ssafy.simai.global.command;

import com.ssafy.simai.domain.gpu.GpuSessionService;
import com.ssafy.simai.domain.gpu.GpuSessionService.BootRevoked;
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
 * admin device reactivate --id laptop-01   (폐기한 기기를 새 토큰으로 되살림. rotate 는 폐기된 기기를 거부한다)
 * admin worker register --id worker-01 --job-types vlm_rationale,tbm_report
 * admin worker rotate --id worker-01
 * admin worker revoke --id worker-01
 * admin worker reactivate --id worker-01
 * admin gpu-boot register --id gpu-boot-runpod --worker-id worker-gpu-01   (GPU 부팅 비밀값, S15P31A101-699. 워커는 먼저 등록)
 * admin gpu-boot rotate --id gpu-boot-runpod
 * admin gpu-boot revoke --id gpu-boot-runpod       (그 비밀값으로 연 GPU 세션도 끝내고 세션 토큰을 폐기)
 * admin gpu-boot reactivate --id gpu-boot-runpod
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
            return "admin device register --id <기기 ID> --kind LAPTOP|WATCH | rotate --id <기기 ID> | revoke --id <기기 ID> | reactivate --id <기기 ID>";
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
                case "reactivate" -> printIssued(io, tokens.reactivate(MachineKind.DEVICE, id(rest)), "되살림");
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
            return "admin worker register --id <워커 ID> --job-types <유형,…> | rotate --id <워커 ID> | revoke --id <워커 ID> | reactivate --id <워커 ID>";
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
                case "reactivate" -> printIssued(io, tokens.reactivate(MachineKind.WORKER, id(rest)), "되살림");
                default -> throw new CommandUsageException(usage());
            };
        }
    }

    /**
     * GPU 부팅 비밀값(S15P31A101-699). 원문은 공급자 비밀 저장소(RunPod Secrets·AWS Secrets Manager)에만 넣는다.
     * 이 비밀값으로는 GPU 세션 등록·진행·종료 보고만 할 수 있고, 등록하면 묶인 워커의 토큰과 Tier 0 토큰이 새로 발급된다.
     */
    @Component
    static class GpuBootCommand implements AdminCommand {

        private final MachineTokenService tokens;
        private final GpuSessionService sessions;

        GpuBootCommand(MachineTokenService tokens, GpuSessionService sessions) {
            this.tokens = tokens;
            this.sessions = sessions;
        }

        @Override
        public String name() {
            return "gpu-boot";
        }

        @Override
        public String usage() {
            return "admin gpu-boot register --id <비밀값 ID> --worker-id <워커 ID> | rotate --id <비밀값 ID> | revoke --id <비밀값 ID>"
                    + " | reactivate --id <비밀값 ID>";
        }

        @Override
        public int run(List<String> args, CommandIo io) {
            String action = action(args);
            List<String> rest = args.subList(1, args.size());
            return switch (action) {
                case "register" -> {
                    CommandOptions options = CommandOptions.parse(rest, Set.of("id", "worker-id"));
                    yield printIssued(io, tokens.registerGpuBoot(options.require("id"), options.require("worker-id")), "등록");
                }
                case "rotate" -> printIssued(io, tokens.rotate(MachineKind.GPU_BOOT, id(rest)), "비밀값을 바꿈");
                case "revoke" -> {
                    BootRevoked revoked = sessions.revokeBoot(id(rest));
                    printRevoked(io, revoked.revoked());
                    io.out().println(revoked.endedSessions().isEmpty() ? "끝낸 GPU 세션 없음"
                            : "끝낸 GPU 세션(토큰 폐기): " + revoked.endedSessions());
                    yield AdminCommand.OK;
                }
                case "reactivate" -> printIssued(io, tokens.reactivate(MachineKind.GPU_BOOT, id(rest)), "되살림");
                default -> throw new CommandUsageException(usage());
            };
        }
    }

    private static String action(List<String> args) {
        if (args.isEmpty()) {
            throw new CommandUsageException("register·rotate·revoke·reactivate 중 하나를 적는다");
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
