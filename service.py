"""学校体育实课运行的 HTTP 入口。

健康检查保持历史契约；其余 /api/* 路由把 JSON 请求转发给 pe_domain.Ledger。
角色由请求方在 actor 字段（写接口）或 X-Actor-* 请求头（读接口）中声明；
家长接口只认签发的家长令牌，公众接口只返回学校聚合。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from pe_domain import (
    AccessDenied,
    DomainError,
    Ledger,
    ROLE_ADMIN,
    ROLE_PARENT,
    ROLE_RESEARCHER,
    ROLE_TEACHER,
    ValidationError,
)

# HTTP 请求头只能用 latin-1，因此读接口的角色头使用英文代号
ROLE_CODES = {
    "researcher": ROLE_RESEARCHER,
    "admin": ROLE_ADMIN,
    "teacher": ROLE_TEACHER,
    "parent": ROLE_PARENT,
}

SERVICE_ID = "daily-pe"
SERVICE_NAME = "学校体育实课运行"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# 路由表：(方法, 路径前缀) -> 处理函数名
def build_handler(ledger: Ledger | None = None):
    """生成绑定指定账本的 Handler 类；测试可借此隔离数据。"""
    ledger = ledger or Ledger()

    class Handler(BaseHTTPRequestHandler):
        """提供健康检查与实课运行的 JSON 接口。"""

        server_version = "daily-pe/1.0"

        # -- 基础收发 -------------------------------------------------------

        def _send_json(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValidationError(f"请求体不是合法 JSON: {exc}")
            if not isinstance(data, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return data

        def _actor(self, body: dict | None = None) -> dict:
            actor = dict((body or {}).get("actor") or {})
            if not actor:
                role_code = (self.headers.get("X-Actor-Role") or "").lower()
                actor = {
                    "role": ROLE_CODES.get(role_code) or self.headers.get("X-Actor-Role"),
                    "name": self.headers.get("X-Actor-Name"),
                    "teacher_id": self.headers.get("X-Actor-Id"),
                    "actor_id": self.headers.get("X-Actor-Id"),
                }
            return actor

        def _handle_error(self, exc: DomainError):
            self._send_json(exc.status, {"error": type(exc).__name__, "message": str(exc)})

        def _not_found(self):
            self._send_json(404, {"error": "NotFound", "message": "未知路由"})

        # -- GET 路由 -------------------------------------------------------

        def do_GET(self):
            parsed = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                if parsed.path == "/health":
                    self._send_json(200, health_payload())
                    return
                if parsed.path == "/api/plans":
                    self._send_json(200, ledger.get_plan(self._actor(),
                                                         query["class_id"],
                                                         version=int(query["version"])
                                                         if query.get("version") else None))
                    return
                if parsed.path == "/api/coverage":
                    self._send_json(200, ledger.class_coverage(
                        self._actor(), query["class_id"], query.get("as_of")))
                    return
                if parsed.path == "/api/anomalies":
                    self._send_json(200, ledger.scan_anomalies(
                        self._actor(), query["class_id"], query.get("as_of")))
                    return
                if parsed.path == "/api/anomaly-roster":
                    self._send_json(200, ledger.anomaly_roster(
                        self._actor(), query["school_id"], query.get("as_of")))
                    return
                if parsed.path == "/api/reconstruct":
                    self._send_json(200, ledger.reconstruct_class(
                        self._actor(), query["class_id"], query.get("as_of")))
                    return
                if parsed.path == "/api/public/school-summary":
                    # 公众免认证；带身份时按其角色读取，聚合内容相同
                    actor = self._actor()
                    if not actor.get("role"):
                        actor = {"role": "公众", "name": "公众查询"}
                    self._send_json(200, ledger.public_school_summary(
                        actor, query["school_id"], query.get("as_of")))
                    return
                if parsed.path == "/api/parent":
                    token = query.get("token") or self.headers.get("X-Parent-Token")
                    if not token:
                        raise AccessDenied("缺少家长令牌 token")
                    self._send_json(200, ledger.parent_view(token, query.get("as_of")))
                    return
                self._not_found()
            except DomainError as exc:
                self._handle_error(exc)
            except KeyError as exc:
                self._handle_error(ValidationError(f"缺少查询参数: {exc.args[0]}"))

        # -- POST 路由 ------------------------------------------------------

        def do_POST(self):
            parsed = urlparse(self.path)
            try:
                body = self._read_json()
                actor = self._actor(body)
                path = parsed.path

                if path == "/api/master-data":
                    result = ledger.seed_master_data(actor, body.get("data") or body)
                elif path == "/api/parent-token":
                    result = ledger.issue_parent_token(actor, body["student_id"])
                elif path == "/api/weather":
                    result = ledger.record_weather(
                        actor, body["school_id"], body["date"], body["condition"],
                        temp_c=body.get("temp_c"), source=body.get("source", ""))
                elif path == "/api/plans":
                    result = ledger.submit_plan(
                        actor, body["school_id"], body["class_ids"], body["week_start"],
                        body["weeks"], body["items"], note=body.get("note", ""))
                elif path == "/api/activities/sample":
                    result = ledger.suggest_sample(
                        actor, body["activity_id"], count=int(body.get("count", 6)))
                elif path == "/api/events":
                    result = ledger.report_event(
                        actor, body["activity_id"], body["event_type"], body["occurred_at"],
                        payload=body.get("payload"), offline=bool(body.get("offline")),
                        offline_reason=body.get("offline_reason", ""),
                        client_ref=body.get("client_ref", ""))
                elif path == "/api/reschedule":
                    result = ledger.reschedule(
                        actor, body["activity_id"], body["new_date"], body["new_slot"],
                        body["new_venue_id"], body["basis"])
                elif path == "/api/weather-substitution":
                    result = ledger.weather_substitution(
                        actor, body["activity_id"], body["kind"], body["new_slot"],
                        body["new_venue_id"], body["skill"], body["weather_id"],
                        content_name=body.get("content_name", ""))
                elif path == "/api/occupy":
                    result = ledger.occupy(actor, body["activity_id"], body["basis"])
                elif path == "/api/adaptations":
                    result = ledger.student_adaptation(
                        actor, body["student_id"], body["adaptation"], body["basis"],
                        body["date_from"], body["date_to"], note=body.get("note", ""))
                elif path == "/api/makeups":
                    result = ledger.arrange_makeup(
                        actor, body["origin_activity_id"], body["new_date"], body["new_slot"],
                        body["new_venue_id"], body["skill"], basis_note=body.get("basis_note", ""))
                elif path == "/api/observations":
                    result = ledger.record_observation(
                        actor, body["student_id"], body["activity_id"], body["skill"],
                        body["level"], note=body.get("note", ""))
                elif path == "/api/reviews/resolve":
                    result = ledger.resolve_review(
                        actor, body["class_id"], body["key"], body["decision"],
                        note=body.get("note", ""), as_of=body.get("as_of"))
                else:
                    self._not_found()
                    return
                self._send_json(200, result)
            except DomainError as exc:
                self._handle_error(exc)
            except KeyError as exc:
                self._handle_error(ValidationError(f"缺少字段: {exc.args[0]}"))

        def log_message(self, *_args):
            return

    Handler.ledger = ledger
    return Handler


# 默认模块级 Handler，保持 `from service import Handler` 的历史用法
ledger = Ledger()
Handler = build_handler(ledger)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        assert len(ROLE_RESEARCHER) == 3
        assert Ledger() is not Ledger()
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
