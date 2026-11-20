"""Tool 단위 테스트. 외부 API를 호출하지 않고 응답을 모의한다.

    python -m unittest discover -s backend/tests -v

실제 API를 사용하는 확인은 backend/check_tools.py가 담당한다. 여기서는 실제로 재현하기 어려운
실패(시간 초과, 해석 불가 응답, 미신청 안내 페이지, 빈 값)가 약속한 실패 응답으로 변환되는지 검증한다.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools import Tool, call_tool, catalog, fail, ok  # noqa: E402
from app.tools import ecos, kepco, law  # noqa: E402

SECRET = "secret-key-123"


def fake_response(status=200, json_body=None, text=None):
    res = mock.Mock()
    res.status_code = status
    res.text = text if text is not None else ""
    if json_body is None:
        res.json.side_effect = ValueError("not json")
    else:
        res.json.return_value = json_body
    return res


CATALOG = [
    {
        "item_code": "307125AA",
        "item_name": "열연강판",
        "parent_name": "열간압연강재",
        "weight": 3.6,
        "first_month": "196501",
        "latest_month": "202609",
    }
]


def ecos_row(month, value):
    return {
        "TIME": month,
        "DATA_VALUE": value,
        "ITEM_NAME1": "열연강판",
        "UNIT_NAME": "2020=100",
    }


class RegistryTest(unittest.TestCase):
    def test_catalog_exposes_only_name_description_parameters(self):
        specs = catalog()
        self.assertEqual(len(specs), 4)
        for s in specs:
            self.assertEqual(set(s), {"name", "description", "parameters"})
            self.assertEqual(s["parameters"]["type"], "object")

    def test_catalog_filters_by_kind(self):
        self.assertEqual(len(catalog("external")), 4)
        self.assertEqual(catalog("local"), [])

    def test_run_never_raises(self):
        def boom(**_):
            raise RuntimeError(SECRET)

        t = Tool("boom", "test", {"type": "object", "properties": {}}, boom)
        r = t.run()
        self.assertEqual(r["error"]["code"], "UPSTREAM_ERROR")
        self.assertNotIn(SECRET, str(r))

    def test_run_rejects_malformed_result(self):
        t = Tool(
            "bad", "test", {"type": "object", "properties": {}}, lambda: {"data": 1}
        )
        self.assertEqual(t.run()["error"]["code"], "UPSTREAM_ERROR")

    def test_type_checks(self):
        self.assertEqual(
            call_tool("search_material_item", {"keyword": 123})["error"]["code"],
            "BAD_ARGUMENT",
        )
        self.assertEqual(
            call_tool("search_material_item", {"keyword": "a", "max_results": True})[
                "error"
            ]["code"],
            "BAD_ARGUMENT",
        )
        self.assertEqual(
            call_tool("search_material_item", {"keyword": "a", "max_results": 99})[
                "error"
            ]["code"],
            "BAD_ARGUMENT",
        )
        self.assertEqual(
            call_tool("search_material_item", ["keyword"])["error"]["code"],
            "BAD_ARGUMENT",
        )

    def test_ok_fail_shape(self):
        self.assertEqual(ok({"a": 1}), {"ok": True, "data": {"a": 1}})
        self.assertEqual(
            fail("X", "m"), {"ok": False, "error": {"code": "X", "message": "m"}}
        )


@mock.patch.dict(os.environ, {"ECOS_OPEN_API": SECRET})
class EcosTest(unittest.TestCase):
    def setUp(self):
        ecos._memory_catalog[ecos.STAT_CODE] = (
            10**12,
            CATALOG,
        )  # 캐시를 채워 목록 호출을 건너뛴다

    def tearDown(self):
        ecos._memory_catalog.clear()

    def test_timeout_is_upstream_error_without_key(self):
        with mock.patch(
            "app.tools.base.requests.get",
            side_effect=requests.Timeout(f"url?key={SECRET}"),
        ):
            r = call_tool(
                "get_material_price_index",
                {"item_code": "307125AA", "base_month": "202512"},
            )
        self.assertEqual(r["error"]["code"], "UPSTREAM_ERROR")
        self.assertNotIn(SECRET, str(r))

    def test_transient_failure_is_retried_once(self):
        responses = [
            requests.ConnectionError("blip"),
            fake_response(
                json_body={"StatisticSearch": {"row": [ecos_row("202512", "130.58")]}}
            ),
            fake_response(status=503, text="busy"),
            fake_response(
                json_body={"StatisticSearch": {"row": [ecos_row("202609", "150.55")]}}
            ),
        ]
        with mock.patch("app.tools.base.requests.get", side_effect=responses) as get:
            r = call_tool(
                "get_material_price_index",
                {"item_code": "307125AA", "base_month": "202512"},
            )
        self.assertTrue(r["ok"])
        self.assertEqual(get.call_count, 4)

    def test_invalid_key_code(self):
        body = {
            "RESULT": {"CODE": "INFO-100", "MESSAGE": "인증키가 유효하지 않습니다."}
        }
        with mock.patch(
            "app.tools.base.requests.get", return_value=fake_response(json_body=body)
        ):
            r = call_tool(
                "get_material_price_index",
                {"item_code": "307125AA", "base_month": "202512"},
            )
        self.assertEqual(r["error"]["code"], "INVALID_KEY")

    def test_empty_value_is_not_filled(self):
        responses = [
            fake_response(
                json_body={"StatisticSearch": {"row": [ecos_row("202512", "")]}}
            ),
            fake_response(
                json_body={"StatisticSearch": {"row": [ecos_row("202609", "150.55")]}}
            ),
        ]
        with mock.patch("app.tools.base.requests.get", side_effect=responses):
            r = call_tool(
                "get_material_price_index",
                {"item_code": "307125AA", "base_month": "202512"},
            )
        self.assertEqual(r["error"]["code"], "NO_INDEX_VALUE")

    def test_change_rate(self):
        responses = [
            fake_response(
                json_body={"StatisticSearch": {"row": [ecos_row("202512", "130.58")]}}
            ),
            fake_response(
                json_body={"StatisticSearch": {"row": [ecos_row("202609", "150.55")]}}
            ),
        ]
        with mock.patch("app.tools.base.requests.get", side_effect=responses):
            r = call_tool(
                "get_material_price_index",
                {
                    "item_code": "307125AA",
                    "base_month": "202512",
                    "current_month": "202701",
                },
            )
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["change_rate_pct"], 15.29)
        self.assertTrue(r["data"]["estimated"])
        self.assertEqual(r["data"]["current"]["month"], "202609")

    def test_search_default_and_no_data(self):
        r = call_tool("search_material_item", {"keyword": "열연 강판"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["as_of"], "202609")
        self.assertFalse(r["data"]["estimated"])
        self.assertEqual(
            call_tool("search_material_item", {"keyword": "없는품목"})["error"]["code"],
            "NO_DATA",
        )

    def test_non_json_response(self):
        with mock.patch(
            "app.tools.base.requests.get", return_value=fake_response(text="<html>")
        ):
            r = call_tool(
                "get_material_price_index",
                {"item_code": "307125AA", "base_month": "202512"},
            )
        self.assertEqual(r["error"]["code"], "UPSTREAM_ERROR")


@mock.patch.dict(os.environ, {"LAW_API_OC": SECRET})
class LawTest(unittest.TestCase):
    def tearDown(self):
        law._law_body_cache.clear()

    def test_unregistered_html_is_invalid_key(self):
        html = (
            "<!DOCTYPE html><html><h2>미신청된 목록/본문에 대한 접근입니다.</h2></html>"
        )
        with mock.patch(
            "app.tools.base.requests.get", return_value=fake_response(text=html)
        ):
            r = call_tool(
                "search_law_article", {"law_name": "상생협력법", "keyword": "연동"}
            )
        self.assertEqual(r["error"]["code"], "INVALID_KEY")
        self.assertNotIn(SECRET, str(r))

    def test_search_and_no_result(self):
        # 검색 결과가 1건이면 law가 배열이 아니라 객체로 온다
        search = {
            "LawSearch": {
                "totalCnt": "1",
                "law": {
                    "법령일련번호": "290193",
                    "현행연혁코드": "현행",
                    "법령명한글": "대ㆍ중소기업 상생협력 촉진에 관한 법률",
                    "법령약칭명": "상생협력법",
                    "법령ID": "010165",
                    "시행일자": "20260929",
                },
            }
        }
        # 항, 호도 1개면 객체, 여럿이면 배열. 항이 조문내용보다 앞에 와도 조문내용을 먼저 모은다
        body = {
            "법령": {
                "조문": {
                    "조문단위": [
                        {"조문번호": "1", "조문여부": "전문", "조문내용": "제1장 총칙"},
                        {
                            "조문번호": "2",
                            "조문여부": "조문",
                            "조문제목": "정의",
                            "항": {
                                "호": [
                                    {"호내용": '1. "중소기업"이란 ...'},
                                    {
                                        "호내용": '12. "주요 원재료"란 ... 100분의 10 이상인 원재료',
                                        "목": {"목내용": "가. 예시"},
                                    },
                                ]
                            },
                            "조문내용": "제2조(정의)",
                        },
                        {
                            "조문번호": "22",
                            "조문가지번호": "2",
                            "조문여부": "조문",
                            "항": [
                                {"항내용": "① 납품대금 연동 ..."},
                                {"항내용": "② ..."},
                            ],
                            "조문내용": "제22조의2(납품대금 연동)",
                        },
                    ]
                }
            }
        }
        with mock.patch(
            "app.tools.base.requests.get",
            side_effect=[
                fake_response(json_body=search),
                fake_response(json_body=body),
            ],
        ):
            r = call_tool(
                "search_law_article",
                {"law_name": "상생협력법", "keyword": "주요 원재료"},
            )
        self.assertTrue(r["ok"])
        a = r["data"]["articles"][0]
        self.assertEqual(a["article_no"], "제2조")
        self.assertIn("주요 원재료", a["excerpt"])
        self.assertNotIn("제1장", str(r))  # 장 제목(전문)은 제외하고 검색한다
        self.assertIn("/법령/대ㆍ중소기업상생협력촉진에관한법률/제2조", a["link"])
        self.assertNotIn(SECRET, str(r))
        # 같은 법령은 캐시에서 읽으므로 검색 API는 1회만 추가로 호출한다
        with mock.patch(
            "app.tools.base.requests.get", side_effect=[fake_response(json_body=search)]
        ):
            r = call_tool(
                "search_law_article", {"law_name": "상생협력법", "keyword": "연동"}
            )
            self.assertEqual(r["data"]["articles"][0]["article_no"], "제22조의2")
        with mock.patch(
            "app.tools.base.requests.get", side_effect=[fake_response(json_body=search)]
        ):
            r = call_tool(
                "search_law_article", {"law_name": "상생협력법", "keyword": "우주"}
            )
        self.assertEqual(r["error"]["code"], "NO_DATA")

    def test_empty_search_is_not_found(self):
        empty = {"LawSearch": {"totalCnt": "0", "resultCode": "00"}}
        with mock.patch(
            "app.tools.base.requests.get", return_value=fake_response(json_body=empty)
        ):
            r = call_tool(
                "search_law_article", {"law_name": "없는법", "keyword": "연동"}
            )
        self.assertEqual(r["error"]["code"], "NOT_FOUND")

    def test_missing_oc_result_is_invalid_key(self):
        body = {
            "result": "필수입력요소 검증에 실패하였습니다.",
            "msg": "필수 입력값이 존재하지 않습니다.",
        }
        with mock.patch(
            "app.tools.base.requests.get", return_value=fake_response(json_body=body)
        ):
            r = call_tool(
                "search_law_article", {"law_name": "상생협력법", "keyword": "연동"}
            )
        self.assertEqual(r["error"]["code"], "INVALID_KEY")


@mock.patch.dict(os.environ, {"KEP_CO_API": SECRET})
class KepcoTest(unittest.TestCase):
    def tearDown(self):
        kepco._month_cache.clear()

    @mock.patch("app.tools.kepco.time.sleep")
    def test_spurious_401_is_retried(self, _sleep):
        unauthorized = fake_response(
            401, {"errCd": "401", "errMsg": "InvalidApiKeyException"}
        )
        good = fake_response(
            200,
            {
                "data": [
                    {
                        "city": "달서구",
                        "bill": "1000",
                        "powerUsage": "10",
                        "custCnt": "1",
                    }
                ]
            },
        )
        with mock.patch(
            "app.tools.base.requests.get", side_effect=[unauthorized, good, good]
        ):
            r = call_tool(
                "get_industrial_power_price",
                {"base_month": "202512", "current_month": "202512"},
            )
        self.assertTrue(r["ok"])

    @mock.patch("app.tools.kepco.time.sleep")
    def test_persistent_401_is_invalid_key(self, _sleep):
        unauthorized = fake_response(
            401, {"errCd": "401", "errMsg": "InvalidApiKeyException"}
        )
        with mock.patch(
            "app.tools.base.requests.get", return_value=unauthorized
        ) as get:
            r = call_tool("get_industrial_power_price", {"base_month": "202512"})
        self.assertEqual(r["error"]["code"], "INVALID_KEY")
        self.assertEqual(get.call_count, kepco.AUTH_RETRIES + 1)

    def test_no_recent_data(self):
        with mock.patch(
            "app.tools.base.requests.get",
            return_value=fake_response(404, {"errCd": "404", "errMsg": "NotFound"}),
        ):
            r = call_tool("get_industrial_power_price", {"base_month": "202512"})
        self.assertEqual(r["error"]["code"], "NO_DATA")

    def test_empty_bill_is_not_filled(self):
        rows = [{"city": "달서구", "bill": "", "powerUsage": "100", "custCnt": "1"}]
        with mock.patch(
            "app.tools.base.requests.get",
            return_value=fake_response(200, {"data": rows}),
        ):
            r = call_tool(
                "get_industrial_power_price",
                {"base_month": "202512", "current_month": "202512"},
            )
        self.assertEqual(r["error"]["code"], "NO_INDEX_VALUE")

    def test_unit_cost(self):
        def fake_get(url, params, timeout):
            bill = "2000" if params["month"] == "09" else "1000"
            return fake_response(
                200,
                {
                    "data": [
                        {
                            "city": "달서구",
                            "bill": bill,
                            "powerUsage": "10",
                            "custCnt": "3",
                        }
                    ]
                },
            )

        with mock.patch("app.tools.base.requests.get", side_effect=fake_get):
            r = call_tool(
                "get_industrial_power_price",
                {"base_month": "202512", "current_month": "202609"},
            )
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["base"]["unit_cost_won_per_kwh"], 100.0)
        self.assertEqual(r["data"]["change_rate_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
