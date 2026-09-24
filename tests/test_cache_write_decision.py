# -*- coding: utf-8 -*-
"""POST /cache-write-decision 的行为测试。

覆盖：
- 可缓存：max-age 扣除 Age 得到剩余秒数；s-maxage 优先于 max-age；Age 省略
  按 0 处理；quoted-string 取值（max-age="60"）正确解析；指令名大小写不
  敏感；
- 拒绝：no-store、private（含 private="..." 限定形式）、Vary: *（含与
  字段名混合）、带 Authorization 且响应未显式允许共享（public/s-maxage/
  must-revalidate 均缺席）、no-cache、无新鲜期指令、Age 达到/超过新鲜期；
- Authorization 显式允许共享：public / s-maxage / must-revalidate 任一
  出现即可写入；
- 缓存键：同一输入稳定；URI/区间/ETag/Vary 指定请求头值任一变化都会改变
  键；请求头字段名大小写不影响键；Vary 头缺席与空取值区分；非 Vary 请求
  头不影响键；
- 422：非法 URI（含空白）、end<start、弱/非法/空白 ETag、非法
  Cache-Control（空值、非数字 max-age、未闭合引号、非法指令名）、非法
  Age、非法 Vary、请求头字段名非法/大小写重复/取值含控制字符，错误结构
  为 {"error", "detail": [{"message", "loc"}]}。

直接运行：python -m unittest discover -s tests
"""

from __future__ import annotations

import re
import unittest

from app.main import app

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]

URI = "https://objects.example.com/bucket/blob-1"
ETAG = '"v3-strong"'


