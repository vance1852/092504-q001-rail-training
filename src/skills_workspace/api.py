"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .defects import DefectService
from .errors import DomainError, ValidationError
from .models import WriteReceipt
from .service import DomainService
from .storage import Database


def _receipt(status_created: int, receipt: WriteReceipt) -> tuple[int, dict[str, Any]]:
    """把幂等回执转换为 HTTP 响应，重放返回 200 并附上首次写入的明细。"""

    payload = receipt.__dict__
    return (200 if receipt.replayed else status_created), payload


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          defects: DefectService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    segments = [segment for segment in parsed.path.split("/") if segment]
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        if defects is not None:
            if method == "POST" and segments == ["vehicles"]:
                return _receipt(201, defects.register_vehicle(actor_id=actor_id, **body))
            if method == "POST" and segments == ["stations"]:
                return _receipt(201, defects.register_station(actor_id=actor_id, **body))
            if method == "POST" and segments == ["check-items"]:
                return _receipt(201, defects.register_check_item(actor_id=actor_id, **body))
            if method == "POST" and segments == ["measurements"]:
                return _receipt(201, defects.record_measurement(actor_id=actor_id, **body))
            if method == "POST" and segments == ["component-batches"]:
                return _receipt(201, defects.register_component_batch(actor_id=actor_id, **body))
            if method == "POST" and segments == ["cases"]:
                return _receipt(201, defects.open_case(actor_id=actor_id, **body))
            if method == "POST" and segments == ["leases", "reap"]:
                return 200, defects.reap_expired_leases(actor_id=actor_id)
            if method == "POST" and len(segments) == 3 and segments[0] == "cases":
                case_id, action = segments[1], segments[2]
                if action == "versions":
                    return _receipt(201, defects.update_case_version(actor_id=actor_id, case_id=case_id, **body))
                if action == "claims":
                    return _receipt(201, defects.claim_case(actor_id=actor_id, case_id=case_id, **body))
                if action == "dispositions":
                    return _receipt(201, defects.submit_disposition(actor_id=actor_id, case_id=case_id, **body))
                if action == "reviews":
                    return _receipt(200, defects.review_disposition(actor_id=actor_id, case_id=case_id, **body))
                if action == "cosign":
                    return _receipt(200, defects.cosign_closure(actor_id=actor_id, case_id=case_id, **body))
                if action == "movements":
                    return _receipt(201, defects.record_component_movement(actor_id=actor_id, case_id=case_id, **body))
            if method == "GET" and len(segments) == 3 and segments[0] == "cases" and segments[2] == "report":
                return 200, defects.case_report(segments[1])
            if method == "GET" and segments == ["reports", "pending"]:
                query = parse_qs(parsed.query)
                site_id = query.get("site_id", [None])[0]
                return 200, defects.pending_report(site_id)
            if method == "GET" and segments == ["reports", "component-flow"]:
                query = parse_qs(parsed.query)
                batch_id = query.get("batch_id", [""])[0]
                if not batch_id:
                    raise ValidationError("batch_id 不能为空")
                return 200, defects.component_flow(batch_id)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    defects: DefectService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                defects=getattr(self, "defects", None))
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
    Handler.service = DomainService(database)
    Handler.defects = DefectService(database)
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
