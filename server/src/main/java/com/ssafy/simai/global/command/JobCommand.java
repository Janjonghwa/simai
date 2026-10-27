package com.ssafy.simai.global.command;

import com.ssafy.simai.domain.job.AnalysisJobService;
import com.ssafy.simai.domain.job.AnalysisJobService.JobSpec;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Set;
import org.springframework.stereotype.Component;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * I5 분석 작업을 손으로 넣고 취소한다(S15P31A101-229). 워커 연동 확인·재처리용이다. 평소 작업은 각 기능(TBM·VLM 근거 등)이 넣는다.
 * 입력은 계약 job_request 의 그 유형 input 형식이어야 한다(등록할 때 검사).
 * <pre>
 * admin job enqueue --type static_reconstruct --input-file input.json [--max-attempts 2] [--timeout-ms 600000]
 * admin job enqueue --type vlm_rationale --input '{"...": ...}'
 * admin job cancel --id job-...
 * </pre>
 */
@Component
class JobCommand implements AdminCommand {

    private static final int DEFAULT_MAX_ATTEMPTS = 2;
    private static final int DEFAULT_TIMEOUT_MS = 600_000;

    private final AnalysisJobService jobs;
    private final ObjectMapper mapper;

    JobCommand(AnalysisJobService jobs, ObjectMapper mapper) {
        this.jobs = jobs;
        this.mapper = mapper;
    }

    @Override
    public String name() {
        return "job";
    }

    @Override
    public String usage() {
        return "admin job enqueue --type <job_type> (--input <JSON> | --input-file <경로>) [--max-attempts N] [--timeout-ms N]"
                + " | cancel --id <job_id>";
    }

    @Override
    public int run(List<String> args, CommandIo io) {
        if (args.isEmpty()) {
            throw new CommandUsageException("enqueue·cancel 중 하나를 적는다");
        }
        List<String> rest = args.subList(1, args.size());
        return switch (args.getFirst()) {
            case "enqueue" -> enqueue(CommandOptions.parse(rest,
                    Set.of("type", "input", "input-file", "max-attempts", "timeout-ms")), io);
            case "cancel" -> {
                String jobId = CommandOptions.parse(rest, Set.of("id")).require("id");
                io.out().println(jobs.cancel(jobId) ? "취소했다: " + jobId : "이미 끝난 작업이다: " + jobId);
                yield OK;
            }
            default -> throw new CommandUsageException(usage());
        };
    }

    private int enqueue(CommandOptions options, CommandIo io) {
        JsonNode input = input(options);
        int maxAttempts = number(options, "max-attempts", DEFAULT_MAX_ATTEMPTS);
        int timeoutMs = number(options, "timeout-ms", DEFAULT_TIMEOUT_MS);
        String jobId = jobs.enqueue(new JobSpec(options.require("type"), input, maxAttempts, timeoutMs, null, null));
        io.out().println("작업을 넣었다: " + jobId);
        return OK;
    }

    private JsonNode input(CommandOptions options) {
        String text;
        if (options.get("input").isPresent() == options.get("input-file").isPresent()) {
            throw new CommandUsageException("--input 과 --input-file 중 하나만 적는다");
        }
        try {
            text = options.get("input").isPresent() ? options.require("input")
                    : Files.readString(Path.of(options.require("input-file")), StandardCharsets.UTF_8);
            return mapper.readTree(text);
        } catch (IOException | JacksonException e) {
            throw new CommandUsageException("입력 JSON 을 읽지 못했다: " + e.getMessage());
        }
    }

    private static int number(CommandOptions options, String key, int defaultValue) {
        try {
            return options.get(key).map(Integer::parseInt).orElse(defaultValue);
        } catch (NumberFormatException e) {
            throw new CommandUsageException("--" + key + " 는 정수다");
        }
    }
}
