"""轨道车辆实训缺陷闭环服务。

接收标准化检查项、测量记录与部件批次，按车辆与训练工位形成带版本的
缺陷案例；学员凭有期限的处置租约作业，提交修订后由不同教员复核，退回
生成新修订而不覆盖旧结论，安全关键项须二次签署才能关闭。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .domain import is_known_system
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import (
    CaseReview,
    CaseRevision,
    ComponentBatch,
    ComponentMovement,
    DefectCase,
    InspectionItem,
    LeaseView,
    Measurement,
)
from .service import DomainService

DEFAULT_LEASE_SECONDS = 1800
MAX_LEASE_SECONDS = 24 * 3600

CASE_STATUSES = frozenset(
    {"open", "in_progress", "in_review", "awaiting_countersign", "returned", "closed"}
)
COMPONENT_MOVEMENTS = frozenset({"receive", "transfer", "consume"})


class DefectService(DomainService):
    """在基础登记能力之上实现缺陷案例的完整闭环。"""

    # ---- 基础登记：车辆、工位、检查项 ------------------------------------

    def register_vehicle(self, *, request_id: str, actor_id: str, vehicle_id: str,
                         site_id: str, name: str) -> Any:
        payload = {"actor_id": actor_id, "vehicle_id": vehicle_id,
                   "site_id": site_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._same_organization(actor, site["organization_id"])
            vehicle_id = self._identifier(vehicle_id, "vehicle_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO vehicles(vehicle_id,organization_id,site_id,name,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (vehicle_id, site["organization_id"], site_id, name, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("车辆编号已经存在或场所无效") from exc
                append_event(connection, actor_id=actor_id, action="vehicle.registered",
                             resource_type="vehicle", resource_id=vehicle_id,
                             detail={"site_id": site_id, "name": name}, occurred_at=self._now())
                return "vehicle", vehicle_id, {"vehicle_id": vehicle_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_vehicle", payload=payload, create=create)

    def register_workstation(self, *, request_id: str, actor_id: str, workstation_id: str,
                             site_id: str, name: str) -> Any:
        payload = {"actor_id": actor_id, "workstation_id": workstation_id,
                   "site_id": site_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._same_organization(actor, site["organization_id"])
            workstation_id = self._identifier(workstation_id, "workstation_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO workstations(workstation_id,site_id,name,created_at) "
                        "VALUES(?,?,?,?)",
                        (workstation_id, site_id, name, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("工位编号已经存在或场所无效") from exc
                append_event(connection, actor_id=actor_id, action="workstation.registered",
                             resource_type="workstation", resource_id=workstation_id,
                             detail={"site_id": site_id, "name": name}, occurred_at=self._now())
                return "workstation", workstation_id, {"workstation_id": workstation_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_workstation", payload=payload, create=create)

    def register_inspection_item(self, *, request_id: str, actor_id: str, item_code: str,
                                 title: str, system_code: str, safety_critical: bool,
                                 unit: str = "", lower_limit: float | None = None,
                                 upper_limit: float | None = None) -> Any:
        payload = {"actor_id": actor_id, "item_code": item_code, "title": title,
                   "system_code": system_code, "safety_critical": bool(safety_critical),
                   "unit": unit, "lower_limit": lower_limit, "upper_limit": upper_limit}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            organization_id = actor.organization_id
            item_code = self._identifier(item_code, "item_code")
            title = self._text(title, "title")
            if not is_known_system(system_code):
                raise ValidationError("system_code 必须是 door、brake 或 traction")
            unit = str(unit or "").strip()[:40]
            lower_limit, upper_limit = self._limits(lower_limit, upper_limit)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO inspection_items(organization_id,item_code,title,system_code,"
                        "safety_critical,unit,lower_limit,upper_limit,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (organization_id, item_code, title, system_code,
                         1 if safety_critical else 0, unit, lower_limit, upper_limit, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("检查项编号在本机构已经存在") from exc
                append_event(connection, actor_id=actor_id, action="inspection_item.registered",
                             resource_type="inspection_item", resource_id=item_code,
                             detail={"organization_id": organization_id, "system_code": system_code,
                                     "safety_critical": bool(safety_critical),
                                     "lower_limit": lower_limit, "upper_limit": upper_limit},
                             occurred_at=self._now())
                return "inspection_item", item_code, {"item_code": item_code}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_inspection_item", payload=payload, create=create)

    # ---- 测量记录 -------------------------------------------------------

    def record_measurement(self, *, request_id: str, actor_id: str, vehicle_id: str,
                           workstation_id: str, item_code: str, value: float,
                           component_batch_id: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "vehicle_id": vehicle_id,
                   "workstation_id": workstation_id, "item_code": item_code, "value": value,
                   "component_batch_id": component_batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            vehicle = self._vehicle(connection, vehicle_id)
            workstation = self._workstation(connection, workstation_id)
            if workstation["site_id"] != vehicle["site_id"]:
                raise ValidationError("车辆与工位不属于同一场所")
            self._same_organization(actor, vehicle["organization_id"])
            item = self._inspection_item(connection, vehicle["organization_id"], item_code)
            value = self._number(value, "value")
            in_tolerance = self._within_limits(value, item)
            if component_batch_id is not None:
                batch = self._batch(connection, component_batch_id)
                if batch["organization_id"] != vehicle["organization_id"]:
                    raise NotFoundError("部件批次不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                measurement_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO measurements(measurement_id,organization_id,vehicle_id,"
                    "workstation_id,item_code,value,unit,in_tolerance,component_batch_id,"
                    "measured_by,measured_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (measurement_id, vehicle["organization_id"], vehicle_id, workstation_id,
                     item_code, value, item["unit"], 1 if in_tolerance else 0,
                     component_batch_id, actor_id, self._now(), self._now()),
                )
                append_event(connection, actor_id=actor_id, action="measurement.recorded",
                             resource_type="measurement", resource_id=measurement_id,
                             detail={"vehicle_id": vehicle_id, "workstation_id": workstation_id,
                                     "item_code": item_code, "value": value,
                                     "in_tolerance": in_tolerance,
                                     "component_batch_id": component_batch_id},
                             occurred_at=self._now())
                return "measurement", measurement_id, {
                    "measurement_id": measurement_id, "in_tolerance": in_tolerance,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="record_measurement", payload=payload, create=create)

    # ---- 部件批次与流转 -------------------------------------------------

    def register_component_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                                 part_number: str, description: str = "", quantity: int = 0,
                                 location: str = "warehouse") -> Any:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "part_number": part_number,
                   "description": description, "quantity": quantity, "location": location}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            organization_id = actor.organization_id
            batch_id = self._identifier(batch_id, "batch_id")
            part_number = self._text(part_number, "part_number", 80)
            description = str(description or "").strip()[:500]
            quantity = self._integer(quantity, "quantity", minimum=0)
            location = self._text(location, "location", 80)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO component_batches(batch_id,organization_id,part_number,"
                        "description,quantity,location,created_at) VALUES(?,?,?,?,?,?,?)",
                        (batch_id, organization_id, part_number, description, quantity,
                         location, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("部件批次编号已经存在") from exc
                movement_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO component_movements(movement_id,batch_id,action,from_location,"
                    "to_location,actor_id,note,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                    (movement_id, batch_id, "registered", "", location, actor_id,
                     "批次建档", self._now()),
                )
                append_event(connection, actor_id=actor_id, action="component_batch.registered",
                             resource_type="component_batch", resource_id=batch_id,
                             detail={"part_number": part_number, "quantity": quantity,
                                     "location": location},
                             occurred_at=self._now())
                return "component_batch", batch_id, {"batch_id": batch_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_component_batch", payload=payload,
                                    create=create)

    def move_component(self, *, request_id: str, actor_id: str, batch_id: str, action: str,
                       to_location: str, quantity: int | None = None, note: str = "") -> Any:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "action": action,
                   "to_location": to_location, "quantity": quantity, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch = self._batch(connection, batch_id)
            self._same_organization(actor, batch["organization_id"])
            replay = self._replay_if_seen(
                connection, request_id=request_id, action="move_component", payload=payload)
            if replay is not None:
                return replay
            if action not in COMPONENT_MOVEMENTS:
                raise ValidationError("action 必须是 receive、transfer 或 consume")
            to_location = self._text(to_location, "to_location", 80)
            note = str(note or "").strip()[:500]
            current_quantity = batch["quantity"]
            from_location = batch["location"]
            if action == "receive":
                delta = self._integer(quantity, "quantity", minimum=1)
                new_quantity = current_quantity + delta
                from_location = ""
            elif action == "consume":
                delta = self._integer(quantity, "quantity", minimum=1)
                if delta > current_quantity:
                    raise ConflictError("消耗数量超过批次库存")
                new_quantity = current_quantity - delta
            else:
                if quantity is not None:
                    raise ValidationError("transfer 不允许修改数量")
                new_quantity = current_quantity
                if to_location == from_location:
                    raise ConflictError("部件已经位于该位置")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE component_batches SET quantity=?, location=? WHERE batch_id=?",
                    (new_quantity, to_location, batch_id),
                )
                movement_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO component_movements(movement_id,batch_id,action,from_location,"
                    "to_location,actor_id,note,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                    (movement_id, batch_id, action, from_location, to_location, actor_id,
                     note, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="component.moved",
                             resource_type="component_batch", resource_id=batch_id,
                             detail={"movement_id": movement_id, "action": action,
                                     "from_location": from_location, "to_location": to_location,
                                     "quantity": quantity, "quantity_after": new_quantity},
                             occurred_at=self._now())
                return "component_movement", movement_id, {
                    "movement_id": movement_id, "quantity_after": new_quantity,
                    "location": to_location,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="move_component", payload=payload, create=create)

    # ---- 缺陷案例：开案 -------------------------------------------------

    def open_defect_case(self, *, request_id: str, actor_id: str, vehicle_id: str,
                         workstation_id: str, item_code: str, case_key: str, title: str,
                         measurement_id: str | None = None) -> Any:
        payload = {"actor_id": actor_id, "vehicle_id": vehicle_id,
                   "workstation_id": workstation_id, "item_code": item_code,
                   "case_key": case_key, "title": title, "measurement_id": measurement_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            vehicle = self._vehicle(connection, vehicle_id)
            workstation = self._workstation(connection, workstation_id)
            if workstation["site_id"] != vehicle["site_id"]:
                raise ValidationError("车辆与工位不属于同一场所")
            self._same_organization(actor, vehicle["organization_id"])
            item = self._inspection_item(connection, vehicle["organization_id"], item_code)
            case_key = self._identifier(case_key, "case_key")
            title = self._text(title, "title")
            source_measurement_id: str | None = None
            source_basis: dict[str, Any] | None = None
            if measurement_id is not None:
                measurement = self._measurement(connection, measurement_id)
                if measurement["vehicle_id"] != vehicle_id or \
                        measurement["workstation_id"] != workstation_id or \
                        measurement["item_code"] != item_code:
                    raise ValidationError("测量记录与指定车辆、工位或检查项不一致")
                if measurement["in_tolerance"]:
                    raise ValidationError("测量结果仍在容差范围内，不能据此开立缺陷案例")
                source_measurement_id = measurement_id
                source_basis = {"measurement_id": measurement_id, "value": measurement["value"],
                                "unit": measurement["unit"],
                                "lower_limit": item["lower_limit"],
                                "upper_limit": item["upper_limit"]}

            def create() -> tuple[str, str, dict[str, Any]]:
                case_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO defect_cases(case_id,case_key,organization_id,vehicle_id,"
                        "workstation_id,item_code,safety_critical,source_measurement_id,status,"
                        "current_revision_no,opened_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,0,?,?)",
                        (case_id, case_key, vehicle["organization_id"], vehicle_id,
                         workstation_id, item_code, 1 if item["safety_critical"] else 0,
                         source_measurement_id, "open", actor_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("同一车辆与工位下的案例编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="defect_case.opened",
                             resource_type="defect_case", resource_id=case_id,
                             detail={"case_key": case_key, "title": title,
                                     "vehicle_id": vehicle_id, "workstation_id": workstation_id,
                                     "item_code": item_code,
                                     "safety_critical": bool(item["safety_critical"]),
                                     "source_measurement": source_basis},
                             occurred_at=self._now())
                return "defect_case", case_id, {"case_id": case_id, "status": "open",
                                                "safety_critical": bool(item["safety_critical"])}

            return self._idempotent(connection, request_id=request_id,
                                    action="open_defect_case", payload=payload, create=create)

    # ---- 处置租约：领取、释放、超时回收 ---------------------------------

    def claim_case(self, *, request_id: str, actor_id: str, case_id: str,
                   lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Any:
        payload = {"actor_id": actor_id, "case_id": case_id, "lease_seconds": lease_seconds}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            case = self._case(connection, case_id)
            self._same_organization(actor, case["organization_id"])
            lease_seconds = self._integer(lease_seconds, "lease_seconds",
                                          minimum=60, maximum=MAX_LEASE_SECONDS)
            replay = self._replay_if_seen(
                connection, request_id=request_id, action="claim_case", payload=payload)
            if replay is not None:
                return replay
            self._expire_due(connection)
            case = self._case(connection, case_id)
            if case["status"] not in ("open", "returned"):
                if case["status"] == "closed":
                    raise ConflictError("案例已经关闭，不能再次领取")
                raise ConflictError("案例已被其他学员持有或正在复核")

            def create() -> tuple[str, str, dict[str, Any]]:
                granted_at, expires_at = self._lease_window(lease_seconds)
                lease_id = uuid.uuid4().hex
                try:
                    connection.execute(
                        "INSERT INTO case_leases(lease_id,case_id,revision_base_no,holder_id,"
                        "granted_at,expires_at) VALUES(?,?,?,?,?,?)",
                        (lease_id, case_id, case["current_revision_no"], actor_id,
                         granted_at, expires_at),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("案例已被其他学员持有") from exc
                connection.execute(
                    "UPDATE defect_cases SET status='in_progress', lease_holder_id=?, "
                    "lease_expires_at=? WHERE case_id=?",
                    (actor_id, expires_at, case_id),
                )
                append_event(connection, actor_id=actor_id, action="defect_case.leased",
                             resource_type="defect_case", resource_id=case_id,
                             detail={"lease_id": lease_id, "holder_id": actor_id,
                                     "revision_base_no": case["current_revision_no"],
                                     "granted_at": granted_at, "expires_at": expires_at},
                             occurred_at=granted_at)
                return "case_lease", lease_id, {"lease_id": lease_id, "case_id": case_id,
                                                "holder_id": actor_id,
                                                "granted_at": granted_at,
                                                "expires_at": expires_at}

            return self._idempotent(connection, request_id=request_id,
                                    action="claim_case", payload=payload, create=create)

    def release_case(self, *, request_id: str, actor_id: str, case_id: str,
                     note: str = "") -> Any:
        payload = {"actor_id": actor_id, "case_id": case_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            case = self._case(connection, case_id)
            self._require_holder_or_admin(actor, case)
            replay = self._replay_if_seen(
                connection, request_id=request_id, action="release_case", payload=payload)
            if replay is not None:
                return replay
            self._expire_due(connection)
            case = self._case(connection, case_id)
            if case["status"] != "in_progress" or not case["lease_holder_id"]:
                raise ConflictError("案例当前没有有效租约")
            self._require_holder_or_admin(actor, case)

            def create() -> tuple[str, str, dict[str, Any]]:
                self._finish_active_lease(connection, case_id, "released", note)
                connection.execute(
                    "UPDATE defect_cases SET status='open', lease_holder_id=NULL, "
                    "lease_expires_at=NULL WHERE case_id=?",
                    (case_id,),
                )
                append_event(connection, actor_id=actor_id, action="defect_case.released",
                             resource_type="defect_case", resource_id=case_id,
                             detail={"holder_id": case["lease_holder_id"], "note": note},
                             occurred_at=self._now())
                return "defect_case", case_id, {"case_id": case_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id,
                                    action="release_case", payload=payload, create=create)

    def expire_due_leases(self) -> list[str]:
        """回收所有已到期但尚未释放的租约，返回被回收的案例编号。"""

        with self.database.transaction(immediate=True) as connection:
            return self._expire_due(connection)

    # ---- 提交修订与教员复核 ---------------------------------------------

    def submit_revision(self, *, request_id: str, actor_id: str, case_id: str,
                        diagnosis: str, isolation: str, retest_result: str,
                        evidence: list[Any] | None = None) -> Any:
        payload = {"actor_id": actor_id, "case_id": case_id, "diagnosis": diagnosis,
                   "isolation": isolation, "retest_result": retest_result,
                   "evidence": evidence or []}
        if not isinstance(evidence or [], list):
            raise ValidationError("evidence 必须是数组")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_if_seen(
                connection, request_id=request_id, action="submit_revision", payload=payload)
            if replay is not None:
                return replay
            self._expire_due(connection)
            case = self._case(connection, case_id)
            self._same_organization(actor, case["organization_id"])
            if case["status"] != "in_progress":
                raise ConflictError("案例不在处置中，无法提交修订")
            if case["lease_holder_id"] != actor_id and actor.role != "admin":
                raise PermissionDenied("只有当前租约持有人能提交修订")
            diagnosis = self._text(diagnosis, "diagnosis", 2000)
            isolation = self._text(isolation, "isolation", 2000)
            retest_result = self._text(retest_result, "retest_result", 2000)
            evidence_json = canonical_json(evidence or [])
            content_hash = digest({
                "diagnosis": diagnosis, "isolation": isolation,
                "retest_result": retest_result, "evidence": evidence or [],
            })
            revision_no = case["current_revision_no"] + 1

            def create() -> tuple[str, str, dict[str, Any]]:
                revision_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO case_revisions(revision_id,case_id,revision_no,diagnosis,"
                    "isolation,retest_result,evidence_json,submitted_by,submitted_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (revision_id, case_id, revision_no, diagnosis, isolation, retest_result,
                     evidence_json, actor_id, self._now()),
                )
                connection.execute(
                    "UPDATE defect_cases SET status='in_review', current_revision_no=?, "
                    "lease_holder_id=NULL, lease_expires_at=NULL WHERE case_id=?",
                    (revision_no, case_id),
                )
                self._finish_active_lease(connection, case_id, "submitted", "修订已提交复核")
                append_event(connection, actor_id=actor_id, action="defect.revision_submitted",
                             resource_type="defect_case", resource_id=case_id,
                             detail={"revision_id": revision_id, "revision_no": revision_no,
                                     "submitted_by": actor_id, "content_hash": content_hash},
                             occurred_at=self._now())
                return "case_revision", revision_id, {"revision_id": revision_id,
                                                      "case_id": case_id,
                                                      "revision_no": revision_no}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_revision", payload=payload, create=create)

    def review_revision(self, *, request_id: str, actor_id: str, case_id: str,
                        decision: str, reason: str = "") -> Any:
        payload = {"actor_id": actor_id, "case_id": case_id, "decision": decision,
                   "reason": reason}
        if decision not in ("approved", "returned"):
            raise ValidationError("decision 必须是 approved 或 returned")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            case = self._case(connection, case_id)
            self._same_organization(actor, case["organization_id"])
            replay = self._replay_if_seen(
                connection, request_id=request_id, action="review_revision", payload=payload)
            if replay is not None:
                return replay
            if case["status"] not in ("in_review", "awaiting_countersign"):
                raise ConflictError("案例当前没有等待复核的修订")
            revision = self._revision(connection, case_id, case["current_revision_no"])
            if revision["submitted_by"] == actor_id and actor.role != "admin":
                raise PermissionDenied("必须由提交学员之外的另一名教员复核")
            existing = self._reviews(connection, case_id, revision["revision_no"])
            prior = {row["decision"] for row in existing}
            if "returned" in prior:
                raise ConflictError("该修订已被退回，需要提交新修订")
            if "countersigned" in prior:
                raise ConflictError("该修订已经完成二次签署")
            if "approved" in prior:
                if decision == "returned":
                    raise ConflictError("修订已经通过首签，不能再退回")
                raise ConflictError("该修订已经完成首签，安全关键项等待二次签署")
            if decision == "returned":
                reason = self._text(reason, "reason", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO case_reviews(review_id,case_id,revision_no,reviewer_id,"
                    "decision,reason,signed_at) VALUES(?,?,?,?,?,?,?)",
                    (review_id, case_id, revision["revision_no"], actor_id,
                     decision, reason, self._now()),
                )
                if decision == "returned":
                    connection.execute(
                        "UPDATE defect_cases SET status='returned' WHERE case_id=?", (case_id,)
                    )
                    append_event(connection, actor_id=actor_id,
                                 action="defect.revision_returned",
                                 resource_type="defect_case", resource_id=case_id,
                                 detail={"review_id": review_id,
                                         "revision_no": revision["revision_no"],
                                         "reviewer_id": actor_id, "reason": reason},
                                 occurred_at=self._now())
                else:
                    next_status = "awaiting_countersign" if case["safety_critical"] else "closed"
                    connection.execute(
                        "UPDATE defect_cases SET status=? WHERE case_id=?",
                        (next_status, case_id),
                    )
                    append_event(connection, actor_id=actor_id,
                                 action="defect.revision_approved",
                                 resource_type="defect_case", resource_id=case_id,
                                 detail={"review_id": review_id,
                                         "revision_no": revision["revision_no"],
                                         "reviewer_id": actor_id,
                                         "safety_critical": bool(case["safety_critical"]),
                                         "next_status": next_status},
                                 occurred_at=self._now())
                    if next_status == "closed":
                        append_event(connection, actor_id=actor_id, action="defect_case.closed",
                                     resource_type="defect_case", resource_id=case_id,
                                     detail={"revision_no": revision["revision_no"],
                                             "closer_id": actor_id, "mode": "single_sign"},
                                     occurred_at=self._now())
                return "case_review", review_id, {"review_id": review_id, "case_id": case_id,
                                                  "revision_no": revision["revision_no"],
                                                  "decision": decision,
                                                  "status": "returned" if decision == "returned"
                                                  else next_status}

            return self._idempotent(connection, request_id=request_id,
                                    action="review_revision", payload=payload, create=create)

    def countersign_case(self, *, request_id: str, actor_id: str, case_id: str,
                         reason: str = "") -> Any:
        payload = {"actor_id": actor_id, "case_id": case_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            case = self._case(connection, case_id)
            self._same_organization(actor, case["organization_id"])
            replay = self._replay_if_seen(
                connection, request_id=request_id, action="countersign_case", payload=payload)
            if replay is not None:
                return replay
            if not case["safety_critical"]:
                raise ValidationError("非安全关键案例不需要二次签署")
            if case["status"] != "awaiting_countersign":
                raise ConflictError("案例当前不等待二次签署")
            revision = self._revision(connection, case_id, case["current_revision_no"])
            reviews = self._reviews(connection, case_id, revision["revision_no"])
            approver = next((row for row in reviews if row["decision"] == "approved"), None)
            if approver is None:
                raise ConflictError("缺少首次签署记录")
            if actor_id in (revision["submitted_by"], approver["reviewer_id"]) and \
                    actor.role != "admin":
                raise PermissionDenied("二次签署必须由另一名教员完成")
            reason = str(reason or "").strip()[:1000]

            def create() -> tuple[str, str, dict[str, Any]]:
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO case_reviews(review_id,case_id,revision_no,reviewer_id,"
                    "decision,reason,signed_at) VALUES(?,?,?,?,?,?,?)",
                    (review_id, case_id, revision["revision_no"], actor_id,
                     "countersigned", reason, self._now()),
                )
                connection.execute(
                    "UPDATE defect_cases SET status='closed' WHERE case_id=?", (case_id,)
                )
                append_event(connection, actor_id=actor_id, action="defect.countersigned",
                             resource_type="defect_case", resource_id=case_id,
                             detail={"review_id": review_id,
                                     "revision_no": revision["revision_no"],
                                     "first_signer_id": approver["reviewer_id"],
                                     "second_signer_id": actor_id},
                             occurred_at=self._now())
                append_event(connection, actor_id=actor_id, action="defect_case.closed",
                             resource_type="defect_case", resource_id=case_id,
                             detail={"revision_no": revision["revision_no"],
                                     "closer_id": actor_id, "mode": "dual_sign"},
                             occurred_at=self._now())
                return "case_review", review_id, {"review_id": review_id, "case_id": case_id,
                                                  "revision_no": revision["revision_no"],
                                                  "decision": "countersigned",
                                                  "status": "closed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="countersign_case", payload=payload, create=create)

    # ---- 查询与报告 -----------------------------------------------------

    def get_case(self, case_id: str) -> DefectCase:
        with self.database.transaction(immediate=True) as connection:
            self._expire_due(connection)
            row = self._case(connection, case_id)
        return self._case_model(row)

    def list_revisions(self, case_id: str) -> list[CaseRevision]:
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT * FROM case_revisions WHERE case_id=? ORDER BY revision_no", (case_id,)
            ).fetchall()
        return [CaseRevision(row["revision_id"], row["case_id"], row["revision_no"],
                             row["diagnosis"], row["isolation"], row["retest_result"],
                             json.loads(row["evidence_json"]), row["submitted_by"],
                             row["submitted_at"]) for row in rows]

    def list_reviews(self, case_id: str) -> list[CaseReview]:
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT * FROM case_reviews WHERE case_id=? ORDER BY signed_at, rowid", (case_id,)
            ).fetchall()
        return [CaseReview(row["review_id"], row["case_id"], row["revision_no"],
                           row["reviewer_id"], row["decision"], row["reason"],
                           row["signed_at"]) for row in rows]

    def list_leases(self, case_id: str) -> list[LeaseView]:
        now_text = self._now()
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT * FROM case_leases WHERE case_id=? ORDER BY granted_at", (case_id,)
            ).fetchall()
        result = []
        for row in rows:
            active = row["released_at"] is None and row["expires_at"] > now_text
            result.append(LeaseView(row["lease_id"], row["case_id"], row["revision_base_no"],
                                    row["holder_id"], row["granted_at"], row["expires_at"],
                                    active, row["released_at"], row["release_reason"]))
        return result

    def case_basis(self, case_id: str) -> list[dict[str, Any]]:
        """还原案例的每次判断依据（审计事件链）。"""

        return [event for event in self.audit_events(0) if event["resource_type"] ==
                "defect_case" and event["resource_id"] == case_id]

    def case_report(self, case_id: str) -> dict[str, Any]:
        """聚合单个案例的完整闭环视图。"""

        with self.database.transaction(immediate=True) as connection:
            self._expire_due(connection)
            row = self._case(connection, case_id)
            source = None
            if row["source_measurement_id"]:
                measurement = connection.execute(
                    "SELECT * FROM measurements WHERE measurement_id=?",
                    (row["source_measurement_id"],),
                ).fetchone()
                item = self._inspection_item(connection, row["organization_id"],
                                             row["item_code"])
                source = {"measurement_id": measurement["measurement_id"],
                          "value": measurement["value"], "unit": measurement["unit"],
                          "item_code": item["item_code"], "title": item["title"],
                          "system_code": item["system_code"],
                          "lower_limit": item["lower_limit"],
                          "upper_limit": item["upper_limit"],
                          "measured_by": measurement["measured_by"],
                          "measured_at": measurement["measured_at"],
                          "component_batch_id": measurement["component_batch_id"]}
        case = self._case_model(row)
        revisions = [revision.__dict__ for revision in self.list_revisions(case_id)]
        reviews = [review.__dict__ for review in self.list_reviews(case_id)]
        leases = [lease.__dict__ for lease in self.list_leases(case_id)]
        responsibility = self._responsibility(case, reviews)
        return {"case": case.__dict__, "source_measurement": source,
                "revisions": revisions, "reviews": reviews, "leases": leases,
                "current_responsibility": responsibility,
                "judgment_basis": self.case_basis(case_id)}

    def list_unfinished(self) -> list[dict[str, Any]]:
        """列出所有未关闭案例（重启后仍可还原未完工作）。"""

        with self.database.transaction(immediate=True) as connection:
            self._expire_due(connection)
            rows = connection.execute(
                "SELECT * FROM defect_cases WHERE status!='closed' "
                "ORDER BY created_at, case_id"
            ).fetchall()
        items = []
        for row in rows:
            case = self._case_model(row)
            items.append({"case": case.__dict__,
                          "current_responsibility": self._responsibility(case)})
        return items

    def component_trace(self, batch_id: str) -> dict[str, Any]:
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT * FROM component_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("部件批次不存在")
            movements = [ComponentMovement(m["movement_id"], m["batch_id"], m["action"],
                                           m["from_location"], m["to_location"], m["actor_id"],
                                           m["note"], m["occurred_at"]).__dict__
                         for m in self.database.connection.execute(
                             "SELECT * FROM component_movements WHERE batch_id=? "
                             "ORDER BY occurred_at, rowid", (batch_id,))]
        return {"batch": ComponentBatch(row["batch_id"], row["organization_id"],
                                        row["part_number"], row["description"],
                                        row["quantity"], row["location"]).__dict__,
                "movements": movements}

    def get_measurement(self, measurement_id: str) -> Measurement:
        with self.database.lock:
            row = self._measurement(self.database.connection, measurement_id)
        return Measurement(row["measurement_id"], row["vehicle_id"], row["workstation_id"],
                           row["item_code"], row["value"], row["unit"],
                           bool(row["in_tolerance"]), row["component_batch_id"],
                           row["measured_by"], row["measured_at"])

    def list_inspection_items(self, organization_id: str) -> list[InspectionItem]:
        with self.database.lock:
            rows = self.database.connection.execute(
                "SELECT * FROM inspection_items WHERE organization_id=? ORDER BY item_code",
                (organization_id,),
            ).fetchall()
        return [InspectionItem(r["organization_id"], r["item_code"], r["title"],
                               r["system_code"], bool(r["safety_critical"]), r["unit"],
                               r["lower_limit"], r["upper_limit"]) for r in rows]

    # ---- 内部辅助 -------------------------------------------------------

    def _format(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _lease_window(self, seconds: int) -> tuple[str, str]:
        granted = self.clock.now()
        expires = granted + timedelta(seconds=seconds)
        return self._format(granted), self._format(expires)

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _vehicle(self, connection, vehicle_id: str):
        row = connection.execute("SELECT * FROM vehicles WHERE vehicle_id=?",
                                 (vehicle_id,)).fetchone()
        if row is None:
            raise NotFoundError("车辆不存在")
        return row

    def _workstation(self, connection, workstation_id: str):
        row = connection.execute("SELECT * FROM workstations WHERE workstation_id=?",
                                 (workstation_id,)).fetchone()
        if row is None:
            raise NotFoundError("工位不存在")
        return row

    def _inspection_item(self, connection, organization_id: str, item_code: str):
        row = connection.execute(
            "SELECT * FROM inspection_items WHERE organization_id=? AND item_code=?",
            (organization_id, item_code),
        ).fetchone()
        if row is None:
            raise NotFoundError("检查项不存在")
        return row

    def _measurement(self, connection, measurement_id: str):
        row = connection.execute("SELECT * FROM measurements WHERE measurement_id=?",
                                 (measurement_id,)).fetchone()
        if row is None:
            raise NotFoundError("测量记录不存在")
        return row

    def _batch(self, connection, batch_id: str):
        row = connection.execute("SELECT * FROM component_batches WHERE batch_id=?",
                                 (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("部件批次不存在")
        return row

    def _case(self, connection, case_id: str):
        row = connection.execute("SELECT * FROM defect_cases WHERE case_id=?",
                                 (case_id,)).fetchone()
        if row is None:
            raise NotFoundError("缺陷案例不存在")
        return row

    def _revision(self, connection, case_id: str, revision_no: int):
        row = connection.execute(
            "SELECT * FROM case_revisions WHERE case_id=? AND revision_no=?",
            (case_id, revision_no),
        ).fetchone()
        if row is None:
            raise NotFoundError("案例修订不存在")
        return row

    def _reviews(self, connection, case_id: str, revision_no: int):
        return connection.execute(
            "SELECT * FROM case_reviews WHERE case_id=? AND revision_no=? ORDER BY signed_at,rowid",
            (case_id, revision_no),
        ).fetchall()

    def _finish_active_lease(self, connection, case_id: str, reason: str, note: str) -> None:
        connection.execute(
            "UPDATE case_leases SET released_at=?, release_reason=? "
            "WHERE case_id=? AND released_at IS NULL",
            (self._now(), reason, case_id),
        )

    def _expire_due(self, connection) -> list[str]:
        now_text = self._now()
        rows = connection.execute(
            "SELECT l.lease_id, l.case_id, l.holder_id FROM case_leases AS l "
            "JOIN defect_cases AS c ON c.case_id=l.case_id "
            "WHERE l.released_at IS NULL AND l.expires_at<=? AND c.status='in_progress'",
            (now_text,),
        ).fetchall()
        expired = []
        for row in rows:
            connection.execute(
                "UPDATE case_leases SET released_at=?, release_reason='timeout' WHERE lease_id=?",
                (now_text, row["lease_id"]),
            )
            connection.execute(
                "UPDATE defect_cases SET status='open', lease_holder_id=NULL, "
                "lease_expires_at=NULL WHERE case_id=?",
                (row["case_id"],),
            )
            append_event(connection, actor_id="system", action="defect_case.lease_expired",
                         resource_type="defect_case", resource_id=row["case_id"],
                         detail={"lease_id": row["lease_id"], "holder_id": row["holder_id"],
                                 "expired_at": now_text},
                         occurred_at=now_text)
            expired.append(row["case_id"])
        return expired

    def _same_organization(self, actor, organization_id: str) -> None:
        if actor.organization_id != organization_id and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的资源")

    def _require_holder_or_admin(self, actor, case_row) -> None:
        if actor.role == "admin":
            return
        if actor.role != "operator" or case_row["lease_holder_id"] != actor.actor_id:
            raise PermissionDenied("只有租约持有人能执行该动作")

    def _limits(self, lower: Any, upper: Any) -> tuple[float | None, float | None]:
        lower = None if lower is None else float(lower)
        upper = None if upper is None else float(upper)
        if lower is not None and upper is not None and lower > upper:
            raise ValidationError("下限不能大于上限")
        return lower, upper

    def _within_limits(self, value: float, item) -> bool:
        if item["lower_limit"] is not None and value < item["lower_limit"]:
            return False
        if item["upper_limit"] is not None and value > item["upper_limit"]:
            return False
        return True

    def _number(self, value: Any, field: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{field} 必须是数字")
        if number != number or number in (float("inf"), float("-inf")):
            raise ValidationError(f"{field} 必须是有限数字")
        return number

    def _integer(self, value: Any, field: str, *, minimum: int | None = None,
                 maximum: int | None = None) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数")
        if minimum is not None and value < minimum:
            raise ValidationError(f"{field} 不能小于 {minimum}")
        if maximum is not None and value > maximum:
            raise ValidationError(f"{field} 不能大于 {maximum}")
        return value

    def _responsibility(self, case: DefectCase, reviews: list | None = None) -> dict[str, Any]:
        if case.status == "in_progress":
            return {"stage": "disposition", "responsible_actor_id": case.lease_holder_id,
                    "lease_expires_at": case.lease_expires_at}
        if case.status == "in_review":
            return {"stage": "review", "responsible_role": "reviewer"}
        if case.status == "awaiting_countersign":
            return {"stage": "countersign", "responsible_role": "reviewer"}
        if case.status == "returned":
            return {"stage": "rework", "responsible_role": "operator"}
        if case.status == "closed":
            return {"stage": "closed", "responsible_actor_id": None}
        return {"stage": "awaiting_claim", "responsible_role": "operator"}

    def _case_model(self, row) -> DefectCase:
        return DefectCase(row["case_id"], row["case_key"], row["organization_id"],
                          row["vehicle_id"], row["workstation_id"], row["item_code"],
                          bool(row["safety_critical"]), row["source_measurement_id"],
                          row["status"], row["current_revision_no"], row["lease_holder_id"],
                          row["lease_expires_at"], row["opened_by"], row["created_at"])
