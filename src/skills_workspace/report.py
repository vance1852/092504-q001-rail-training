"""生成缺陷闭环的命令行报告。

用法示例：

    PYTHONPATH=src python3 -m skills_workspace.report --database service.sqlite3 --case-id case-001
    PYTHONPATH=src python3 -m skills_workspace.report --database service.sqlite3 --batch-id batch-001
    PYTHONPATH=src python3 -m skills_workspace.report --database service.sqlite3 --pending [--site-id site-001]

报告内容直接来自 SQLite，服务重启后仍能还原每次判断依据、部件流转、
当前责任人以及未完工作。
"""

from __future__ import annotations

import argparse
import json

from .defects import DefectService
from .errors import DomainError
from .storage import Database


def main() -> int:
    """生成报告并打印 JSON，业务错误以退出码 1 结束。"""

    parser = argparse.ArgumentParser(description="生成轨道车辆实训缺陷闭环报告")
    parser.add_argument("--database", required=True, help="SQLite 数据库路径")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--case-id", help="输出单个缺陷案例的完整闭环报告")
    target.add_argument("--batch-id", help="输出部件批次的流转报告")
    target.add_argument("--pending", action="store_true", help="输出未完工作汇总报告")
    parser.add_argument("--site-id", default=None, help="按场所过滤未完工作")
    args = parser.parse_args()

    database = Database(args.database)
    try:
        service = DefectService(database)
        if args.case_id:
            report = service.case_report(args.case_id)
        elif args.batch_id:
            report = service.component_flow(args.batch_id)
        else:
            report = service.pending_report(args.site_id)
    except DomainError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False))
        return 1
    finally:
        database.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
