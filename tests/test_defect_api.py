import unittest

from skills_workspace.api import route
from skills_workspace.defects import DefectService
from skills_workspace.storage import Database


class DefectApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DefectService(self.database)
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="实训中心")
        s.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="a1",
                         display_name="管理员", role="admin", organization_id="o1")
        s.register_actor(request_id="stu", actor_id="a1", new_actor_id="op1",
                         display_name="学员", role="operator", organization_id="o1")
        s.register_actor(request_id="tea", actor_id="a1", new_actor_id="rv1",
                         display_name="教员", role="reviewer", organization_id="o1")
        s.register_site(request_id="st", actor_id="a1", site_id="s1",
                        organization_id="o1", name="车间", timezone_name="Asia/Shanghai")
        s.register_vehicle(request_id="vh", actor_id="a1", vehicle_id="v1",
                           site_id="s1", name="拖车")
        s.register_workstation(request_id="ws", actor_id="a1", workstation_id="w1",
                               site_id="s1", name="车门工位")
        s.register_inspection_item(request_id="it", actor_id="rv1", item_code="DOOR-1",
                                   title="车门力", system_code="door",
                                   safety_critical=False, unit="N",
                                   lower_limit=100, upper_limit=200)

    def tearDown(self):
        self.database.close()

    def _post(self, path, body, actor):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor})

    def test_full_non_safety_loop_over_routes(self):
        status, opened = self._post(
            "/defect-cases",
            {"request_id": "c1", "vehicle_id": "v1", "workstation_id": "w1",
             "item_code": "DOOR-1", "case_key": "D-1", "title": "超差"}, "op1")
        self.assertEqual(201, status)
        case_id = opened["resource_id"]

        status, claim = self._post(
            f"/defect-cases/{case_id}/claim",
            {"request_id": "l1", "lease_seconds": 600}, "op1")
        self.assertEqual(201, status)

        # 相同回执重放返回 200。
        status, replay = self._post(
            f"/defect-cases/{case_id}/claim",
            {"request_id": "l1", "lease_seconds": 600}, "op1")
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])
        self.assertEqual(claim["resource_id"], replay["resource_id"])

        status, revision = self._post(
            f"/defect-cases/{case_id}/revisions",
            {"request_id": "r1", "diagnosis": "d", "isolation": "i",
             "retest_result": "ok"}, "op1")
        self.assertEqual(201, status)

        # 学员复核自己 -> 403。
        status, _ = self._post(
            f"/defect-cases/{case_id}/reviews",
            {"request_id": "x1", "decision": "approved"}, "op1")
        self.assertEqual(403, status)

        status, review = self._post(
            f"/defect-cases/{case_id}/reviews",
            {"request_id": "v1", "decision": "approved"}, "rv1")
        self.assertEqual(201, status)

        status, report = route(self.service, "GET", f"/defect-cases/{case_id}", None)
        self.assertEqual(200, status)
        self.assertEqual("closed", report["case"]["status"])
        self.assertEqual(1, len(report["revisions"]))

    def test_concurrent_claim_conflict_over_route(self):
        _, opened = self._post(
            "/defect-cases",
            {"request_id": "c1", "vehicle_id": "v1", "workstation_id": "w1",
             "item_code": "DOOR-1", "case_key": "D-1", "title": "超差"}, "op1")
        case_id = opened["resource_id"]
        self._post(f"/defect-cases/{case_id}/claim",
                   {"request_id": "l1", "lease_seconds": 600}, "op1")
        # 第二个学员用不同 request_id 领取 -> 409。
        status, payload = self._post(
            f"/defect-cases/{case_id}/claim",
            {"request_id": "l2", "lease_seconds": 600}, "rv1")
        self.assertEqual(403, status)  # reviewer 不能领取
        status, payload = route(
            self.service, "POST", f"/defect-cases/{case_id}/claim",
            {"request_id": "l2", "lease_seconds": 600}, {"X-Actor-Id": "a1"})
        # admin 可领取，但案例已被 op1 持有 -> 409
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])

    def test_unfinished_and_expiration_routes(self):
        _, opened = self._post(
            "/defect-cases",
            {"request_id": "c1", "vehicle_id": "v1", "workstation_id": "w1",
             "item_code": "DOOR-1", "case_key": "D-1", "title": "超差"}, "op1")
        case_id = opened["resource_id"]
        status, payload = route(self.service, "GET", "/defect-cases/unfinished", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        status, payload = route(self.service, "POST", "/lease-expirations", {},
                                {"X-Actor-Id": "a1"})
        self.assertEqual(200, status)
        self.assertEqual([], payload["expired_case_ids"])

    def test_case_not_found_is_404(self):
        status, payload = route(self.service, "GET", "/defect-cases/nope", None)
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
