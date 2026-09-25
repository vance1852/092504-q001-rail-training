import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skills_workspace.defects import DefectService
from skills_workspace.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from skills_workspace.service import DomainService
from skills_workspace.storage import Database


class ManualClock:
    """测试用可推进时钟。"""

    def __init__(self, value):
        self._value = value

    def now(self):
        return self._value

    def advance(self, **kwargs):
        self._value += timedelta(**kwargs)


class DefectServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = ManualClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.domain = DomainService(self.database, self.clock)
        self.service = DefectService(self.database, self.clock)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="训练机构一")
        self.domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        for request_id, actor_id, name, role in (
                ("op", "op1", "训练负责人", "operator"),
                ("t1", "t1", "学员一", "trainee"),
                ("t2", "t2", "学员二", "trainee"),
                ("i1", "i1", "教员一", "instructor"),
                ("i2", "i2", "教员二", "instructor"),
                ("au", "au1", "审计员", "auditor")):
            self.domain.register_actor(request_id=request_id, actor_id="a1", new_actor_id=actor_id,
                                       display_name=name, role=role, organization_id="o1")
        self.domain.register_site(request_id="site", actor_id="op1", site_id="s1",
                                  organization_id="o1", name="实训场地", timezone_name="Asia/Shanghai")
        self.service.register_vehicle(request_id="veh", actor_id="op1", vehicle_id="veh-1",
                                      site_id="s1", name="示范轨道车辆")
        self.service.register_station(request_id="sta", actor_id="op1", station_id="sta-1",
                                      site_id="s1", name="车门实训工位")
        self.service.register_check_item(request_id="ci-door", actor_id="op1", item_id="item-door",
                                         system="door", title="车门锁闭状态检查",
                                         safety_critical=True, criteria="锁闭回路阻值小于 1Ω")
        self.service.register_check_item(request_id="ci-brake", actor_id="op1", item_id="item-brake",
                                         system="brake", title="制动缸行程测量",
                                         safety_critical=False, criteria="行程 90mm 至 130mm")
        self.service.record_measurement(request_id="mea-1", actor_id="op1", measurement_id="mea-1",
                                        vehicle_id="veh-1", item_id="item-door",
                                        value="3.2", unit="Ω", passed=False)
        self.service.register_component_batch(request_id="bat-1", actor_id="op1", batch_id="bat-1",
                                              part_number="DL-LOCK-2026A",
                                              description="车门锁闭机构批次", quantity=12)

    def tearDown(self):
        self.database.close()

    def _open_case(self, case_id="case-1", items=("item-door",), measurements=("mea-1",),
                   request_id=None, **kwargs):
        params = dict(request_id=request_id or f"open-{case_id}", actor_id="op1", case_id=case_id,
                      vehicle_id="veh-1", station_id="sta-1", system="door",
                      title="车门锁闭故障排故", check_item_ids=list(items),
                      measurement_ids=list(measurements))
        params.update(kwargs)
        return self.service.open_case(**params)

    def _claim(self, request_id="claim-1", actor_id="t1", case_id="case-1", minutes=60):
        return self.service.claim_case(request_id=request_id, actor_id=actor_id,
                                       case_id=case_id, duration_minutes=minutes)

    def _submit(self, request_id="sub-1", actor_id="t1", case_id="case-1"):
        return self.service.submit_disposition(request_id=request_id, actor_id=actor_id,
                                               case_id=case_id, diagnosis="锁闭开关失效",
                                               isolation="隔离车门回路", retest="复测阻值 0.8Ω")

    # ---- 完整闭环 ----

    def test_safety_critical_case_requires_second_signature(self):
        opened = self._open_case()
        self.assertTrue(opened.detail["safety_critical"])
        claim = self._claim()
        self.assertFalse(claim.replayed)
        self.assertEqual("2026-09-25T09:00:00.000000Z", claim.detail["expires_at"])
        self.service.record_component_movement(request_id="mv-1", actor_id="t1", case_id="case-1",
                                               batch_id="bat-1", action="install", quantity=1,
                                               note="更换锁闭机构")
        submitted = self._submit()
        self.assertEqual(1, submitted.detail["revision_no"])
        self.assertEqual(1, submitted.detail["case_version"])

        rejected = self.service.review_disposition(request_id="rev-1", actor_id="i1",
                                                   case_id="case-1", revision_no=1,
                                                   decision="reject", note="隔离措施未覆盖旁路风险")
        self.assertEqual("leased", rejected.detail["case_status"])

        resubmitted = self._submit(request_id="sub-2")
        self.assertEqual(2, resubmitted.detail["revision_no"])
        approved = self.service.review_disposition(request_id="rev-2", actor_id="i1",
                                                   case_id="case-1", revision_no=2,
                                                   decision="approve", note="处置有效")
        self.assertEqual("awaiting_second_signature", approved.detail["case_status"])

        with self.assertRaises(PermissionDenied):
            self.service.cosign_closure(request_id="co-x", actor_id="i1", case_id="case-1",
                                        revision_no=2, note="同一教员不能二次签署")
        closed = self.service.cosign_closure(request_id="co-1", actor_id="i2", case_id="case-1",
                                             revision_no=2, note="复核复测数据，同意关闭")
        self.assertEqual("closed", closed.detail["case_status"])

        report = self.service.case_report("case-1")
        self.assertEqual("closed", report["case"]["status"])
        self.assertEqual(2, report["case"]["closed_revision"])
        self.assertEqual(1, report["case"]["closed_case_version"])
        self.assertEqual(2, len(report["revisions"]))
        first, second = report["revisions"]
        self.assertEqual("rejected", first["status"])
        self.assertEqual("隔离措施未覆盖旁路风险", first["review_note"])
        self.assertEqual("closed", second["status"])
        self.assertEqual("i1", second["reviewed_by"]["actor_id"])
        self.assertEqual("i2", second["second_signed_by"]["actor_id"])
        self.assertEqual("closed", report["leases"][0]["status"])
        self.assertIsNone(report["responsible"]["actor_id"])
        self.assertEqual("案例已关闭", report["responsible"]["reason"])
        valid, _ = self.domain.verify_audit()
        self.assertTrue(valid)

    def test_non_critical_case_closes_with_single_review(self):
        self._open_case(case_id="case-2", items=("item-brake",), measurements=())
        self._claim(request_id="claim-2", case_id="case-2")
        self._submit(request_id="sub-2", case_id="case-2")
        result = self.service.review_disposition(request_id="rev-2", actor_id="i1",
                                                 case_id="case-2", revision_no=1,
                                                 decision="approve", note="处置有效")
        self.assertEqual("closed", result.detail["case_status"])
        report = self.service.case_report("case-2")
        self.assertEqual("closed", report["revisions"][0]["status"])
        self.assertIsNone(report["revisions"][0]["second_signed_by"])

    # ---- 并发与幂等 ----

    def test_concurrent_claims_have_exactly_one_winner(self):
        self._open_case()
        barrier = threading.Barrier(6)
        wins, conflicts = [], []

        def worker(index):
            barrier.wait()
            try:
                wins.append(self._claim(request_id=f"claim-{index}", actor_id="t1"))
            except ConflictError:
                conflicts.append(index)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, len(wins))
        self.assertEqual(5, len(conflicts))
        report = self.service.case_report("case-1")
        self.assertEqual(1, len(report["leases"]))
        self.assertEqual("t1", report["responsible"]["actor_id"])

    def test_concurrent_reviews_have_exactly_one_winner(self):
        self._open_case(case_id="case-2", items=("item-brake",), measurements=())
        self._claim(request_id="claim-2", case_id="case-2")
        self._submit(request_id="sub-2", case_id="case-2")
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(actor_id, request_id):
            barrier.wait()
            try:
                receipt = self.service.review_disposition(
                    request_id=request_id, actor_id=actor_id, case_id="case-2",
                    revision_no=1, decision="approve", note="处置有效")
                outcomes.append(("ok", receipt))
            except ConflictError:
                outcomes.append(("conflict", None))

        threads = [threading.Thread(target=worker, args=("i1", "rev-a")),
                   threading.Thread(target=worker, args=("i2", "rev-b"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        outcomes.sort(key=lambda item: item[0])
        self.assertEqual(["conflict", "ok"], [kind for kind, _ in outcomes])
        self.assertEqual("closed", self.service.case_report("case-2")["case"]["status"])

    def test_claim_replay_returns_same_receipt_and_conflict_on_change(self):
        self._open_case()
        first = self._claim()
        replay = self._claim()
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        self.assertEqual(first.detail, replay.detail)
        with self.assertRaises(ConflictError):
            self._claim(minutes=30)

    def test_measurement_business_key_is_idempotent(self):
        again = self.service.record_measurement(request_id="mea-1b", actor_id="op1",
                                                measurement_id="mea-1", vehicle_id="veh-1",
                                                item_id="item-door", value="3.2",
                                                unit="Ω", passed=False)
        self.assertEqual("mea-1", again.resource_id)
        with self.assertRaises(ConflictError):
            self.service.record_measurement(request_id="mea-1c", actor_id="op1",
                                            measurement_id="mea-1", vehicle_id="veh-1",
                                            item_id="item-door", value="9.9",
                                            unit="Ω", passed=False)

    def test_case_id_reuse_with_different_content_conflicts(self):
        self._open_case()
        with self.assertRaises(ConflictError):
            self._open_case(request_id="open-other", title="另一个故障")

    # ---- 租约超时 ----

    def test_expired_lease_blocks_submit_and_allows_reclaim(self):
        self._open_case()
        self._claim(minutes=60)
        self.clock.advance(minutes=61)
        with self.assertRaises(PermissionDenied):
            self._submit()
        reclaim = self._claim(request_id="claim-2", actor_id="t2")
        self.assertFalse(reclaim.replayed)
        report = self.service.case_report("case-1")
        self.assertEqual(2, len(report["leases"]))
        self.assertEqual("expired", report["leases"][0]["status"])
        self.assertEqual("active", report["leases"][1]["status"])
        self.assertEqual("t2", report["responsible"]["actor_id"])

    def test_reap_expired_leases_releases_case(self):
        self._open_case()
        lease = self._claim(minutes=30)
        self.clock.advance(minutes=31)
        result = self.service.reap_expired_leases(actor_id="op1")
        self.assertEqual([lease.resource_id], result["reaped_lease_ids"])
        report = self.service.case_report("case-1")
        self.assertEqual("open", report["case"]["status"])
        self.assertEqual("expired", report["leases"][0]["status"])
        again = self.service.reap_expired_leases(actor_id="op1")
        self.assertEqual(0, again["reaped"])

    def test_reject_after_lease_expiry_returns_case_to_open(self):
        self._open_case()
        self._claim(minutes=30)
        self._submit()
        self.clock.advance(minutes=31)
        result = self.service.review_disposition(request_id="rev-1", actor_id="i1",
                                                 case_id="case-1", revision_no=1,
                                                 decision="reject", note="复测数据不完整")
        self.assertEqual("open", result.detail["case_status"])

    # ---- 复核规则 ----

    def test_review_requires_instructor_role_and_pending_revision(self):
        self._open_case()
        self._claim()
        with self.assertRaises(NotFoundError):
            self.service.review_disposition(request_id="rev-x", actor_id="i1", case_id="case-1",
                                            revision_no=1, decision="approve", note="修订尚不存在")
        self._submit()
        with self.assertRaises(PermissionDenied):
            self.service.review_disposition(request_id="rev-y", actor_id="t2", case_id="case-1",
                                            revision_no=1, decision="approve", note="学员不能复核")
        with self.assertRaises(ValidationError):
            self.service.review_disposition(request_id="rev-z", actor_id="i1", case_id="case-1",
                                            revision_no=1, decision="maybe", note="非法决定")
        self.service.review_disposition(request_id="rev-1", actor_id="i1", case_id="case-1",
                                        revision_no=1, decision="reject", note="退回")
        with self.assertRaises(ConflictError):
            self.service.review_disposition(request_id="rev-w", actor_id="i2", case_id="case-1",
                                            revision_no=1, decision="approve", note="已复核过")

    def test_rejection_preserves_old_revision_conclusion(self):
        self._open_case()
        self._claim()
        self._submit(request_id="sub-1")
        self.service.review_disposition(request_id="rev-1", actor_id="i1", case_id="case-1",
                                        revision_no=1, decision="reject", note="第一次退回")
        self._submit(request_id="sub-2")
        self.service.review_disposition(request_id="rev-2", actor_id="i1", case_id="case-1",
                                        revision_no=2, decision="reject", note="第二次退回")
        report = self.service.case_report("case-1")
        self.assertEqual(2, len(report["revisions"]))
        self.assertEqual("第一次退回", report["revisions"][0]["review_note"])
        self.assertEqual("第二次退回", report["revisions"][1]["review_note"])
        self.assertEqual("rejected", report["revisions"][0]["status"])

    # ---- 版本 ----

    def test_case_version_snapshot_and_revision_binding(self):
        self._open_case()
        self.service.record_measurement(request_id="mea-2", actor_id="op1", measurement_id="mea-2",
                                        vehicle_id="veh-1", item_id="item-door",
                                        value="2.8", unit="Ω", passed=False)
        bumped = self.service.update_case_version(request_id="ver-2", actor_id="op1",
                                                  case_id="case-1", add_measurement_ids=["mea-2"],
                                                  note="交接班后复测")
        self.assertEqual(2, bumped.detail["version"])
        self._claim()
        submitted = self._submit()
        self.assertEqual(2, submitted.detail["case_version"])
        with self.assertRaises(ConflictError):
            self.service.update_case_version(request_id="ver-3", actor_id="op1", case_id="case-1",
                                             add_measurement_ids=["mea-2"], note="已领取不能升版")
        report = self.service.case_report("case-1")
        self.assertEqual(2, len(report["versions"]))
        self.assertEqual(["mea-1"], [m["measurement_id"] for m in report["versions"][0]["measurements"]])
        self.assertEqual(["mea-1", "mea-2"],
                         [m["measurement_id"] for m in report["versions"][1]["measurements"]])
        self.assertEqual(2, report["revisions"][0]["case_version"])

    def test_version_update_rejects_unknown_or_foreign_content(self):
        self._open_case()
        with self.assertRaises(NotFoundError):
            self.service.update_case_version(request_id="ver-x", actor_id="op1", case_id="case-1",
                                             add_measurement_ids=["mea-404"], note="测量不存在")
        with self.assertRaises(ValidationError):
            self.service.update_case_version(request_id="ver-y", actor_id="op1", case_id="case-1",
                                             add_measurement_ids=["mea-1"], note="重复内容")

    # ---- 部件流转 ----

    def test_component_flow_report_reconstructs_movements(self):
        self._open_case()
        self._claim()
        self.service.record_component_movement(request_id="mv-1", actor_id="t1", case_id="case-1",
                                               batch_id="bat-1", action="remove", quantity=1,
                                               note="拆下故障锁闭机构")
        self.service.record_component_movement(request_id="mv-2", actor_id="t1", case_id="case-1",
                                               batch_id="bat-1", action="install", quantity=1,
                                               note="装复新锁闭机构")
        flow = self.service.component_flow("bat-1")
        self.assertEqual(2, flow["movement_count"])
        self.assertEqual(["remove", "install"], [m["action"] for m in flow["movements"]])
        self.assertEqual("veh-1", flow["movements"][0]["vehicle_id"])
        self.assertEqual("t1", flow["movements"][0]["actor"]["actor_id"])
        with self.assertRaises(PermissionDenied):
            self.service.record_component_movement(request_id="mv-3", actor_id="t2",
                                                   case_id="case-1", batch_id="bat-1",
                                                   action="install", quantity=1, note="非持有人")

    # ---- 报告与重启 ----

    def test_pending_report_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "defects.sqlite3"
            database = Database(path)
            clock = ManualClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
            domain = DomainService(database, clock)
            service = DefectService(database, clock)
            domain.register_organization(request_id="org", actor_id="bootstrap",
                                         organization_id="o1", name="训练机构一")
            domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                  display_name="管理员", role="admin", organization_id="o1")
            domain.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                  display_name="训练负责人", role="operator", organization_id="o1")
            domain.register_actor(request_id="t1", actor_id="a1", new_actor_id="t1",
                                  display_name="学员一", role="trainee", organization_id="o1")
            domain.register_site(request_id="site", actor_id="op1", site_id="s1",
                                 organization_id="o1", name="实训场地", timezone_name="Asia/Shanghai")
            service.register_vehicle(request_id="veh", actor_id="op1", vehicle_id="veh-1",
                                     site_id="s1", name="示范轨道车辆")
            service.register_station(request_id="sta", actor_id="op1", station_id="sta-1",
                                     site_id="s1", name="车门实训工位")
            service.register_check_item(request_id="ci", actor_id="op1", item_id="item-door",
                                        system="door", title="车门锁闭状态检查",
                                        safety_critical=True, criteria="阻值小于 1Ω")
            service.open_case(request_id="c1", actor_id="op1", case_id="case-a",
                              vehicle_id="veh-1", station_id="sta-1", system="door",
                              title="故障甲", check_item_ids=["item-door"])
            service.open_case(request_id="c2", actor_id="op1", case_id="case-b",
                              vehicle_id="veh-1", station_id="sta-1", system="door",
                              title="故障乙", check_item_ids=["item-door"])
            service.claim_case(request_id="cl", actor_id="t1", case_id="case-a", duration_minutes=60)
            service.submit_disposition(request_id="sub", actor_id="t1", case_id="case-a",
                                       diagnosis="诊断", isolation="隔离", retest="复测")
            database.close()

            restarted = Database(path)
            try:
                clock2 = ManualClock(datetime(2026, 9, 25, 8, 30, tzinfo=timezone.utc))
                service2 = DefectService(restarted, clock2)
                domain2 = DomainService(restarted, clock2)
                pending = service2.pending_report("s1")
                self.assertEqual(1, pending["counts"]["awaiting_review"])
                self.assertEqual("case-a", pending["awaiting_review"][0]["case_id"])
                self.assertEqual(1, pending["counts"]["open_cases"])
                self.assertEqual("case-b", pending["open_cases"][0]["case_id"])
                self.assertEqual(1, pending["counts"]["active_leases"])
                self.assertEqual(3, pending["counts"]["unfinished"])
                clock2.advance(minutes=31)
                pending = service2.pending_report("s1")
                self.assertEqual(1, pending["counts"]["expired_leases"])
                valid, _ = domain2.verify_audit()
                self.assertTrue(valid)
            finally:
                restarted.close()

    def test_responsible_person_at_each_stage(self):
        self._open_case()
        responsible = self.service.case_report("case-1")["responsible"]
        self.assertEqual("待学员领取", responsible["reason"])
        self._claim()
        responsible = self.service.case_report("case-1")["responsible"]
        self.assertEqual("trainee", responsible["role"])
        self.assertEqual("t1", responsible["actor_id"])
        self._submit()
        responsible = self.service.case_report("case-1")["responsible"]
        self.assertEqual("instructor", responsible["role"])
        self.assertEqual(["t1"], responsible["excluded_actor_ids"])
        self.service.review_disposition(request_id="rev-1", actor_id="i1", case_id="case-1",
                                        revision_no=1, decision="approve", note="首签")
        responsible = self.service.case_report("case-1")["responsible"]
        self.assertEqual("待二次签署", responsible["reason"])
        self.assertEqual(["t1", "i1"], responsible["excluded_actor_ids"])

    def test_case_report_includes_judgment_basis_and_audit(self):
        self._open_case()
        self._claim()
        self._submit()
        self.service.review_disposition(request_id="rev-1", actor_id="i1", case_id="case-1",
                                        revision_no=1, decision="reject", note="复测数据不完整")
        report = self.service.case_report("case-1")
        actions = [event["action"] for event in report["audit_events"]]
        self.assertEqual(["case.opened", "lease.acquired", "disposition.submitted",
                          "disposition.rejected"], actions)
        rejected = next(event for event in report["audit_events"]
                        if event["action"] == "disposition.rejected")
        self.assertEqual("复测数据不完整", rejected["detail"]["note"])
        self.assertEqual(1, rejected["detail"]["case_version"])

    # ---- 权限与校验 ----

    def test_role_and_org_guards(self):
        with self.assertRaises(PermissionDenied):
            self._claim(actor_id="op1")
        with self.assertRaises(PermissionDenied):
            self.service.open_case(request_id="c-x", actor_id="t1", case_id="case-x",
                                   vehicle_id="veh-1", station_id="sta-1", system="door",
                                   title="越权", check_item_ids=["item-door"])
        with self.assertRaises(ValidationError):
            self._open_case(case_id="case-bad", system="engine")
        with self.assertRaises(NotFoundError):
            self.service.case_report("case-404")


class DefectApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = ManualClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.domain = DomainService(self.database, self.clock)
        self.defects = DefectService(self.database, self.clock)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="训练机构一")
        self.domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.domain.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                   display_name="训练负责人", role="operator", organization_id="o1")
        self.domain.register_actor(request_id="t1", actor_id="a1", new_actor_id="t1",
                                   display_name="学员一", role="trainee", organization_id="o1")
        self.domain.register_site(request_id="site", actor_id="op1", site_id="s1",
                                  organization_id="o1", name="实训场地", timezone_name="Asia/Shanghai")
        self.defects.register_vehicle(request_id="veh", actor_id="op1", vehicle_id="veh-1",
                                      site_id="s1", name="示范轨道车辆")
        self.defects.register_station(request_id="sta", actor_id="op1", station_id="sta-1",
                                      site_id="s1", name="车门实训工位")
        self.defects.register_check_item(request_id="ci", actor_id="op1", item_id="item-door",
                                         system="door", title="车门锁闭状态检查",
                                         safety_critical=True, criteria="阻值小于 1Ω")
        self.defects.open_case(request_id="c1", actor_id="op1", case_id="case-1",
                               vehicle_id="veh-1", station_id="sta-1", system="door",
                               title="车门锁闭故障排故", check_item_ids=["item-door"])

    def tearDown(self):
        self.database.close()

    def _route(self, method, path, body=None, actor=""):
        from skills_workspace.api import route
        return route(self.domain, method, path, body, {"X-Actor-Id": actor}, defects=self.defects)

    def test_claim_and_report_over_http(self):
        status, payload = self._route("POST", "/cases/case-1/claims",
                                      {"request_id": "rq-1", "duration_minutes": 30}, actor="t1")
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        self.assertEqual("2026-09-25T08:30:00.000000Z", payload["detail"]["expires_at"])
        status, payload = self._route("POST", "/cases/case-1/claims",
                                      {"request_id": "rq-1", "duration_minutes": 30}, actor="t1")
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])
        status, payload = self._route("POST", "/cases/case-1/claims",
                                      {"request_id": "rq-1", "duration_minutes": 45}, actor="t1")
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])
        status, payload = self._route("GET", "/cases/case-1/report")
        self.assertEqual(200, status)
        self.assertEqual("leased", payload["case"]["status"])
        self.assertEqual("t1", payload["responsible"]["actor_id"])
        status, payload = self._route("GET", "/reports/pending?site_id=s1")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["counts"]["active_leases"])

    def test_reap_and_component_flow_over_http(self):
        self._route("POST", "/cases/case-1/claims",
                    {"request_id": "rq-1", "duration_minutes": 1}, actor="t1")
        self.clock.advance(minutes=2)
        status, payload = self._route("POST", "/leases/reap", {}, actor="op1")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["reaped"])
        status, payload = self._route("GET", "/reports/component-flow?batch_id=bat-404")
        self.assertEqual(404, status)

    def test_defect_route_validation_errors(self):
        status, payload = self._route("POST", "/cases/case-1/claims",
                                      {"request_id": "rq-2", "duration_minutes": 0}, actor="t1")
        self.assertEqual(400, status)
        status, payload = self._route("POST", "/cases/case-1/claims",
                                      {"request_id": "rq-3", "duration_minutes": 30}, actor="op1")
        self.assertEqual(403, status)
        status, payload = self._route("GET", "/cases/case-404/report")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
