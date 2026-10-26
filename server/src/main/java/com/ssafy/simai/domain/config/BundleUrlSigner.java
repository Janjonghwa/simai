package com.ssafy.simai.domain.config;

import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.properties.AuthProperties;
import com.ssafy.simai.global.properties.ConfigBundleProperties;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.util.Base64;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;

/**
 * 구성 묶음 파일 주소에 붙이는 서명(sig). Tier 0 는 헤더 없이 https GET 만 하므로(ai/services/tier0/config_fetch.py) 주소 자체가
 * 권한이다. 주소는 매니페스트 안에 들어가 해시가 고정되므로 만료를 두지 않는다(같은 주소는 늘 같은 바이트를 준다).
 * 키는 JWT 서명 키에서 용도 이름으로 따로 뽑는다(새 비밀 값을 늘리지 않는다). JWT 키를 바꾸면 이전 묶음 주소는 더 열리지 않는다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class BundleUrlSigner {

    private static final String ALGORITHM = "HmacSHA256";
    private static final byte[] PURPOSE = "simai/config-bundle-file/v1".getBytes(StandardCharsets.UTF_8);

    private final SecretKeySpec key;
    private final String baseUrl;

    public BundleUrlSigner(AuthProperties auth, ConfigBundleProperties properties) {
        byte[] jwtKey = JwtConfig.decodeKey(auth.jwt().secret()).getEncoded();
        this.key = new SecretKeySpec(hmac(new SecretKeySpec(jwtKey, ALGORITHM), PURPOSE), ALGORITHM);
        this.baseUrl = properties.publicBaseUrl();
    }

    /** Tier 0 는 https 주소만 받는다 */
    public boolean configured() {
        return baseUrl.startsWith("https://");
    }

    public String url(String configVersion, String name) {
        return baseUrl + "/api/config-bundles/" + URLEncoder.encode(configVersion, StandardCharsets.UTF_8)
                + "/files/" + name + "?sig=" + sign(configVersion, name);
    }

    public boolean verify(String configVersion, String name, String sig) {
        if (sig == null) {
            return false;
        }
        return MessageDigest.isEqual(sign(configVersion, name).getBytes(StandardCharsets.US_ASCII),
                sig.getBytes(StandardCharsets.US_ASCII));
    }

    private String sign(String configVersion, String name) {
        byte[] data = (configVersion + "\n" + name).getBytes(StandardCharsets.UTF_8);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(hmac(key, data));
    }

    private static byte[] hmac(SecretKeySpec key, byte[] data) {
        try {
            Mac mac = Mac.getInstance(ALGORITHM);
            mac.init(key);
            return mac.doFinal(data);
        } catch (GeneralSecurityException e) {
            throw new IllegalStateException(e);
        }
    }
}
