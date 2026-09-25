"""离线命令行报告：还原判断依据、部件流转、责任人与未完工作。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .defects import DefectService
from .storage import Database


def build_report(service: DefectService, kind: str, key: str | None = None) -> dict:
    if kind == "case":
        if not key:
            raise ValueError("case 报告需要提供 case_id")
        return service.case_report(key)
    if kind == "unfinished":
        return {"items": service.list_unfinished()}
    if kind == "component":
        if not key:
            raise ValueError("component 报告需要提供 batch_id")
        return service.component_trace(key)
    if kind == "audit":
        valid, count = service.verify_audit()
        return {"audit_valid": valid, "audit_events": count, "items": service.audit_events(0)}
    raise ValueError(f"未知报告类型: {kind}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="输出缺陷闭环离线报告")
    parser.add_argument("--database", required=True, help="SQLite 数据库文件")
    parser.add_argument("kind", choices=("case", "unfinished", "component", "audit"))
    parser.add_argument("key", nargs="?", default=None, help="case_id 或 batch_id")
    args = parser.parse_args(argv)
    if not Path(args.database).exists():
        parser.error(f"数据库文件不存在: {args.database}")
    database = Database(args.database)
    try:
        service = DefectService(database)
        report = build_report(service, args.kind, args.key)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
