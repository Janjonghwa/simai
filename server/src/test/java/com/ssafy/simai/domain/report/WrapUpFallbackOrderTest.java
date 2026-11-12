package com.ssafy.simai.domain.report;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 결정적 대체 마감 문장은 실행마다 같아야 한다. 시간대는 초반 → 중반 → 후반 순서다(S15P31A101-632, -655 형식). */
class WrapUpFallbackOrderTest {

    @Test
    void 단계는_초반_중반_후반_순서로_나온다() {
        ObjectNode facts = new ObjectMapper().createObjectNode().put("event_count", 3);
        facts.putObject("phase_counts").put("early", 1).put("middle", 1).put("late", 1);
        String text = TbmReportService.wrapUpFallback(facts);
        int early = text.indexOf("초반 1건");
        int middle = text.indexOf("중반 1건");
        int late = text.indexOf("후반 1건");
        assertThat(early).isNotNegative();
        assertThat(early).isLessThan(middle);
        assertThat(middle).isLessThan(late);
        assertThat(TbmReportService.wrapUpFallback(facts)).isEqualTo(text);
    }

    @Test
    void 경보가_없으면_짧게_끝내고_0건_항목은_나열하지_않는다() {
        ObjectNode facts = new ObjectMapper().createObjectNode().put("event_count", 0);
        assertThat(TbmReportService.wrapUpFallback(facts)).isEqualTo("작업 종료 미팅. 경보: 없음. 특이사항 없이 작업을 마쳤다.");
    }

    @Test
    void 이름표_붙은_짧은_문장으로_0건은_빼고_등급은_한국어다() {
        ObjectNode facts = new ObjectMapper().createObjectNode().put("event_count", 3).put("max_severity", "CRITICAL")
                .put("operator_ack_count", 1).put("unacknowledged_critical_count", 0).put("outside_work_entry_count", 0);
        facts.putObject("severity_counts").put("CRITICAL", 2).put("HIGH", 0).put("CAUTION", 1).put("INFO", 0);
        facts.putObject("rule_counts").put("R1", 1).put("R4", 2);
        facts.putArray("top_zones").addObject().put("name", "개구부 1").put("count", 2);
        facts.putObject("phase_counts").put("early", 0).put("middle", 3).put("late", 0);
        String text = TbmReportService.wrapUpFallback(facts);
        assertThat(text).isEqualTo("작업 종료 미팅. 경보: 총 3건(위험 2건, 주의 1건), 최고 등급 위험. 주요 위험: 낙상 의심 2건, 위험 구역 진입 1건. "
                + "많이 발생한 구역: 개구부 1 2건. 조치: 운영자 확인 1건, 미확인 위험 경보 없음. 시간대: 중반 3건. "
                + "다음 작업 반영: 개구부 1 점검과 낙상 의심 예방 대책을 작업 전 TBM에 반영한다.");
        assertThat(text).doesNotContain("0건");
    }
}
