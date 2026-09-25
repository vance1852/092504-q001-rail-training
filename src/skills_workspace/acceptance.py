"""运行基础服务与缺陷闭环的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .defects import DefectService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链和一条缺陷闭环链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        defects = DefectService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范训练机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="训练负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-trainee", actor_id="admin-001", new_actor_id="trainee-001",
                               display_name="学员甲", role="trainee", organization_id="org-001")
        service.register_actor(request_id="req-instructor-1", actor_id="admin-001", new_actor_id="instructor-001",
                               display_name="教员甲", role="instructor", organization_id="org-001")
        service.register_actor(request_id="req-instructor-2", actor_id="admin-001", new_actor_id="instructor-002",
                               display_name="教员乙", role="instructor", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号生产场所", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="institution_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="institution_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        defects.register_vehicle(request_id="req-vehicle", actor_id="operator-001",
                                 vehicle_id="vehicle-001", site_id="site-001", name="示范轨道车辆")
        defects.register_station(request_id="req-station", actor_id="operator-001",
                                 station_id="station-001", site_id="site-001", name="车门实训工位")
        defects.register_check_item(request_id="req-item-door", actor_id="operator-001",
                                    item_id="item-door-001", system="door", title="车门锁闭状态检查",
                                    safety_critical=True, criteria="锁闭到位开关回路阻值小于 1Ω")
        defects.register_check_item(request_id="req-item-brake", actor_id="operator-001",
                                    item_id="item-brake-001", system="brake", title="制动缸行程测量",
                                    safety_critical=False, criteria="行程在 90mm 至 130mm 之间")
        defects.record_measurement(request_id="req-measurement", actor_id="operator-001",
                                   measurement_id="mea-001", vehicle_id="vehicle-001",
                                   item_id="item-door-001", value="3.2", unit="Ω", passed=False)
        defects.register_component_batch(request_id="req-batch", actor_id="operator-001",
                                         batch_id="batch-001", part_number="DL-LOCK-2026A",
                                         description="车门锁闭机构批次", quantity=12)
        defects.open_case(request_id="req-case", actor_id="operator-001", case_id="case-001",
                          vehicle_id="vehicle-001", station_id="station-001", system="door",
                          title="车门锁闭故障排故", check_item_ids=["item-door-001"],
                          measurement_ids=["mea-001"])
        claim = defects.claim_case(request_id="req-claim", actor_id="trainee-001",
                                   case_id="case-001", duration_minutes=120)
        defects.record_component_movement(request_id="req-move", actor_id="trainee-001",
                                          case_id="case-001", batch_id="batch-001", action="install",
                                          quantity=1, note="更换锁闭机构")
        defects.submit_disposition(request_id="req-submit-1", actor_id="trainee-001", case_id="case-001",
                                   diagnosis="锁闭开关失效", isolation="隔离车门回路", retest="复测阻值 0.8Ω")
        defects.review_disposition(request_id="req-review-1", actor_id="instructor-001", case_id="case-001",
                                   revision_no=1, decision="reject", note="隔离措施未覆盖旁路风险")
        defects.submit_disposition(request_id="req-submit-2", actor_id="trainee-001", case_id="case-001",
                                   diagnosis="锁闭开关失效并伴随旁路松动", isolation="隔离车门回路并挂牌上锁",
                                   retest="复测阻值 0.8Ω 且旁路断开")
        defects.review_disposition(request_id="req-review-2", actor_id="instructor-001", case_id="case-001",
                                   revision_no=2, decision="approve", note="处置有效，同意关闭")
        defects.cosign_closure(request_id="req-cosign", actor_id="instructor-002", case_id="case-001",
                               revision_no=2, note="复核复测数据，同意关闭")

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        report = defects.case_report("case-001")
        pending = defects.pending_report("site-001")
        flow = defects.component_flow("batch-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "case_status": report["case"]["status"],
                  "case_revisions": len(report["revisions"]),
                  "rejected_revision_preserved": report["revisions"][0]["status"] == "rejected",
                  "second_signature": report["revisions"][1]["second_signed_by"]["actor_id"] == "instructor-002",
                  "closed_case_version": report["case"]["closed_case_version"],
                  "lease_closed": report["leases"][0]["status"] == "closed",
                  "claim_expires_at": claim.detail["expires_at"],
                  "pending_unfinished": pending["counts"]["unfinished"],
                  "component_movements": flow["movement_count"]}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = result["status"] == "ok" and result["audit_valid"] and result["case_status"] == "closed"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
