import threading
import unittest
from datetime import datetime, timedelta, timezone

from skills_workspace.clock import FixedClock
from skills_workspace.defects import DefectService
from skills_workspace.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from skills_workspace.storage import Database


class DefectServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.service = DefectService(self.database, self.clock)
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="实训中心")
        s.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                         display_name="管理员", role="admin", organization_id="o1")
        s.register_actor(request_id="op1", actor_id="a1", new_actor_id="op1",
                         display_name="学员甲", role="operator", organization_id="o1")
        s.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                         display_name="学员乙", role="operator", organization_id="o1")
        s.register_actor(request_id="rev1", actor_id="a1", new_actor_id="rev1",
                         display_name="王教员", role="reviewer", organization_id="o1")
        s.register_actor(request_id="rev2", actor_id="a1", new_actor_id="rev2",
                         display_name="李教员", role="reviewer", organization_id="o1")
        s.register_site(request_id="site", actor_id="a1", site_id="s1",
                        organization_id="o1", name="车间", timezone_name="Asia/Shanghai")
        s.register_vehicle(request_id="veh", actor_id="a1", vehicle_id="v1",
                           site_id="s1", name="拖车")
        s.register_workstation(request_id="ws", actor_id="a1", workstation_id="w1",
                               site_id="s1", name="车门工位")
        s.register_inspection_item(request_id="item-door", actor_id="rev1",
                                   item_code="DOOR-FORCE", title="车门关闭力",
                                   system_code="door", safety_critical=True,
                                   unit="N", lower_limit=100, upper_limit=200)
        s.register_inspection_item(request_id="item-brake", actor_id="rev1",
                                   item_code="BRAKE-P", title="制动压力",
                                   system_code="brake", safety_critical=False,
                                   unit="kPa", lower_limit=450, upper_limit=550)

    def tearDown(self):
        self.database.close()

    def _open_safety_case(self, request_id="case-1", key="D-001"):
        receipt = self.service.open_defect_case(
            request_id=request_id, actor_id="op1", vehicle_id="v1", workstation_id="w1",
            item_code="DOOR-FORCE", case_key=key, title="车门关闭力超差")
        return receipt.resource_id

    # ---- 测量与检查项 ---------------------------------------------------

    def test_measurement_judgement_is_recorded(self):
        bad = self.service.record_measurement(
            request_id="m1", actor_id="op1", vehicle_id="v1", workstation_id="w1",
            item_code="DOOR-FORCE", value=230)
        good = self.service.record_measurement(
            request_id="m2", actor_id="op1", vehicle_id="v1", workstation_id="w1",
            item_code="DOOR-FORCE", value=150)
        self.assertFalse(bad.replayed)
        bad_view = self.service.get_measurement(bad.resource_id)
        good_view = self.service.get_measurement(good.resource_id)
        self.assertFalse(bad_view.in_tolerance)
        self.assertTrue(good_view.in_tolerance)

    def test_in_tolerance_measurement_cannot_open_case(self):
        receipt = self.service.record_measurement(
            request_id="m1", actor_id="op1", vehicle_id="v1", workstation_id="w1",
            item_code="DOOR-FORCE", value=150)
        with self.assertRaises(ValidationError):
            self.service.open_defect_case(
                request_id="c1", actor_id="op1", vehicle_id="v1", workstation_id="w1",
                item_code="DOOR-FORCE", case_key="D-001", title="不应立案",
                measurement_id=receipt.resource_id)

    def test_unknown_system_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.register_inspection_item(
                request_id="ix", actor_id="rev1", item_code="X-1", title="未知系统",
                system_code="hvac", safety_critical=False)

    # ---- 租约：并发、重放、超时 -----------------------------------------

    def test_concurrent_claims_have_single_winner(self):
        case_id = self._open_safety_case()
        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def claim(actor: str, request_id: str) -> None:
            barrier.wait()
            try:
                self.service.claim_case(request_id=request_id, actor_id=actor,
                                        case_id=case_id, lease_seconds=600)
                outcomes.append("won")
            except ConflictError:
                outcomes.append("lost")

        t1 = threading.Thread(target=claim, args=("op1", "q1"))
        t2 = threading.Thread(target=claim, args=("op2", "q2"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(outcomes), ["lost", "won"])
        view = self.service.get_case(case_id)
        self.assertEqual("in_progress", view.status)
        self.assertIn(view.lease_holder_id, ("op1", "op2"))

    def test_same_receipt_replays_lease_even_after_state_changes(self):
        case_id = self._open_safety_case()
        first = self.service.claim_case(request_id="q1", actor_id="op1",
                                        case_id=case_id, lease_seconds=600)
        # 他人并发领取被拒。
        with self.assertRaises(ConflictError):
            self.service.claim_case(request_id="q2", actor_id="op2", case_id=case_id)
        # 相同回执重放返回原租约。
        replay = self.service.claim_case(request_id="q1", actor_id="op1",
                                         case_id=case_id, lease_seconds=600)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        # 不同内容复用编号得到冲突。
        with self.assertRaises(ConflictError):
            self.service.claim_case(request_id="q1", actor_id="op1",
                                    case_id=case_id, lease_seconds=900)

    def test_expired_lease_is_reclaimed_and_replay_stays_stable(self):
        case_id = self._open_safety_case()
        first = self.service.claim_case(request_id="q1", actor_id="op1",
                                        case_id=case_id, lease_seconds=600)
        self.clock.advance(timedelta(seconds=601))
        expired = self.service.expire_due_leases()
        self.assertEqual([case_id], expired)
        view = self.service.get_case(case_id)
        self.assertEqual("open", view.status)
        self.assertIsNone(view.lease_holder_id)
        # 原回执重放仍是原租约编号，新编号产生新租约。
        replay = self.service.claim_case(request_id="q1", actor_id="op1",
                                         case_id=case_id, lease_seconds=600)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        second = self.service.claim_case(request_id="q3", actor_id="op2",
                                         case_id=case_id, lease_seconds=600)
        self.assertFalse(second.replayed)

    # ---- 修订、退回与不同教员复核 ---------------------------------------

    def test_return_creates_new_revision_without_overwriting(self):
        case_id = self._open_safety_case()
        self.service.claim_case(request_id="q1", actor_id="op1", case_id=case_id)
        self.service.submit_revision(
            request_id="r1", actor_id="op1", case_id=case_id,
            diagnosis="初判错误", isolation="断电隔离", retest_result="仍超差")
        self.service.review_revision(
            request_id="v1", actor_id="rev1", case_id=case_id,
            decision="returned", reason="继续排查")
        self.assertEqual("returned", self.service.get_case(case_id).status)
        self.service.claim_case(request_id="q2", actor_id="op1", case_id=case_id)
        self.service.submit_revision(
            request_id="r2", actor_id="op1", case_id=case_id,
            diagnosis="导轨卡滞", isolation="润滑复位", retest_result="复测合格")
        revisions = self.service.list_revisions(case_id)
        self.assertEqual([1, 2], [r.revision_no for r in revisions])
        self.assertEqual("仍超差", revisions[0].retest_result)
        self.assertEqual("复测合格", revisions[1].retest_result)

    def test_submitter_cannot_review_own_revision(self):
        case_id = self._open_safety_case()
        self.service.claim_case(request_id="q1", actor_id="op1", case_id=case_id)
        self.service.submit_revision(
            request_id="r1", actor_id="op1", case_id=case_id,
            diagnosis="d", isolation="i", retest_result="ok")
        with self.assertRaises(PermissionDenied):
            self.service.review_revision(
                request_id="v0", actor_id="op1", case_id=case_id, decision="approved")

    def test_safety_case_requires_two_distinct_instructors(self):
        case_id = self._open_safety_case()
        self.service.claim_case(request_id="q1", actor_id="op1", case_id=case_id)
        self.service.submit_revision(
            request_id="r1", actor_id="op1", case_id=case_id,
            diagnosis="d", isolation="i", retest_result="ok")
        self.service.review_revision(
            request_id="v1", actor_id="rev1", case_id=case_id, decision="approved")
        self.assertEqual("awaiting_countersign", self.service.get_case(case_id).status)
        with self.assertRaises(PermissionDenied):
            self.service.countersign_case(request_id="c1", actor_id="rev1", case_id=case_id)
        self.service.countersign_case(request_id="c2", actor_id="rev2", case_id=case_id)
        self.assertEqual("closed", self.service.get_case(case_id).status)

    def test_once_approved_a_revision_cannot_be_returned(self):
        case_id = self._open_safety_case()
        self.service.claim_case(request_id="q1", actor_id="op1", case_id=case_id)
        self.service.submit_revision(
            request_id="r1", actor_id="op1", case_id=case_id,
            diagnosis="d", isolation="i", retest_result="ok")
        self.service.review_revision(
            request_id="v1", actor_id="rev1", case_id=case_id, decision="approved")
        with self.assertRaises(ConflictError):
            self.service.review_revision(
                request_id="v2", actor_id="rev2", case_id=case_id,
                decision="returned", reason="反悔")

    def test_non_safety_case_closes_with_single_review(self):
        opened = self.service.open_defect_case(
            request_id="c2", actor_id="op1", vehicle_id="v1", workstation_id="w1",
            item_code="BRAKE-P", case_key="B-1", title="制动压力低")
        case_id = opened.resource_id
        self.service.claim_case(request_id="q1", actor_id="op1", case_id=case_id)
        self.service.submit_revision(
            request_id="r1", actor_id="op1", case_id=case_id,
            diagnosis="d", isolation="i", retest_result="ok")
        self.service.review_revision(
            request_id="v1", actor_id="rev1", case_id=case_id, decision="approved")
        self.assertEqual("closed", self.service.get_case(case_id).status)

    def test_closed_case_cannot_be_claimed(self):
        case_id = self._open_safety_case()
        self.service.claim_case(request_id="q1", actor_id="op1", case_id=case_id)
        self.service.submit_revision(
            request_id="r1", actor_id="op1", case_id=case_id,
            diagnosis="d", isolation="i", retest_result="ok")
        self.service.review_revision(
            request_id="v1", actor_id="rev1", case_id=case_id, decision="approved")
        self.service.countersign_case(request_id="c2", actor_id="rev2", case_id=case_id)
        with self.assertRaises(ConflictError):
            self.service.claim_case(request_id="q9", actor_id="op1", case_id=case_id)

    # ---- 部件流转 -------------------------------------------------------

    def test_component_movement_chain_and_stock(self):
        self.service.register_component_batch(
            request_id="b1", actor_id="op1", batch_id="bat-1", part_number="P-1",
            quantity=5, location="warehouse")
        self.service.move_component(request_id="mv1", actor_id="op1", batch_id="bat-1",
                                    action="transfer", to_location="w1")
        with self.assertRaises(ConflictError):
            self.service.move_component(request_id="mv2", actor_id="op1", batch_id="bat-1",
                                        action="consume", to_location="w1", quantity=9)
        self.service.move_component(request_id="mv3", actor_id="op1", batch_id="bat-1",
                                    action="consume", to_location="w1", quantity=2)
        trace = self.service.component_trace("bat-1")
        self.assertEqual(3, trace["batch"]["quantity"])
        self.assertEqual("w1", trace["batch"]["location"])
        self.assertEqual(["registered", "transfer", "consume"],
                         [m["action"] for m in trace["movements"]])
        with self.assertRaises(NotFoundError):
            self.service.component_trace("missing")

    # ---- 报告与审计 -----------------------------------------------------

    def test_report_restores_basis_responsibility_and_audit(self):
        case_id = self._open_safety_case()
        self.service.claim_case(request_id="q1", actor_id="op1", case_id=case_id)
        report = self.service.case_report(case_id)
        self.assertEqual("disposition", report["current_responsibility"]["stage"])
        self.assertEqual("op1", report["current_responsibility"]["responsible_actor_id"])
        actions = [event["action"] for event in report["judgment_basis"]]
        self.assertIn("defect_case.opened", actions)
        self.assertIn("defect_case.leased", actions)
        unfinished = {item["case"]["case_key"] for item in
                      self.service.list_unfinished()}
        self.assertIn("D-001", unfinished)
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)

    def test_reviewer_cannot_register_vehicle(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_vehicle(
                request_id="vx", actor_id="rev1", vehicle_id="vX",
                site_id="s1", name="越权车辆")


if __name__ == "__main__":
    unittest.main()
