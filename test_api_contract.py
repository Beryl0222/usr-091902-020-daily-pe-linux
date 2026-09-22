"""端到端接口契约测试：从主数据到家长视图走真实 HTTP。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from service import build_handler
from test_pe_domain import (
    A_admin,
    A_researcher,
    A_teacher,
    CLASS,
    SCHOOL,
    TEACHER,
    V_FIELD,
    V_GYM,
    WEEK_START,
    build_world,
    class_activities,
    complete_activity,
    standard_plan_items,
)
from pe_domain import EVENT_CONTENT, EVENT_END, EVENT_SAMPLE, EVENT_START, EVENT_TEACHER_SIGN, EVENT_VENUE, Ledger


def make_server():
    ledger = Ledger()
    handler = build_handler(ledger)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}", ledger


class ApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.thread, cls.base_url, cls.ledger = make_server()
        build_world(cls.ledger)
        cls.ledger.submit_plan(A_admin, SCHOOL, [CLASS], WEEK_START, 2, standard_plan_items())
        cls.pe = class_activities(cls.ledger, atype="体育课")[0]
        cls.sample = [f"{CLASS}-s{i:02d}" for i in range(1, 7)]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    # -- HTTP 工具 ---------------------------------------------------------

    def post(self, path, payload):
        req = Request(f"{self.base_url}{path}", method="POST",
                      data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json; charset=utf-8"})
        with urlopen(req, timeout=3) as response:
            return response.status, json.load(response)

    def get(self, path, **params):
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urlencode(params)
        with urlopen(url, timeout=3) as response:
            return response.status, json.load(response)

    def get_with_headers(self, path, headers, **params):
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urlencode(params)
        req = Request(url, headers=headers)
        with urlopen(req, timeout=3) as response:
            return response.status, json.load(response)

    def assert_http_error(self, status, fn):
        try:
            fn()
        except HTTPError as error:
            self.assertEqual(error.code, status)
            return json.load(error)
        self.fail(f"应返回 {status}")

    # -- 契约 ---------------------------------------------------------------

    def test_health_contract_unchanged(self):
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "service": "daily-pe", "name": "学校体育实课运行"})

    def test_event_chain_over_http_completes_activity(self):
        day, slot = self.pe["date"], self.pe["slot"]
        from test_pe_domain import day_minutes

        def event(kind, at, payload=None, **extra):
            body = {"actor": A_teacher, "activity_id": self.pe["activity_id"],
                    "event_type": kind, "occurred_at": at, "payload": payload or {}, **extra}
            return self.post("/api/events", body)

        status, body = event(EVENT_VENUE, day_minutes(day, slot, -5), {"usable": True})
        self.assertEqual(status, 200)
        event(EVENT_TEACHER_SIGN, day_minutes(day, slot, -4))
        event(EVENT_SAMPLE, day_minutes(day, slot, -2), {"student_ids": self.sample})
        event(EVENT_START, day_minutes(day, slot, 0))
        event(EVENT_CONTENT, day_minutes(day, slot, 10),
              {"mode": "技能教学", "skills": ["篮球"], "minutes": 40, "phase": "新授"})
        status, body = event(EVENT_END, day_minutes(day, slot, 40))
        self.assertEqual(body["activity_status"], "已完成")

    def test_duplicate_device_ref_rejected_with_409(self):
        from test_pe_domain import day_minutes
        another = class_activities(self.ledger, atype="体育课")[1]
        payload = {"actor": A_teacher, "activity_id": another["activity_id"],
                   "event_type": EVENT_VENUE,
                   "occurred_at": day_minutes(another["date"], another["slot"], -5),
                   "payload": {"usable": True}, "client_ref": "gate-X"}
        self.post("/api/events", payload)
        error = self.assert_http_error(409, lambda: self.post("/api/events", payload))
        self.assertEqual(error["error"], "Conflict")

    def test_validation_error_returns_422(self):
        # pe[0] 已被前序测试完成，取尚未完成的 pe[1]：占课先校验依据，返回 422
        unfinished = class_activities(self.ledger, atype="体育课")[1]
        error = self.assert_http_error(422, lambda: self.post("/api/occupy", {
            "actor": A_teacher, "activity_id": unfinished["activity_id"],
            "basis": {"reason": "口头通知"}}))
        self.assertIn("依据", error["message"])

    def test_role_forbidden_returns_403(self):
        self.assert_http_error(403, lambda: self.get(
            "/api/reconstruct", class_id=CLASS))  # 无身份
        error = self.assert_http_error(403, lambda: self.post("/api/plans", {
            "actor": A_teacher, "school_id": SCHOOL, "class_ids": [CLASS],
            "week_start": WEEK_START, "weeks": 1, "items": standard_plan_items()}))
        self.assertIn("学校管理员", error["message"])

    def test_anomaly_to_review_flow(self):
        # 另一所场景放在独立账本：占课 → 异常清单 → 教研员裁定
        server, thread, url, ledger = make_server()
        try:
            build_world(ledger)
            ledger.submit_plan(A_admin, SCHOOL, [CLASS], WEEK_START, 1, standard_plan_items())
            target = class_activities(ledger, atype="体育课")[2]

            def post(path, payload):
                req = Request(f"{url}{path}", method="POST",
                              data=json.dumps(payload, ensure_ascii=False).encode(),
                              headers={"Content-Type": "application/json"})
                with urlopen(req, timeout=3) as response:
                    return json.load(response)

            post("/api/occupy", {"actor": A_admin, "activity_id": target["activity_id"],
                                 "basis": {"notice_id": "N-1", "source": "教务处", "reason": "占用"}})
            req = Request(
                f"{url}/api/anomaly-roster?school_id={SCHOOL}&as_of=2026-09-09",
                headers={"X-Actor-Role": "researcher"})
            with urlopen(req, timeout=3) as response:
                roster = json.load(response)
            item = next(a for c in roster["classes"] for a in c["items"]
                        if a["kind"] == "阴阳课表·实课被占")
            result = post("/api/reviews/resolve",
                          {"actor": A_researcher, "class_id": CLASS, "key": item["key"],
                           "decision": "已确认", "note": "通知属实"})
            self.assertEqual(result["status"], "已确认")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_public_and_parent_views(self):
        # 完成另一节体育课，让聚合有数据
        another = class_activities(self.ledger, atype="体育课")[2]
        complete_activity(self.ledger, another, self.sample)
        # 伤病适配 + 家长令牌
        self.post("/api/adaptations", {
            "actor": A_teacher, "student_id": f"{CLASS}-s03", "adaptation": "降低强度",
            "basis": {"medical_id": "M-1", "source": "社区医院"},
            "date_from": "2026-09-07", "date_to": "2026-09-30"})
        _, token_body = self.post("/api/parent-token",
                                  {"actor": A_admin, "student_id": f"{CLASS}-s03"})
        _, public = self.get("/api/public/school-summary", school_id=SCHOOL)
        self.assertNotIn("classes", {k.lower() for k in public})  # 无班级/个人明细
        self.assertGreaterEqual(public["pe_sessions_completed"], 2)
        _, parent = self.get("/api/parent", token=token_body["token"])
        self.assertEqual(parent["adaptations"][0]["adaptation"], "降低强度")
        self.assertNotIn(f"{CLASS}-s01", json.dumps(parent, ensure_ascii=False))
        # 坏令牌
        self.assert_http_error(403, lambda: self.get("/api/parent", token="bad"))

    def test_no_body_is_bad_request_shape(self):
        req = Request(f"{self.base_url}/api/events", method="POST", data=b"",
                      headers={"Content-Type": "application/json"})
        try:
            urlopen(req, timeout=3)
        except HTTPError as error:
            self.assertEqual(error.code, 422)
            return
        self.fail("空请求体应被拒绝")

    def test_unknown_route_404(self):
        self.assert_http_error(404, lambda: self.get("/api/nope"))


if __name__ == "__main__":
    unittest.main()
