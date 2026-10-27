package com.ssafy.simai.global.command;

import com.ssafy.simai.domain.floorplan.FloorPlanService;
import com.ssafy.simai.domain.floorplan.FloorPlanService.Frame;
import com.ssafy.simai.domain.floorplan.FloorPlanService.Registered;
import com.ssafy.simai.global.exception.BusinessException;
import java.time.Clock;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import org.springframework.stereotype.Component;

/**
 * BEV 평면도 입력 프레임 등록(S15P31A101-240). 카메라마다 사람 없는 프레임을 객체 저장소 버킷에 올린 뒤 그 주소를 등록한다.
 * 등록하면 바로 대조해, 현재 보정이 CALIBRATED 면 평면도 작업을 넣는다(아니면 CALIBRATED 가 될 때 넣는다).
 * <pre>
 * admin floorplan frames --frames cam_A=s3://버킷/bev/cam_A.jpg,cam_B=s3://버킷/bev/cam_B.jpg [--source LIVE] [--captured-at ms]
 * admin floorplan reconcile
 * </pre>
 */
@Component
class FloorPlanCommand implements AdminCommand {

    /** 사람이 고른 정지 프레임이라 촬영 시각은 대략값이다. 평면도는 시각을 쓰지 않는다 */
    private static final long UNCERTAINTY_MS = 1000;

    private final FloorPlanService floorPlans;
    private final Clock clock;

    FloorPlanCommand(FloorPlanService floorPlans, Clock clock) {
        this.floorPlans = floorPlans;
        this.clock = clock;
    }

    @Override
    public String name() {
        return "floorplan";
    }

    @Override
    public String usage() {
        return "admin floorplan frames --frames <camera_id>=s3://...[,<camera_id>=s3://...] [--source LIVE|FILE|SIM|DATASET]"
                + " [--captured-at <ms>] | reconcile";
    }

    @Override
    public int run(List<String> args, CommandIo io) {
        if (args.isEmpty()) {
            throw new CommandUsageException("frames·reconcile 중 하나를 적는다");
        }
        List<String> rest = args.subList(1, args.size());
        return switch (args.getFirst()) {
            case "frames" -> frames(CommandOptions.parse(rest, Set.of("frames", "source", "captured-at")), io);
            case "reconcile" -> {
                CommandOptions.parse(rest, Set.of());
                io.out().println(floorPlans.reconcile().map(id -> "평면도 작업을 넣었다: " + id)
                        .orElse("넣을 평면도 작업이 없다(보정이 CALIBRATED 가 아니거나, 프레임이 없거나, 이미 넣었다)"));
                yield OK;
            }
            default -> throw new CommandUsageException(usage());
        };
    }

    private int frames(CommandOptions options, CommandIo io) {
        String source = options.get("source").orElse("LIVE");
        long capturedAt;
        try {
            capturedAt = options.get("captured-at").map(Long::parseLong).orElseGet(clock::millis);
        } catch (NumberFormatException e) {
            throw new CommandUsageException("--captured-at 은 UTC 밀리초 정수다");
        }
        String timestampSource = switch (source) {
            case "FILE", "DATASET" -> "FILE";
            case "SIM" -> "SIM";
            default -> "RECEIVE_ESTIMATED";
        };
        List<Frame> frames = new ArrayList<>();
        for (String item : options.require("frames").split(",")) {
            int eq = item.indexOf('=');
            if (eq <= 0) {
                throw new CommandUsageException("--frames 는 <camera_id>=s3://... 를 쉼표로 잇는다: " + item);
            }
            frames.add(new Frame(item.substring(0, eq).strip(), item.substring(eq + 1).strip(), capturedAt, timestampSource,
                    UNCERTAINTY_MS));
        }
        Registered registered;
        try {
            registered = floorPlans.registerFrames(source, frames);
        } catch (BusinessException e) {
            io.err().println("프레임을 등록하지 못했다: " + e.getErrorCode().message());
            return FAILED;
        }
        io.out().println("프레임 묶음을 등록했다: " + registered.frameSetId() + " (카메라 " + frames.size() + "대)");
        io.out().println(registered.jobId().map(id -> "평면도 작업을 넣었다: " + id)
                .orElse("평면도 작업은 보정이 CALIBRATED 가 되면 넣는다"));
        return OK;
    }
}