@unittest.skipIf(TestClient is None, "需要安装 starlette（随 fastapi 安装）")
class CacheWriteDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    # ----------------------------- 构造工具 -----------------------------

    def post(self, body: dict):
        return self.client.post("/cache-write-decision", json=body)

    def req(self, **over) -> dict:
        body = {
            "uri": URI,
            "start": 0,
            "end": 99,
            "etag": ETAG,
            "cache_control": "max-age=60",
        }
        body.update(over)
        return body

    def err422(self, resp, *, loc: str | None = None, code: str | None = None):
        self.assertEqual(resp.status_code, 422, resp.text)
        payload = resp.json()
        detail = payload["detail"][0]
        self.assertIn("message", detail)
        if loc is not None:
            self.assertEqual(detail["loc"], loc, payload)
        if code is not None:
            self.assertEqual(payload["error"], code, payload)
        return payload

    def deny(self, body: dict, reason: str) -> dict:
        resp = self.post(body)
        self.assertEqual(resp.status_code, 200, resp.text)
        d = resp.json()
        self.assertFalse(d["cacheable"], d)
        self.assertEqual(d["reason"], reason, d)
        self.assertIsNone(d["remaining_seconds"])
        self.assertIsNone(d["cache_key"])
        self.assertEqual(d["vary_headers"], [])
        return d

    # ----------------------------- 可缓存 -----------------------------

    def test_cacheable_max_age_minus_age(self) -> None:
        d = self.post(self.req(cache_control="max-age=60", age="10")).json()
        self.assertTrue(d["cacheable"])
        self.assertEqual(d["reason"], "cacheable")
        self.assertEqual(d["remaining_seconds"], 50)
        self.assertRegex(d["cache_key"], re.compile(r"^[0-9a-f]{64}$"))
        self.assertEqual(d["vary_headers"], [])

    def test_age_absent_defaults_to_zero(self) -> None:
        d = self.post(self.req(cache_control="max-age=60")).json()
        self.assertEqual(d["remaining_seconds"], 60)

    def test_s_maxage_wins_over_max_age(self) -> None:
        d = self.post(
            self.req(cache_control="max-age=10, s-maxage=120", age="20")
        ).json()
        self.assertTrue(d["cacheable"])
        self.assertEqual(d["remaining_seconds"], 100)

    def test_s_maxage_alone(self) -> None:
        d = self.post(self.req(cache_control="s-maxage=30", age="5")).json()
        self.assertEqual(d["remaining_seconds"], 25)

    def test_quoted_delta_seconds(self) -> None:
        # 含引号的指令取值按 HTTP 指令语法解析。
        d = self.post(self.req(cache_control='max-age="60"', age="15")).json()
        self.assertTrue(d["cacheable"])
        self.assertEqual(d["remaining_seconds"], 45)

    def test_comma_inside_quoted_string_does_not_split(self) -> None:
        # 引号内的逗号不是指令分隔符：扩展指令取值 "30, x" 整体解析，
        # max-age 仍然生效。
        d = self.post(
            self.req(cache_control='max-age=60, ext="30, x"', age="10")
        ).json()
        self.assertTrue(d["cacheable"])
        self.assertEqual(d["remaining_seconds"], 50)

    def test_quoted_pair_escape_parsed(self) -> None:
        # private="Set-Cookie\" 经 quoted-pair 反转义后仍是 private 指令。
        self.deny(
            self.req(cache_control='private="a\\"b", max-age=60'),
            "private",
        )

    def test_obs_text_allowed_in_quoted_string(self) -> None:
        # quoted-string 允许 obs-text（0x80-0xFF），不影响其他指令解析。
        d = self.post(
            self.req(cache_control='max-age=60, ext="café"')
        ).json()
        self.assertTrue(d["cacheable"])

    def test_directive_names_case_insensitive(self) -> None:
        d = self.post(self.req(cache_control="MAX-AGE=60", age="10")).json()
        self.assertEqual(d["remaining_seconds"], 50)
        self.deny(self.req(cache_control="No-Store, max-age=60"), "no_store")
        self.deny(self.req(cache_control="PRIVATE, max-age=60"), "private")

    def test_ows_and_empty_members_tolerated(self) -> None:
        d = self.post(
            self.req(cache_control="  max-age=60  ,, public ,")
        ).json()
        self.assertTrue(d["cacheable"])
        self.assertEqual(d["remaining_seconds"], 60)

    def test_remaining_one_second_is_cacheable(self) -> None:
        d = self.post(self.req(cache_control="max-age=61", age="60")).json()
        self.assertTrue(d["cacheable"])
        self.assertEqual(d["remaining_seconds"], 1)

    # ----------------------------- 拒绝条件 -----------------------------

    def test_no_store_denied(self) -> None:
        self.deny(self.req(cache_control="no-store"), "no_store")
        # no-store 优先于其他判定（即使同时给了新鲜期）。
        self.deny(self.req(cache_control="no-store, max-age=60"), "no_store")

    def test_private_denied(self) -> None:
        self.deny(self.req(cache_control="private, max-age=60"), "private")

    def test_private_qualified_denied(self) -> None:
        # 限定字段的 private="..." 同样拒绝共享缓存写入。
        self.deny(
            self.req(cache_control='private="Set-Cookie", max-age=60'),
            "private",
        )

    def test_vary_star_denied(self) -> None:
        self.deny(self.req(vary="*"), "vary_star")
        self.deny(self.req(vary=" * "), "vary_star")
        # "*" 与字段名混合同样按 Vary:* 拒绝。
        self.deny(self.req(vary="Accept-Encoding, *"), "vary_star")

    def test_authorization_without_shared_allowance_denied(self) -> None:
        self.deny(
            self.req(request_headers={"Authorization": "Bearer t"}),
            "authorization_not_shared",
        )
        # 字段名大小写不影响判定。
        self.deny(
            self.req(request_headers={"AUTHORIZATION": "Bearer t"}),
            "authorization_not_shared",
        )
        # 没有显式允许共享的指令（no-transform 等不算）。
        self.deny(
            self.req(
                request_headers={"Authorization": "Bearer t"},
                cache_control="max-age=60, no-transform",
            ),
            "authorization_not_shared",
        )

    def test_authorization_bypassed_by_public(self) -> None:
        d = self.post(
            self.req(
                request_headers={"Authorization": "Bearer t"},
                cache_control="max-age=60, public",
            )
        ).json()
        self.assertTrue(d["cacheable"])

    def test_authorization_bypassed_by_s_maxage(self) -> None:
        d = self.post(
            self.req(
                request_headers={"Authorization": "Bearer t"},
                cache_control="s-maxage=30",
            )
        ).json()
        self.assertTrue(d["cacheable"])
        self.assertEqual(d["remaining_seconds"], 30)

    def test_authorization_bypassed_by_must_revalidate(self) -> None:
        d = self.post(
            self.req(
                request_headers={"Authorization": "Bearer t"},
                cache_control="max-age=60, must-revalidate",
            )
        ).json()
        self.assertTrue(d["cacheable"])

    def test_no_authorization_header_is_fine(self) -> None:
        d = self.post(
            self.req(request_headers={"Accept": "*/*"})
        ).json()
        self.assertTrue(d["cacheable"])

    def test_no_cache_denied(self) -> None:
        self.deny(self.req(cache_control="no-cache"), "no_cache")
        self.deny(self.req(cache_control="no-cache, max-age=60"), "no_cache")

    def test_no_freshness_lifetime_denied(self) -> None:
        # 未携带 Cache-Control。
        body = self.req()
        del body["cache_control"]
        self.deny(body, "no_freshness_lifetime")
        # 携带了 Cache-Control 但没有任何新鲜期指令。
        self.deny(self.req(cache_control="public"), "no_freshness_lifetime")

    def test_expired_denied(self) -> None:
        # Age 达到新鲜期：剩余 0 秒即过期。
        self.deny(
            self.req(cache_control="max-age=30", age="30"), "expired"
        )
        # Age 超过新鲜期。
        self.deny(
            self.req(cache_control="max-age=30", age="31"), "expired"
        )
        self.deny(
            self.req(cache_control="s-maxage=10, max-age=60", age="10"),
            "expired",
        )

    # ----------------------------- 缓存键 -----------------------------

    def test_cache_key_stable_for_same_input(self) -> None:
        body = self.req(
            request_headers={"Accept-Encoding": "gzip"},
            vary="Accept-Encoding",
            age="10",
        )
        k1 = self.post(body).json()["cache_key"]
        k2 = self.post(dict(body)).json()["cache_key"]
        self.assertEqual(k1, k2)

    def test_cache_key_changes_with_uri_range_etag(self) -> None:
        base = self.req()
        k_base = self.post(base).json()["cache_key"]
        for over in (
            {"uri": URI + "-2"},
            {"start": 1},
            {"end": 100},
            {"etag": '"v4-strong"'},
        ):
            other = self.req(**over)
            self.assertNotEqual(
                k_base, self.post(other).json()["cache_key"], over
            )

    def test_cache_key_includes_vary_header_values(self) -> None:
        body = self.req(
            request_headers={"Accept-Encoding": "gzip"},
            vary="Accept-Encoding",
        )
        k_gzip = self.post(body).json()["cache_key"]
        k_br = self.post(
            self.req(
                request_headers={"Accept-Encoding": "br"},
                vary="Accept-Encoding",
            )
        ).json()["cache_key"]
        self.assertNotEqual(k_gzip, k_br)

    def test_cache_key_ignores_non_vary_headers(self) -> None:
        k1 = self.post(
            self.req(request_headers={"User-Agent": "a"}, vary="Accept-Encoding")
        ).json()["cache_key"]
        k2 = self.post(
            self.req(request_headers={"User-Agent": "b"}, vary="Accept-Encoding")
        ).json()["cache_key"]
        self.assertEqual(k1, k2)

    def test_cache_key_header_name_case_insensitive(self) -> None:
        k1 = self.post(
            self.req(
                request_headers={"Accept-Encoding": "gzip"},
                vary="Accept-Encoding",
            )
        ).json()["cache_key"]
        k2 = self.post(
            self.req(
                request_headers={"ACCEPT-ENCODING": "gzip"},
                vary="accept-encoding",
            )
        ).json()["cache_key"]
        self.assertEqual(k1, k2)

    def test_cache_key_vary_order_irrelevant(self) -> None:
        headers = {"Accept-Encoding": "gzip", "Accept-Language": "zh"}
        k1 = self.post(
            self.req(
                request_headers=headers, vary="Accept-Encoding, Accept-Language"
            )
        ).json()["cache_key"]
        k2 = self.post(
            self.req(
                request_headers=headers, vary="Accept-Language, Accept-Encoding"
            )
        ).json()["cache_key"]
        self.assertEqual(k1, k2)

    def test_vary_absent_header_distinct_from_empty(self) -> None:
        d_absent = self.post(
            self.req(request_headers={}, vary="Accept-Encoding")
        ).json()
        d_empty = self.post(
            self.req(request_headers={"Accept-Encoding": ""}, vary="Accept-Encoding")
        ).json()
        self.assertNotEqual(d_absent["cache_key"], d_empty["cache_key"])
        self.assertEqual(
            d_absent["vary_headers"],
            [{"name": "accept-encoding", "value": None}],
        )
        self.assertEqual(
            d_empty["vary_headers"],
            [{"name": "accept-encoding", "value": ""}],
        )

    def test_vary_headers_echoed_sorted_and_normalized(self) -> None:
        d = self.post(
            self.req(
                request_headers={
                    "Accept-Language": " zh ",
                    "Accept-Encoding": "gzip",
                },
                vary="Accept-Encoding, accept-language",
            )
        ).json()
        self.assertEqual(
            d["vary_headers"],
            [
                {"name": "accept-encoding", "value": "gzip"},
                {"name": "accept-language", "value": "zh"},
            ],
        )

    # ----------------------------- 422：URI / 区间 / ETag -----------------------------

    def test_uri_with_whitespace_rejected(self) -> None:
        self.err422(
            self.post(self.req(uri="https://example.com/a b")),
            loc="uri",
            code="invalid_uri",
        )
        self.err422(
            self.post(self.req(uri="https://example.com/a\tb")),
            loc="uri",
            code="invalid_uri",
        )

    def test_end_before_start_rejected(self) -> None:
        self.err422(
            self.post(self.req(start=10, end=9)),
            loc="end",
            code="invalid_range",
        )

    def test_weak_etag_rejected(self) -> None:
        self.err422(
            self.post(self.req(etag='W/"v3-strong"')),
            loc="etag",
            code="weak_etag",
        )

    def test_invalid_etag_rejected(self) -> None:
        self.err422(
            self.post(self.req(etag="v3-strong")),
            loc="etag",
            code="invalid_etag",
        )

    def test_blank_etag_rejected(self) -> None:
        self.err422(
            self.post(self.req(etag="   ")), loc="etag", code="missing_etag"
        )

    # ---------------------- 422：Cache-Control / Age / Vary ----------------------

    def test_blank_cache_control_rejected(self) -> None:
        self.err422(
            self.post(self.req(cache_control="   ")),
            loc="cache_control",
            code="invalid_cache_control",
        )

    def test_non_numeric_max_age_rejected(self) -> None:
        self.err422(
            self.post(self.req(cache_control="max-age=abc")),
            loc="cache_control",
            code="invalid_cache_control",
        )
        self.err422(
            self.post(self.req(cache_control="s-maxage")),
            loc="cache_control",
            code="invalid_cache_control",
        )
        self.err422(
            self.post(self.req(cache_control="max-age=-5")),
            loc="cache_control",
            code="invalid_cache_control",
        )

    def test_unclosed_quoted_string_rejected(self) -> None:
        self.err422(
            self.post(self.req(cache_control='max-age="60')),
            loc="cache_control",
            code="invalid_cache_control",
        )

    def test_invalid_directive_name_rejected(self) -> None:
        self.err422(
            self.post(self.req(cache_control="max age=60")),
            loc="cache_control",
            code="invalid_cache_control",
        )

    def test_cache_control_crlf_rejected(self) -> None:
        self.err422(
            self.post(self.req(cache_control="max-age=60\r\nX-Evil: 1")),
            loc="cache_control",
            code="invalid_cache_control",
        )

    def test_invalid_age_rejected(self) -> None:
        for bad in ("abc", "-1", "1.5", "", "  "):
            self.err422(
                self.post(self.req(age=bad)),
                loc="age",
                code="invalid_age",
            )

    def test_blank_vary_rejected(self) -> None:
        self.err422(
            self.post(self.req(vary="  ")), loc="vary", code="invalid_vary"
        )

    def test_invalid_vary_token_rejected(self) -> None:
        self.err422(
            self.post(self.req(vary="Accept Encoding")),
            loc="vary",
            code="invalid_vary",
        )

    # ------------------------- 422：request_headers -------------------------

    def test_invalid_header_name_rejected(self) -> None:
        self.err422(
            self.post(self.req(request_headers={"Bad Name": "x"})),
            loc="request_headers",
            code="invalid_header_name",
        )

    def test_duplicate_header_name_case_insensitive_rejected(self) -> None:
        self.err422(
            self.post(
                self.req(
                    request_headers={"Authorization": "a", "authorization": "b"}
                )
            ),
            loc="request_headers",
            code="duplicate_header_name",
        )

    def test_header_value_control_chars_rejected(self) -> None:
        self.err422(
            self.post(self.req(request_headers={"X-Test": "a\r\nb"})),
            loc="request_headers",
            code="invalid_header_value",
        )

    def test_too_many_headers_rejected(self) -> None:
        headers = {f"X-H{i}": "v" for i in range(101)}
        self.err422(
            self.post(self.req(request_headers=headers)),
            loc="request_headers",
            code="too_many_headers",
        )

    # ------------------------- 字段级 422（pydantic） -------------------------

    def test_missing_required_fields(self) -> None:
        resp = self.post({"uri": URI})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    def test_non_string_header_value_rejected(self) -> None:
        resp = self.post(self.req(request_headers={"X-N": 1}))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")

    def test_non_integer_range_rejected(self) -> None:
        resp = self.post(self.req(start=1.5))
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
