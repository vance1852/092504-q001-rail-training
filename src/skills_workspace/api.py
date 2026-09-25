"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .defects import DefectService
from .errors import DomainError, ValidationError
from .storage import Database


def route(service: DefectService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    receipt_status = lambda receipt: 200 if receipt.replayed else 201
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return receipt_status(receipt), receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return receipt_status(receipt), receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return receipt_status(receipt), receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return receipt_status(receipt), receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}

        # 轨道车辆实训缺陷闭环
        if method == "POST" and parsed.path == "/vehicles":
            receipt = service.register_vehicle(actor_id=actor_id, **body)
            return receipt_status(receipt), receipt.__dict__
        if method == "POST" and parsed.path == "/workstations":
            receipt = service.register_workstation(actor_id=actor_id, **body)
            return receipt_status(receipt), receipt.__dict__
        if method == "POST" and parsed.path == "/inspection-items":
            receipt = service.register_inspection_item(actor_id=actor_id, **body)
            return receipt_status(receipt), receipt.__dict__
        if method == "GET" and parsed.path == "/inspection-items":
            query = parse_qs(parsed.query)
            organization_id = query.get("organization_id", [""])[0]
            if not organization_id:
                raise ValidationError("organization_id 不能为空")
            return 200, {"items": [item.__dict__ for item in
                                   service.list_inspection_items(organization_id)]}
        if method == "POST" and parsed.path == "/measurements":
            receipt = service.record_measurement(actor_id=actor_id, **body)
            return receipt_status(receipt), receipt.__dict__
        if method == "POST" and parsed.path == "/component-batches":
            receipt = service.register_component_batch(actor_id=actor_id, **body)
            return receipt_status(receipt), receipt.__dict__
        if method == "POST" and parsed.path == "/component-movements":
            receipt = service.move_component(actor_id=actor_id, **body)
            return receipt_status(receipt), receipt.__dict__
        match = re.fullmatch(r"/component-batches/([A-Za-z0-9_.:-]+)", parsed.path)
        if method == "GET" and match:
            return 200, service.component_trace(match.group(1))
        if method == "POST" and parsed.path == "/defect-cases":
            receipt = service.open_defect_case(actor_id=actor_id, **body)
            return receipt_status(receipt), receipt.__dict__
        if method == "GET" and parsed.path == "/defect-cases/unfinished":
            return 200, {"items": service.list_unfinished()}
        if method == "POST" and parsed.path == "/lease-expirations":
            return 200, {"expired_case_ids": service.expire_due_leases()}
        case_match = re.fullmatch(r"/defect-cases/([A-Za-z0-9_.:-]+)", parsed.path)
        if method == "GET" and case_match:
            return 200, service.case_report(case_match.group(1))
        sub_match = re.fullmatch(
            r"/defect-cases/([A-Za-z0-9_.:-]+)/(claim|release|revisions|reviews|countersign)",
            parsed.path,
        )
        if method == "POST" and sub_match:
            case_id, action_name = sub_match.groups()
            arguments = {"actor_id": actor_id, "case_id": case_id, **body}
            actions = {
                "claim": service.claim_case,
                "release": service.release_case,
                "revisions": service.submit_revision,
                "reviews": service.review_revision,
                "countersign": service.countersign_case,
            }
            receipt = actions[action_name](**arguments)
            return receipt_status(receipt), receipt.__dict__
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DefectService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        if not isinstance(body, dict):
            self._write(400, {"error": "invalid_request", "message": "请求体必须是 JSON 对象"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动轨道车辆实训缺陷闭环服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DefectService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
