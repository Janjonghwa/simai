package com.ssafy.simai.domain.layout;
import static org.assertj.core.api.Assertions.assertThat;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.json.JsonMapper;
class LayoutZonePlansTest {
    @Test void 기존_작업면_참조는_새_구역을_만들지_않는다() {
        var items = JsonMapper.builder().build().readTree("""
            [{"item_id":"ref","kind":"WORK_SURFACE","source_zone_id":"z-existing",
              "source_object_id":"ws-existing","center":[0,0,0.5],"size":[2,1,1],"yaw_rad":0},
             {"item_id":"new","kind":"WORK_SURFACE","center":[0,0,0.5],"size":[2,1,1],"yaw_rad":0}]
            """);
        assertThat(LayoutZonePlans.from(items)).extracting(LayoutZonePlans.Plan::itemId).containsExactly("new");
    }
    @Test void 계획_개구부와_작업면을_변환한다() {
        var items = JsonMapper.builder().build().readTree("""
            [{"item_id":"old","kind":"OPENING","source_zone_id":"z-old","center":[0,0,0],"size":[1,1,1],"yaw_rad":0},
             {"item_id":"open","kind":"OPENING","center":[2,3,0.05],"size":[2,1,0.1],"yaw_rad":0},
             {"item_id":"surface","kind":"WORK_SURFACE","center":[0,0,0.5],"size":[2,1,1],"yaw_rad":0}]
            """);
        var plans = LayoutZonePlans.from(items);
        assertThat(plans).hasSize(2);
        assertThat(plans.get(0).zone().name()).isEqualTo("개구부 2");
        assertThat(plans.get(0).zone().polygonXy()).containsExactly(
            java.util.List.of(0.7, 2.2), java.util.List.of(3.3, 2.2),
            java.util.List.of(3.3, 3.8), java.util.List.of(0.7, 3.8));
        assertThat(plans.get(0).zone().zMaxM()).isEqualTo(2.0);
        assertThat(plans.get(1).surface().topHeightM()).isEqualTo(1.0);
    }
}
