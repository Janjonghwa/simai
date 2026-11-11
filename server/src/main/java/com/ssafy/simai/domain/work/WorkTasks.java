package com.ssafy.simai.domain.work;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;
import java.util.Optional;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 날짜 작업 TBM 보고서에서 고르는 작업내용 항목(GET /api/work-tasks, S15P31A101-609).
 * rule_tags 는 공종(WorkTypes)의 rule_tags 처럼 법령 근거를 고르는 데 쓴다. '기타'는 항목이 아니라 보고서의 {text} 다.
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WorkTasks {
    public record WorkTask(String code, String label, @JsonProperty("rule_tags") List<String> ruleTags) { }

    // 태그는 명세 7장 규칙표 기준: 개구부·단부(R1·R2·R5), 고소 작업면(R2·R3·R4), 바닥 통행(R1·R4).
    private static final List<WorkTask> TASKS = List.of(
            new WorkTask("SLAB_OPENING_COVER", "슬래브 개구부 덮개 설치", List.of("R1", "R2", "R5")),
            new WorkTask("WORK_PLATFORM", "고소 작업면(발판) 설치·해체", List.of("R2", "R3", "R4")),
            new WorkTask("FORMWORK", "거푸집 설치·해체", List.of("R2", "R3", "R4")),
            new WorkTask("REBAR", "철근 배근", List.of("R1", "R2", "R4")),
            new WorkTask("MATERIAL_HANDLING", "자재 반입·정리", List.of("R1", "R5")),
            new WorkTask("EXTERIOR_SCAFFOLD_CHECK", "외벽 비계 점검", List.of("R2", "R3", "R4")),
            new WorkTask("FLOOR_FINISHING", "바닥 마감", List.of("R1", "R4")));

    public static List<WorkTask> all() { return TASKS; }

    public static Optional<WorkTask> find(String code) {
        return code == null ? Optional.empty() : TASKS.stream().filter(t -> t.code().equals(code)).findFirst();
    }

    @GetMapping("/api/work-tasks")
    List<WorkTask> list() { return TASKS; }
}
