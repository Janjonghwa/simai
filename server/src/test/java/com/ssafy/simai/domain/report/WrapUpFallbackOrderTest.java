package com.ssafy.simai.domain.report;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 결정적 대체 마감 문장은 실행마다 같아야 한다. 단계는 초반 → 중반 → 후반 순서다(S15P31A101-632). */
class WrapUpFallbackOrderTest {

    @Test
    void 단계는_초반_중반_후반_순서로_나온다() {
        ObjectNode facts = new ObjectMapper().createObjectNode().put("event_count", 3);
        facts.putObject("phase_counts").put("early", 1).put("middle", 1).put("late", 1);
        String text = TbmReportService.wrapUpFallback(facts);
        int early = text.indexOf("작업 초반");
        int middle = text.indexOf("작업 중반");
        int late = text.indexOf("작업 후반");
        assertThat(early).isNotNegative();
        assertThat(early).isLessThan(middle);
        assertThat(middle).isLessThan(late);
        assertThat(TbmReportService.wrapUpFallback(facts)).isEqualTo(text);
    }
}
