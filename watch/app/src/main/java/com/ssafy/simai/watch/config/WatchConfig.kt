package com.ssafy.simai.watch.config

import java.io.ByteArrayOutputStream
import java.net.URI
import java.net.URISyntaxException

/**
 * 워치가 EC2 에 붙을 때 쓰는 주소와 기기 토큰(NFR-12, 기기별 발급).
 *
 * 노트북 게이트웨이(gateway/site_alert heartbeat.py validate_base_url)와 같은 규칙을 쓴다:
 * https·호스트 필수, 계정·query·fragment 불가, 토큰이 주소 어디에도 없어야 한다.
 * 토큰은 Authorization 헤더에만 싣고 주소·로그·예외 문장·toString 에 남기지 않는다.
 */
class WatchConfig private constructor(
    val ec2BaseUrl: String,
    val deviceToken: String,
) {
    val ec2Host: String get() = URI(ec2BaseUrl).host

    override fun toString(): String = "WatchConfig(ec2BaseUrl=$ec2BaseUrl, deviceToken=***)"

    companion object {
        /** 잘못된 값이면 IllegalArgumentException. 예외 문장에는 입력값을 넣지 않는다(주소에 토큰이 섞였을 수 있다). */
        fun of(rawBaseUrl: String, rawToken: String): WatchConfig {
            val token = rawToken.trim()
            require(token.isNotEmpty() && token.none { it.isWhitespace() }) { "기기 토큰이 비었거나 공백이 들어 있다" }
            return WatchConfig(validateBaseUrl(rawBaseUrl.trim(), token), token)
        }

        private fun validateBaseUrl(url: String, token: String): String {
            val uri = try {
                URI(url)
            } catch (e: URISyntaxException) {
                throw IllegalArgumentException("EC2 주소 형식이 잘못됐다")
            }
            require(uri.scheme == "https" && !uri.host.isNullOrEmpty()) { "EC2 주소는 호스트가 있는 https 주소여야 한다" }
            require(uri.rawUserInfo == null) { "EC2 주소에 계정을 넣지 않는다" }
            require(uri.rawQuery == null && uri.rawFragment == null && '?' !in url && '#' !in url) {
                "EC2 주소에 query·fragment 를 넣지 않는다"
            }
            // 퍼센트 인코딩으로 숨긴 토큰도 막는다. 게이트웨이와 같이 주소 원문·unquote·unquote_plus 세 형태에서
            // 토큰 원문·quote 형태를 찾는다
            val texts = listOf(url, unquote(url), unquote(url.replace('+', ' ')))
            val secrets = listOf(token, quote(token))
            require(texts.none { text -> secrets.any { it in text } }) { "EC2 주소에 기기 토큰을 넣지 않는다" }
            return url.trimEnd('/')
        }

        /**
         * Python urllib.parse.unquote 와 같다: 올바른 %XX 만 풀고 깨진 이스케이프는 그대로 둔다.
         * java.net.URLDecoder 는 깨진 이스케이프 하나에 예외를 내고 '+' 를 공백으로 바꿔 숨긴 토큰을 놓친다.
         */
        private fun unquote(s: String): String {
            val bytes = s.toByteArray(Charsets.UTF_8)
            val out = ByteArrayOutputStream(bytes.size)
            var i = 0
            while (i < bytes.size) {
                val hi = if (i + 2 < bytes.size) hexValue(bytes[i + 1]) else -1
                val lo = if (i + 2 < bytes.size) hexValue(bytes[i + 2]) else -1
                if (bytes[i] == '%'.code.toByte() && hi >= 0 && lo >= 0) {
                    out.write(hi * 16 + lo)
                    i += 3
                } else {
                    out.write(bytes[i].toInt())
                    i += 1
                }
            }
            // 잘못된 UTF-8 은 대체 문자로 바뀐다(Python errors="replace" 와 같음)
            return String(out.toByteArray(), Charsets.UTF_8)
        }

        /** Python urllib.parse.quote(s, safe="") 와 같다: 영문·숫자·`_.-~` 밖은 모두 %XX. */
        private fun quote(s: String): String = buildString {
            for (b in s.toByteArray(Charsets.UTF_8)) {
                val c = (b.toInt() and 0xFF).toChar()
                if (c in 'A'..'Z' || c in 'a'..'z' || c in '0'..'9' || c in "_.-~") append(c)
                else append('%').append("%02X".format(b.toInt() and 0xFF))
            }
        }

        private fun hexValue(b: Byte): Int = Character.digit(b.toInt().toChar(), 16)
    }
}
