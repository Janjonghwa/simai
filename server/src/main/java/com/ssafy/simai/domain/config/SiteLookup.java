package com.ssafy.simai.domain.config;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.util.List;
import java.util.Map;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;

/** MVP 는 현장 하나다(기획서 16장, V2 시드 classroom-1). 구역·작업면·보정·구성 묶음은 그 현장에 붙는다. */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SiteLookup {

    private final NamedParameterJdbcTemplate jdbc;

    public SiteLookup(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public String siteId() {
        List<String> sites = jdbc.queryForList("SELECT site_id FROM site ORDER BY site_id LIMIT 2", Map.of(), String.class);
        if (sites.size() != 1) {
            throw new BusinessException(ErrorCode.Config.SITE_NOT_SINGLE);
        }
        return sites.getFirst();
    }
}
