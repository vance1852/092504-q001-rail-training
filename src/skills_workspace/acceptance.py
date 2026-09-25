"""运行基础服务与缺陷闭环的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .defects import DefectService
from .errors import ConflictError, PermissionDenied
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行一条完整登记链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = DomainService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范训练机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="训练负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号生产场所", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="institution_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="institution_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed}
        database.close()
        return result


def _bootstrap(service: DefectService) -> None:
    service.register_organization(request_id="d-org", actor_id="bootstrap",
                                  organization_id="org-1", name="轨道车辆实训中心")
    service.register_actor(request_id="d-admin", actor_id="bootstrap", new_actor_id="admin-1",
                           display_name="管理员", role="admin", organization_id="org-1")
    service.register_actor(request_id="d-op1", actor_id="admin-1", new_actor_id="op-1",
                           display_name="学员甲", role="operator", organization_id="org-1")
    service.register_actor(request_id="d-op2", actor_id="admin-1", new_actor_id="op-2",
                           display_name="学员乙", role="operator", organization_id="org-1")
    service.register_actor(request_id="d-rev1", actor_id="admin-1", new_actor_id="rev-1",
                           display_name="王教员", role="reviewer", organization_id="org-1")
    service.register_actor(request_id="d-rev2", actor_id="admin-1", new_actor_id="rev-2",
                           display_name="李教员", role="reviewer", organization_id="org-1")
    service.register_site(request_id="d-site", actor_id="admin-1", site_id="site-1",
                          organization_id="org-1", name="车门制动牵引实训车间",
                          timezone_name="Asia/Shanghai")
    service.register_vehicle(request_id="d-vehicle", actor_id="admin-1", vehicle_id="veh-1",
                             site_id="site-1", name="三号编组拖车")
    service.register_workstation(request_id="d-ws", actor_id="admin-1", workstation_id="ws-1",
                                 site_id="site-1", name="车门综合排故工位")


def run_defect() -> dict[str, object]:
    """执行安全关键缺陷案例的完整闭环，并覆盖并发、超时、重放与重启。"""

    clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "defect.sqlite3"
        database = Database(path)
        service = DefectService(database, clock)
        _bootstrap(service)

        # 标准化检查项：车门关闭力为安全关键项，制动缸压力为普通项。
        service.register_inspection_item(
            request_id="d-item-door", actor_id="rev-1", item_code="DOOR-CLOSE-FORCE",
            title="车门关闭压紧力", system_code="door", safety_critical=True,
            unit="N", lower_limit=100, upper_limit=200)
        service.register_inspection_item(
            request_id="d-item-brake", actor_id="rev-1", item_code="BRAKE-CYL-PRESS",
            title="制动缸压力", system_code="brake", safety_critical=False,
            unit="kPa", lower_limit=450, upper_limit=550)

        # 部件批次与流转：建档、转移到工位、消耗两件。
        service.register_component_batch(request_id="d-batch", actor_id="op-1",
                                         batch_id="bat-1", part_number="DR-SEAL-7",
                                         description="车门密封压条", quantity=10,
                                         location="warehouse")
        service.move_component(request_id="d-move-1", actor_id="op-1", batch_id="bat-1",
                               action="transfer", to_location="ws-1")
        service.move_component(request_id="d-move-2", actor_id="op-1", batch_id="bat-1",
                               action="consume", to_location="ws-1", quantity=2,
                               note="更换压条")

        # 超差测量触发安全关键缺陷案例。
        measurement = service.record_measurement(
            request_id="d-meas", actor_id="op-1", vehicle_id="veh-1", workstation_id="ws-1",
            item_code="DOOR-CLOSE-FORCE", value=232, component_batch_id="bat-1")
        measurement_id = measurement.resource_id
        opened = service.open_defect_case(
            request_id="d-case-open", actor_id="op-1", vehicle_id="veh-1",
            workstation_id="ws-1", item_code="DOOR-CLOSE-FORCE", case_key="DOOR-001",
            title="三号车三门关闭力超差", measurement_id=measurement_id)
        door_case = opened.resource_id
        # 同一车辆工位复用案例编号得到确定冲突。
        try:
            service.open_defect_case(
                request_id="d-case-open-2", actor_id="op-1", vehicle_id="veh-1",
                workstation_id="ws-1", item_code="DOOR-CLOSE-FORCE", case_key="DOOR-001",
                title="重复开案", measurement_id=measurement_id)
            duplicate_blocked = False
        except ConflictError:
            duplicate_blocked = True

        # 学员甲领取；重复回放相同回执得到同一租约；学员乙并发领取被拒。
        claim1 = service.claim_case(request_id="d-claim-1", actor_id="op-1",
                                    case_id=door_case, lease_seconds=1800)
        claim1_replay = service.claim_case(request_id="d-claim-1", actor_id="op-1",
                                           case_id=door_case, lease_seconds=1800)
        try:
            service.claim_case(request_id="d-claim-2", actor_id="op-2", case_id=door_case)
            concurrent_blocked = False
        except ConflictError:
            concurrent_blocked = True

        # 时间推进超过租约期限，系统回收后案例重新可领。
        clock.advance(timedelta(seconds=1801))
        expired = service.expire_due_leases()
        # 相同回执重放仍返回原租约编号；相同编号携带不同内容则明确冲突。
        replay_after_expiry = service.claim_case(request_id="d-claim-1", actor_id="op-1",
                                                 case_id=door_case, lease_seconds=1800)
        try:
            service.claim_case(request_id="d-claim-1", actor_id="op-1", case_id=door_case,
                               lease_seconds=900)
            reuse_blocked = False
        except ConflictError:
            reuse_blocked = True
        claim2 = service.claim_case(request_id="d-claim-3", actor_id="op-2",
                                    case_id=door_case, lease_seconds=3600)

        # 学员乙提交首版修订。
        service.submit_revision(
            request_id="d-rev-1", actor_id="op-2", case_id=door_case,
            diagnosis="压紧力偏高，初判为门控器参数漂移",
            isolation="隔离 3 号门门控器并断电",
            retest_result="复测 221N，仍超上限",
            evidence=[{"step": "参数读取", "value": 221}])

        # 另开一个普通制动案例并始终不处理，用于重启后的未完工作核对。
        service.open_defect_case(
            request_id="d-case-brake", actor_id="op-1", vehicle_id="veh-1",
            workstation_id="ws-1", item_code="BRAKE-CYL-PRESS", case_key="BRAKE-009",
            title="制动缸压力偏低")

        # 模拟服务重启：关闭后以同一数据库文件恢复，未完工作与责任人原样还原。
        database.close()
        database = Database(path)
        service = DefectService(database, clock)
        unfinished_before_review = service.list_unfinished()
        restarted_keys = {item["case"]["case_key"] for item in unfinished_before_review}
        restarted_holder = {
            item["case"]["case_key"]: item["case"]["lease_holder_id"]
            for item in unfinished_before_review
            if item["case"]["status"] == "in_review"
        }

        # 王教员复核首版并退回；旧修订保留，案例回到返工。
        review1 = service.review_revision(
            request_id="d-review-1", actor_id="rev-1", case_id=door_case,
            decision="returned", reason="复测仍超差，需检查机械卡滞")
        revisions_after_return = len(service.list_revisions(door_case))

        # 学员乙重新领取并提交第二版修订，首版结论不被覆盖。
        service.claim_case(request_id="d-claim-4", actor_id="op-2",
                           case_id=door_case, lease_seconds=3600)
        service.submit_revision(
            request_id="d-rev-2", actor_id="op-2", case_id=door_case,
            diagnosis="上导轨卡滞导致压紧力偏大",
            isolation="隔离上导轨并润滑复位，门控器恢复供电",
            retest_result="复测 168N，落在 100~200N 区间",
            evidence=[{"step": "导轨检查"}, {"step": "复测", "value": 168}])
        revision_rows = service.list_revisions(door_case)
        revision_history = [revision.revision_no for revision in revision_rows]
        first_revision_preserved = revision_rows[0].retest_result == "复测 221N，仍超上限"

        # 提交学员不能复核自己的修订；首签后必须由另一名教员二次签署。
        try:
            service.review_revision(request_id="d-self-review", actor_id="op-2",
                                    case_id=door_case, decision="approved")
            self_review_blocked = False
        except PermissionDenied:
            self_review_blocked = True
        service.review_revision(request_id="d-review-2", actor_id="rev-1",
                                case_id=door_case, decision="approved")
        awaiting = service.get_case(door_case)
        try:
            service.countersign_case(request_id="d-counter-dup", actor_id="rev-1",
                                     case_id=door_case)
            same_signer_blocked = False
        except PermissionDenied:
            same_signer_blocked = True
        service.countersign_case(request_id="d-counter", actor_id="rev-2",
                                 case_id=door_case)
        closed_case = service.get_case(door_case)
        report = service.case_report(door_case)
        trace = service.component_trace("bat-1")
        unfinished = service.list_unfinished()
        valid, event_count = service.verify_audit()
        database.close()

        return {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "door_case_id": door_case,
            "measurement_id": measurement_id,
            "duplicate_case_blocked": duplicate_blocked,
            "first_claim_replayed": claim1_replay.replayed,
            "concurrent_claim_blocked": concurrent_blocked,
            "expired_case_ids": expired,
            "replay_after_expiry_replayed": replay_after_expiry.replayed,
            "reused_request_id_blocked": reuse_blocked,
            "second_lease_id": claim2.resource_id,
            "return_review_decision": review1.resource_id and "returned",
            "revisions_after_return": revisions_after_return,
            "revision_history": revision_history,
            "first_revision_preserved": first_revision_preserved,
            "self_review_blocked": self_review_blocked,
            "awaiting_status_before_countersign": awaiting.status,
            "same_signer_countersign_blocked": same_signer_blocked,
            "final_status": closed_case.status,
            "safety_critical": closed_case.safety_critical,
            "restart_unfinished_keys": sorted(restarted_keys),
            "restart_review_holder": restarted_holder,
            "report_revisions": len(report["revisions"]),
            "report_reviews": len(report["reviews"]),
            "report_responsibility_stage": report["current_responsibility"]["stage"],
            "component_quantity": trace["batch"]["quantity"],
            "component_movements": len(trace["movements"]),
            "component_locations": [m["to_location"] for m in trace["movements"]],
            "unfinished_after_close": [item["case"]["case_key"] for item in unfinished],
        }


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = {**run(), "defect": run_defect()}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = result["status"] == "ok" and result["audit_valid"] and result["defect"]["status"] == "ok"
    return 0 if ok and result["defect"]["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
