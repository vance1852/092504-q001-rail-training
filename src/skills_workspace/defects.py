"""轨道车辆实训缺陷闭环服务：案例版本、处置租约、复核签署与报告。

在基础服务（机构、操作者、场所、幂等回执、哈希链审计）之上实现：

- 接收标准化检查项、测量记录和部件批次；
- 按车辆与训练工位形成带版本的缺陷案例，版本快照检查项与测量记录；
- 学员领取案例获得有期限的处置租约，超时由时钟确定性回收；
- 学员提交诊断、隔离措施和复测结果，由不同教员复核；
- 退回保留旧修订结论，再次提交生成新修订号；
- 安全关键案例必须经第二名教员二次签署才能关闭；
- 报告还原每次判断依据、部件流转、当前责任人和重启后的未完工作。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Callable, Iterable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .service import IDENTIFIER
from .storage import Database

SYSTEMS = frozenset({"door", "brake", "traction"})

CASE_OPEN = "open"
CASE_LEASED = "leased"
CASE_IN_REVIEW = "in_review"
CASE_AWAITING_SECOND = "awaiting_second_signature"
CASE_CLOSED = "closed"

LEASE_ACTIVE = "active"
LEASE_EXPIRED = "expired"
LEASE_CLOSED = "closed"

REV_IN_REVIEW = "in_review"
REV_REJECTED = "rejected"
REV_AWAITING_SECOND = "awaiting_second_signature"
REV_CLOSED = "closed"

MOVEMENT_ACTIONS = frozenset({"install", "remove"})

MAX_LEASE_MINUTES = 24 * 60


class DefectService:
    """协调缺陷案例的权限、幂等、事务、审计和报告规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ---- 基础工具 ----

    @staticmethod
    def _format(value) -> str:
        """生成固定宽度 UTC 时间文本，保证字符串比较与时间比较一致。"""

        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _now(self) -> str:
        return self._format(self.clock.now())

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _flag(self, value: Any, field: str) -> bool:
        if not isinstance(value, bool):
            raise ValidationError(f"{field} 必须是布尔值")
        return value

    def _count(self, value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValidationError(f"{field} 必须是正整数")
        return value

    def _identifier_list(self, value: Any, field: str) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValidationError(f"{field} 必须是字符串数组")
        return [self._identifier(item, field) for item in value]

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"], row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _require_org(self, actor: Actor, organization_id: str) -> None:
        if actor.role != "admin" and actor.organization_id != organization_id:
            raise PermissionDenied("不能操作其他组织的资源")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        """相同请求返回原回执，不同内容复用编号时报冲突。"""

        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True,
                                json.loads(row["response_json"]))
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False, response)

    # ---- 案例与租约查询 ----

    def _case_row(self, connection, case_id: str):
        row = connection.execute("SELECT * FROM defect_cases WHERE case_id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFoundError("缺陷案例不存在")
        return row

    def _case_org(self, connection, case_id: str) -> str:
        row = connection.execute(
            "SELECT s.organization_id AS org FROM defect_cases c "
            "JOIN vehicles v ON v.vehicle_id=c.vehicle_id "
            "JOIN sites s ON s.site_id=v.site_id WHERE c.case_id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFoundError("缺陷案例不存在")
        return row["org"]

    def _active_lease(self, connection, case_id: str):
        return connection.execute(
            "SELECT * FROM leases WHERE case_id=? AND status=?", (case_id, LEASE_ACTIVE)).fetchone()

    def _reap_case_lease(self, connection, case_id: str, now: str) -> None:
        """把已超时的活动租约标记回收；未进入复核的案例回到待领取。"""

        lease = self._active_lease(connection, case_id)
        if lease is None or lease["expires_at"] > now:
            return
        connection.execute("UPDATE leases SET status=? WHERE lease_id=?", (LEASE_EXPIRED, lease["lease_id"]))
        case = connection.execute("SELECT status FROM defect_cases WHERE case_id=?", (case_id,)).fetchone()
        if case["status"] == CASE_LEASED:
            connection.execute("UPDATE defect_cases SET status=? WHERE case_id=?", (CASE_OPEN, case_id))
        append_event(connection, actor_id="system", action="lease.expired",
                     resource_type="lease", resource_id=lease["lease_id"],
                     detail={"case_id": case_id, "trainee_id": lease["trainee_id"],
                             "expires_at": lease["expires_at"], "reason": "租约超时回收"},
                     occurred_at=now)

    def _revision_row(self, connection, case_id: str, revision_no: int):
        row = connection.execute(
            "SELECT * FROM case_revisions WHERE case_id=? AND revision_no=?",
            (case_id, revision_no)).fetchone()
        if row is None:
            raise NotFoundError("处置修订不存在")
        return row

    # ---- 资料登记 ----

    def register_vehicle(self, *, request_id: str, actor_id: str, vehicle_id: str,
                         site_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "vehicle_id": vehicle_id, "site_id": site_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            vehicle_id = self._identifier(vehicle_id, "vehicle_id")
            name = self._text(name, "name")
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            self._require_org(actor, site["organization_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO vehicles(vehicle_id,site_id,name,created_at) VALUES(?,?,?,?)",
                        (vehicle_id, site_id, name, self._now()))
                except Exception as exc:
                    raise ConflictError("车辆编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="vehicle.registered",
                             resource_type="vehicle", resource_id=vehicle_id,
                             detail={"site_id": site_id, "name": name}, occurred_at=self._now())
                return "vehicle", vehicle_id, {"vehicle_id": vehicle_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="defects.register_vehicle", payload=payload, create=create)

    def register_station(self, *, request_id: str, actor_id: str, station_id: str,
                         site_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "station_id": station_id, "site_id": site_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            station_id = self._identifier(station_id, "station_id")
            name = self._text(name, "name")
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")
            self._require_org(actor, site["organization_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO stations(station_id,site_id,name,created_at) VALUES(?,?,?,?)",
                        (station_id, site_id, name, self._now()))
                except Exception as exc:
                    raise ConflictError("工位编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="station.registered",
                             resource_type="station", resource_id=station_id,
                             detail={"site_id": site_id, "name": name}, occurred_at=self._now())
                return "station", station_id, {"station_id": station_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="defects.register_station", payload=payload, create=create)

    def register_check_item(self, *, request_id: str, actor_id: str, item_id: str, system: str,
                            title: str, safety_critical: bool, criteria: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "item_id": item_id, "system": system, "title": title,
                   "safety_critical": safety_critical, "criteria": criteria}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            item_id = self._identifier(item_id, "item_id")
            if system not in SYSTEMS:
                raise ValidationError("system 必须是 door、brake 或 traction")
            title = self._text(title, "title")
            critical = self._flag(safety_critical, "safety_critical")
            criteria = self._text(criteria, "criteria", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO check_items(item_id,system,title,safety_critical,criteria,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (item_id, system, title, int(critical), criteria, self._now()))
                except Exception as exc:
                    raise ConflictError("检查项编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="check_item.registered",
                             resource_type="check_item", resource_id=item_id,
                             detail={"system": system, "title": title, "safety_critical": critical},
                             occurred_at=self._now())
                return "check_item", item_id, {"item_id": item_id, "safety_critical": critical}

            return self._idempotent(connection, request_id=request_id,
                                    action="defects.register_check_item", payload=payload, create=create)

    def record_measurement(self, *, request_id: str, actor_id: str, measurement_id: str,
                           vehicle_id: str, item_id: str, value: str, unit: str,
                           passed: bool) -> WriteReceipt:
        payload = {"actor_id": actor_id, "measurement_id": measurement_id, "vehicle_id": vehicle_id,
                   "item_id": item_id, "value": value, "unit": unit, "passed": passed}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            measurement_id = self._identifier(measurement_id, "measurement_id")
            vehicle = connection.execute("SELECT * FROM vehicles WHERE vehicle_id=?", (vehicle_id,)).fetchone()
            if vehicle is None:
                raise NotFoundError("车辆不存在")
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (vehicle["site_id"],)).fetchone()
            self._require_org(actor, site["organization_id"])
            if connection.execute("SELECT 1 FROM check_items WHERE item_id=?", (item_id,)).fetchone() is None:
                raise NotFoundError("检查项不存在")
            value = self._text(str(value), "value", 100)
            unit = self._text(unit, "unit", 30)
            passed_flag = self._flag(passed, "passed")
            content_hash = digest({"vehicle_id": vehicle_id, "item_id": item_id, "value": value,
                                   "unit": unit, "passed": passed_flag})

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM measurements WHERE measurement_id=?", (measurement_id,)).fetchone()
                if existing:
                    if existing["payload_hash"] != content_hash:
                        raise ConflictError("同一测量编号已经登记不同内容")
                    return "measurement", measurement_id, {"measurement_id": measurement_id}
                connection.execute(
                    "INSERT INTO measurements(measurement_id,vehicle_id,item_id,value,unit,passed,payload_hash,"
                    "measured_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (measurement_id, vehicle_id, item_id, value, unit, int(passed_flag),
                     content_hash, actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="measurement.recorded",
                             resource_type="measurement", resource_id=measurement_id,
                             detail={"vehicle_id": vehicle_id, "item_id": item_id, "value": value,
                                     "unit": unit, "passed": passed_flag},
                             occurred_at=self._now())
                return "measurement", measurement_id, {"measurement_id": measurement_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="defects.record_measurement", payload=payload, create=create)

    def register_component_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                                 part_number: str, description: str, quantity: int) -> WriteReceipt:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "part_number": part_number,
                   "description": description, "quantity": quantity}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch_id = self._identifier(batch_id, "batch_id")
            part_number = self._identifier(part_number, "part_number")
            description = self._text(description, "description", 500)
            quantity = self._count(quantity, "quantity")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO component_batches(batch_id,part_number,description,quantity,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (batch_id, part_number, description, quantity, self._now()))
                except Exception as exc:
                    raise ConflictError("部件批次编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="component_batch.registered",
                             resource_type="component_batch", resource_id=batch_id,
                             detail={"part_number": part_number, "quantity": quantity},
                             occurred_at=self._now())
                return "component_batch", batch_id, {"batch_id": batch_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="defects.register_component_batch", payload=payload, create=create)

    # ---- 缺陷案例 ----

    def open_case(self, *, request_id: str, actor_id: str, case_id: str, vehicle_id: str,
                  station_id: str, system: str, title: str, check_item_ids: list[str],
                  measurement_ids: list[str] | None = None,
                  safety_critical: bool = False) -> WriteReceipt:
        measurement_ids = measurement_ids or []
        payload = {"actor_id": actor_id, "case_id": case_id, "vehicle_id": vehicle_id,
                   "station_id": station_id, "system": system, "title": title,
                   "check_item_ids": check_item_ids, "measurement_ids": measurement_ids,
                   "safety_critical": safety_critical}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            case_id = self._identifier(case_id, "case_id")
            if system not in SYSTEMS:
                raise ValidationError("system 必须是 door、brake 或 traction")
            title = self._text(title, "title")
            vehicle = connection.execute("SELECT * FROM vehicles WHERE vehicle_id=?", (vehicle_id,)).fetchone()
            if vehicle is None:
                raise NotFoundError("车辆不存在")
            station = connection.execute("SELECT * FROM stations WHERE station_id=?", (station_id,)).fetchone()
            if station is None:
                raise NotFoundError("训练工位不存在")
            if station["site_id"] != vehicle["site_id"]:
                raise ValidationError("车辆与训练工位不在同一场所")
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (vehicle["site_id"],)).fetchone()
            self._require_org(actor, site["organization_id"])
            item_ids = self._identifier_list(check_item_ids, "check_item_ids")
            if not item_ids:
                raise ValidationError("check_item_ids 不能为空")
            measure_ids = self._identifier_list(measurement_ids, "measurement_ids")
            critical = self._flag(safety_critical, "safety_critical")
            for item_id in item_ids:
                item = connection.execute("SELECT * FROM check_items WHERE item_id=?", (item_id,)).fetchone()
                if item is None:
                    raise NotFoundError(f"检查项不存在: {item_id}")
                if item["safety_critical"]:
                    critical = True
            for measure_id in measure_ids:
                measurement = connection.execute(
                    "SELECT * FROM measurements WHERE measurement_id=?", (measure_id,)).fetchone()
                if measurement is None:
                    raise NotFoundError(f"测量记录不存在: {measure_id}")
                if measurement["vehicle_id"] != vehicle_id:
                    raise ValidationError(f"测量记录 {measure_id} 不属于该车辆")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM defect_cases WHERE case_id=?", (case_id,)).fetchone():
                    raise ConflictError("案例编号已经存在")
                now = self._now()
                connection.execute(
                    "INSERT INTO defect_cases(case_id,vehicle_id,station_id,system,title,safety_critical,status,"
                    "version,current_revision,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (case_id, vehicle_id, station_id, system, title, int(critical), CASE_OPEN,
                     1, 0, actor_id, now))
                for item_id in dict.fromkeys(item_ids):
                    connection.execute(
                        "INSERT INTO case_items(case_id,version,item_id) VALUES(?,?,?)", (case_id, 1, item_id))
                for measure_id in dict.fromkeys(measure_ids):
                    connection.execute(
                        "INSERT INTO case_measurements(case_id,version,measurement_id) VALUES(?,?,?)",
                        (case_id, 1, measure_id))
                append_event(connection, actor_id=actor_id, action="case.opened",
                             resource_type="defect_case", resource_id=case_id,
                             detail={"vehicle_id": vehicle_id, "station_id": station_id, "system": system,
                                     "title": title, "safety_critical": critical, "version": 1,
                                     "check_item_ids": item_ids, "measurement_ids": measure_ids},
                             occurred_at=now)
                return "defect_case", case_id, {"case_id": case_id, "version": 1, "safety_critical": critical}

            return self._idempotent(connection, request_id=request_id,
                                    action="defects.open_case", payload=payload, create=create)

    def update_case_version(self, *, request_id: str, actor_id: str, case_id: str,
                            add_check_item_ids: list[str] | None = None,
                            add_measurement_ids: list[str] | None = None,
                            note: str) -> WriteReceipt:
        add_check_item_ids = add_check_item_ids or []
        add_measurement_ids = add_measurement_ids or []
        payload = {"actor_id": actor_id, "case_id": case_id, "add_check_item_ids": add_check_item_ids,
                   "add_measurement_ids": add_measurement_ids, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            case_id = self._identifier(case_id, "case_id")
            case = self._case_row(connection, case_id)
            self._require_org(actor, self._case_org(connection, case_id))
            note = self._text(note, "note", 500)
            item_ids = self._identifier_list(add_check_item_ids, "add_check_item_ids")
            measure_ids = self._identifier_list(add_measurement_ids, "add_measurement_ids")
            if not item_ids and not measure_ids:
                raise ValidationError("升版必须至少新增一个检查项或测量记录")
            critical = bool(case["safety_critical"])
            for item_id in item_ids:
                item = connection.execute("SELECT * FROM check_items WHERE item_id=?", (item_id,)).fetchone()
                if item is None:
                    raise NotFoundError(f"检查项不存在: {item_id}")
                if item["safety_critical"]:
                    critical = True
            for measure_id in measure_ids:
                measurement = connection.execute(
                    "SELECT * FROM measurements WHERE measurement_id=?", (measure_id,)).fetchone()
                if measurement is None:
                    raise NotFoundError(f"测量记录不存在: {measure_id}")
                if measurement["vehicle_id"] != case["vehicle_id"]:
                    raise ValidationError(f"测量记录 {measure_id} 不属于该车辆")

            def create() -> tuple[str, str, dict[str, Any]]:
                current = self._case_row(connection, case_id)
                if current["status"] != CASE_OPEN:
                    raise ConflictError("仅待领取状态的案例可以升版")
                existing_items = {row["item_id"] for row in connection.execute(
                    "SELECT item_id FROM case_items WHERE case_id=? AND version=?",
                    (case_id, current["version"]))}
                existing_measures = {row["measurement_id"] for row in connection.execute(
                    "SELECT measurement_id FROM case_measurements WHERE case_id=? AND version=?",
                    (case_id, current["version"]))}
                new_items = [item for item in dict.fromkeys(item_ids) if item not in existing_items]
                new_measures = [item for item in dict.fromkeys(measure_ids) if item not in existing_measures]
                if not new_items and not new_measures:
                    raise ValidationError("新增内容已包含在当前版本中")
                old_version = current["version"]
                new_version = old_version + 1
                connection.execute(
                    "INSERT INTO case_items(case_id,version,item_id) "
                    "SELECT ?,?,item_id FROM case_items WHERE case_id=? AND version=?",
                    (case_id, new_version, case_id, old_version))
                connection.execute(
                    "INSERT INTO case_measurements(case_id,version,measurement_id) "
                    "SELECT ?,?,measurement_id FROM case_measurements WHERE case_id=? AND version=?",
                    (case_id, new_version, case_id, old_version))
                for item_id in new_items:
                    connection.execute(
                        "INSERT INTO case_items(case_id,version,item_id) VALUES(?,?,?)",
                        (case_id, new_version, item_id))
                for measure_id in new_measures:
                    connection.execute(
                        "INSERT INTO case_measurements(case_id,version,measurement_id) VALUES(?,?,?)",
                        (case_id, new_version, measure_id))
                connection.execute(
                    "UPDATE defect_cases SET version=?, safety_critical=? WHERE case_id=?",
                    (new_version, int(critical), case_id))
                append_event(connection, actor_id=actor_id, action="case.versioned",
                             resource_type="defect_case", resource_id=case_id,
                             detail={"from_version": old_version, "to_version": new_version,
                                     "added_check_items": new_items, "added_measurements": new_measures,
                                     "note": note, "safety_critical": critical},
                             occurred_at=self._now())
                return "defect_case", case_id, {"case_id": case_id, "version": new_version,
                                                "safety_critical": critical}

            return self._idempotent(connection, request_id=request_id,
                                    action="defects.update_case_version", payload=payload, create=create)

    # ---- 租约与处置 ----

    def claim_case(self, *, request_id: str, actor_id: str, case_id: str,
                   duration_minutes: int) -> WriteReceipt:
        if isinstance(duration_minutes, bool) or not isinstance(duration_minutes, int) \
                or not 1 <= duration_minutes <= MAX_LEASE_MINUTES:
            raise ValidationError(f"duration_minutes 必须是 1 到 {MAX_LEASE_MINUTES} 之间的整数")
        payload = {"actor_id": actor_id, "case_id": case_id, "duration_minutes": duration_minutes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "trainee")
            case_id = self._identifier(case_id, "case_id")
            self._case_row(connection, case_id)
            self._require_org(actor, self._case_org(connection, case_id))

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                self._reap_case_lease(connection, case_id, now)
                case = self._case_row(connection, case_id)
                if case["status"] != CASE_OPEN:
                    raise ConflictError("案例当前不可领取")
                expires_at = self._format(self.clock.now() + timedelta(minutes=duration_minutes))
                lease_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO leases(lease_id,case_id,trainee_id,acquired_at,expires_at,status) "
                    "VALUES(?,?,?,?,?,?)",
                    (lease_id, case_id, actor_id, now, expires_at, LEASE_ACTIVE))
                connection.execute("UPDATE defect_cases SET status=? WHERE case_id=?", (CASE_LEASED, case_id))
                append_event(connection, actor_id=actor_id, action="lease.acquired",
                             resource_type="lease", resource_id=lease_id,
                             detail={"case_id": case_id, "trainee_id": actor_id,
                                     "duration_minutes": duration_minutes, "expires_at": expires_at},
                             occurred_at=now)
                return "lease", lease_id, {"lease_id": lease_id, "case_id": case_id,
                                           "expires_at": expires_at}

            return self._idempotent(connection, request_id=request_id,
                                    action="defects.claim_case", payload=payload, create=create)

    def submit_disposition(self, *, request_id: str, actor_id: str, case_id: str,
                           diagnosis: str, isolation: str, retest: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "case_id": case_id, "diagnosis": diagnosis,
                   "isolation": isolation, "retest": retest}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "trainee")
            case_id = self._identifier(case_id, "case_id")
            self._case_row(connection, case_id)
            self._require_org(actor, self._case_org(connection, case_id))
            diagnosis = self._text(diagnosis, "diagnosis", 2000)
            isolation = self._text(isolation, "isolation", 2000)
            retest = self._text(retest, "retest", 2000)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                self._reap_case_lease(connection, case_id, now)
                case = self._case_row(connection, case_id)
                lease = self._active_lease(connection, case_id)
                if lease is None:
                    raise PermissionDenied("没有持有该案例的有效租约")
                if lease["trainee_id"] != actor.actor_id:
                    raise PermissionDenied("仅租约持有人可以提交处置")
                if case["status"] != CASE_LEASED:
                    raise ConflictError("当前没有可提交的处置窗口")
                revision_no = case["current_revision"] + 1
                connection.execute(
                    "INSERT INTO case_revisions(case_id,revision_no,case_version,submitted_by,diagnosis,"
                    "isolation,retest,status,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (case_id, revision_no, case["version"], actor_id, diagnosis, isolation, retest,
                     REV_IN_REVIEW, now))
                connection.execute(
                    "UPDATE defect_cases SET status=?, current_revision=? WHERE case_id=?",
                    (CASE_IN_REVIEW, revision_no, case_id))
                append_event(connection, actor_id=actor_id, action="disposition.submitted",
                             resource_type="case_revision", resource_id=f"{case_id}#{revision_no}",
                             detail={"case_id": case_id, "revision_no": revision_no,
                                     "case_version": case["version"],
                                     "diagnosis_hash": digest(diagnosis),
                                     "isolation_hash": digest(isolation),
                                     "retest_hash": digest(retest)},
                             occurred_at=now)
                return "case_revision", f"{case_id}#{revision_no}", {
                    "case_id": case_id, "revision_no": revision_no, "case_version": case["version"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="defects.submit_disposition", payload=payload, create=create)

    def review_disposition(self, *, request_id: str, actor_id: str, case_id: str,
                           revision_no: int, decision: str, note: str) -> WriteReceipt:
        if decision not in ("approve", "reject"):
            raise ValidationError("decision 必须是 approve 或 reject")
        payload = {"actor_id": actor_id, "case_id": case_id, "revision_no": revision_no,
                   "decision": decision, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "instructor")
            case_id = self._identifier(case_id, "case_id")
            revision_no = self._count(revision_no, "revision_no")
            self._case_row(connection, case_id)
            self._require_org(actor, self._case_org(connection, case_id))
            note = self._text(note, "note", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                self._reap_case_lease(connection, case_id, now)
                case = self._case_row(connection, case_id)
                revision = self._revision_row(connection, case_id, revision_no)
                if case["status"] != CASE_IN_REVIEW or revision["status"] != REV_IN_REVIEW \
                        or case["current_revision"] != revision_no:
                    raise ConflictError("当前没有待复核的修订")
                if revision["submitted_by"] == actor.actor_id:
                    raise PermissionDenied("复核教员不能与提交人相同")
                resource_id = f"{case_id}#{revision_no}"
                if decision == "reject":
                    connection.execute(
                        "UPDATE case_revisions SET status=?, reviewed_by=?, reviewed_at=?, review_note=? "
                        "WHERE case_id=? AND revision_no=?",
                        (REV_REJECTED, actor_id, now, note, case_id, revision_no))
                    lease = self._active_lease(connection, case_id)
                    next_status = CASE_LEASED if lease else CASE_OPEN
                    connection.execute("UPDATE defect_cases SET status=? WHERE case_id=?",
                                       (next_status, case_id))
                    append_event(connection, actor_id=actor_id, action="disposition.rejected",
                                 resource_type="case_revision", resource_id=resource_id,
                                 detail={"case_id": case_id, "revision_no": revision_no,
                                         "case_version": revision["case_version"], "note": note,
                                         "case_status": next_status},
                                 occurred_at=now)
                    return "case_revision", resource_id, {"case_id": case_id, "revision_no": revision_no,
                                                          "decision": "reject", "case_status": next_status}
                if case["safety_critical"]:
                    connection.execute(
                        "UPDATE case_revisions SET status=?, reviewed_by=?, reviewed_at=?, review_note=? "
                        "WHERE case_id=? AND revision_no=?",
                        (REV_AWAITING_SECOND, actor_id, now, note, case_id, revision_no))
                    connection.execute("UPDATE defect_cases SET status=? WHERE case_id=?",
                                       (CASE_AWAITING_SECOND, case_id))
                    append_event(connection, actor_id=actor_id, action="disposition.first_signed",
                                 resource_type="case_revision", resource_id=resource_id,
                                 detail={"case_id": case_id, "revision_no": revision_no,
                                         "case_version": revision["case_version"], "note": note},
                                 occurred_at=now)
                    return "case_revision", resource_id, {"case_id": case_id, "revision_no": revision_no,
                                                          "decision": "approve",
                                                          "case_status": CASE_AWAITING_SECOND}
                connection.execute(
                    "UPDATE case_revisions SET status=?, reviewed_by=?, reviewed_at=?, review_note=? "
                    "WHERE case_id=? AND revision_no=?",
                    (REV_CLOSED, actor_id, now, note, case_id, revision_no))
                connection.execute(
                    "UPDATE defect_cases SET status=?, closed_at=?, closed_revision=?, closed_case_version=? "
                    "WHERE case_id=?",
                    (CASE_CLOSED, now, revision_no, revision["case_version"], case_id))
                connection.execute("UPDATE leases SET status=? WHERE case_id=? AND status=?",
                                   (LEASE_CLOSED, case_id, LEASE_ACTIVE))
                append_event(connection, actor_id=actor_id, action="case.closed",
                             resource_type="defect_case", resource_id=case_id,
                             detail={"revision_no": revision_no, "case_version": revision["case_version"],
                                     "signed_by": [actor_id], "note": note},
                             occurred_at=now)
                return "case_revision", resource_id, {"case_id": case_id, "revision_no": revision_no,
                                                      "decision": "approve", "case_status": CASE_CLOSED}

            return self._idempotent(connection, request_id=request_id,
                                    action="defects.review_disposition", payload=payload, create=create)

    def cosign_closure(self, *, request_id: str, actor_id: str, case_id: str,
                       revision_no: int, note: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "case_id": case_id, "revision_no": revision_no, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "instructor")
            case_id = self._identifier(case_id, "case_id")
            revision_no = self._count(revision_no, "revision_no")
            self._case_row(connection, case_id)
            self._require_org(actor, self._case_org(connection, case_id))
            note = self._text(note, "note", 1000)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                case = self._case_row(connection, case_id)
                revision = self._revision_row(connection, case_id, revision_no)
                if case["status"] != CASE_AWAITING_SECOND or revision["status"] != REV_AWAITING_SECOND \
                        or case["current_revision"] != revision_no:
                    raise ConflictError("当前没有待二次签署的修订")
                if revision["reviewed_by"] == actor.actor_id:
                    raise PermissionDenied("二次签署必须由不同教员完成")
                if revision["submitted_by"] == actor.actor_id:
                    raise PermissionDenied("签署教员不能与提交人相同")
                connection.execute(
                    "UPDATE case_revisions SET status=?, second_signed_by=?, second_signed_at=?, second_note=? "
                    "WHERE case_id=? AND revision_no=?",
                    (REV_CLOSED, actor_id, now, note, case_id, revision_no))
                connection.execute(
                    "UPDATE defect_cases SET status=?, closed_at=?, closed_revision=?, closed_case_version=? "
                    "WHERE case_id=?",
                    (CASE_CLOSED, now, revision_no, revision["case_version"], case_id))
                connection.execute("UPDATE leases SET status=? WHERE case_id=? AND status=?",
                                   (LEASE_CLOSED, case_id, LEASE_ACTIVE))
                append_event(connection, actor_id=actor_id, action="case.closed",
                             resource_type="defect_case", resource_id=case_id,
                             detail={"revision_no": revision_no, "case_version": revision["case_version"],
                                     "signed_by": [revision["reviewed_by"], actor_id], "note": note},
                             occurred_at=now)
                return "case_revision", f"{case_id}#{revision_no}", {
                    "case_id": case_id, "revision_no": revision_no, "case_status": CASE_CLOSED}

            return self._idempotent(connection, request_id=request_id,
                                    action="defects.cosign_closure", payload=payload, create=create)

    def record_component_movement(self, *, request_id: str, actor_id: str, case_id: str,
                                  batch_id: str, action: str, quantity: int,
                                  note: str) -> WriteReceipt:
        if action not in MOVEMENT_ACTIONS:
            raise ValidationError("action 必须是 install 或 remove")
        payload = {"actor_id": actor_id, "case_id": case_id, "batch_id": batch_id,
                   "action": action, "quantity": quantity, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "trainee", "operator", "admin")
            case_id = self._identifier(case_id, "case_id")
            self._case_row(connection, case_id)
            self._require_org(actor, self._case_org(connection, case_id))
            batch_id = self._identifier(batch_id, "batch_id")
            if connection.execute("SELECT 1 FROM component_batches WHERE batch_id=?",
                                  (batch_id,)).fetchone() is None:
                raise NotFoundError("部件批次不存在")
            quantity = self._count(quantity, "quantity")
            note = self._text(note, "note", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                self._reap_case_lease(connection, case_id, now)
                case = self._case_row(connection, case_id)
                if case["status"] == CASE_CLOSED:
                    raise ConflictError("案例已关闭，不能登记部件流转")
                if actor.role == "trainee":
                    lease = self._active_lease(connection, case_id)
                    if lease is None or lease["trainee_id"] != actor.actor_id:
                        raise PermissionDenied("学员需持有该案例的有效租约才能登记部件流转")
                movement_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO component_movements(movement_id,batch_id,case_id,revision_no,vehicle_id,"
                    "action,quantity,note,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (movement_id, batch_id, case_id, case["current_revision"], case["vehicle_id"],
                     action, quantity, note, actor_id, now))
                append_event(connection, actor_id=actor_id, action="component.moved",
                             resource_type="component_movement", resource_id=movement_id,
                             detail={"batch_id": batch_id, "case_id": case_id, "action": action,
                                     "quantity": quantity, "note": note},
                             occurred_at=now)
                return "component_movement", movement_id, {"movement_id": movement_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="defects.record_component_movement", payload=payload, create=create)

    def reap_expired_leases(self, *, actor_id: str) -> dict[str, Any]:
        """回收全部已超时租约，返回确定性的回收清单。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            now = self._now()
            rows = connection.execute(
                "SELECT lease_id, case_id FROM leases WHERE status=? AND expires_at<=? ORDER BY lease_id",
                (LEASE_ACTIVE, now)).fetchall()
            reaped = []
            for row in rows:
                self._reap_case_lease(connection, row["case_id"], now)
                reaped.append(row["lease_id"])
            return {"reaped_lease_ids": reaped, "reaped": len(reaped), "reaped_at": now}

    # ---- 报告 ----

    def _actor_names(self, connection, actor_ids: Iterable[str]) -> dict[str, str]:
        names: dict[str, str] = {}
        for actor_id in actor_ids:
            if actor_id is None or actor_id in names:
                continue
            row = connection.execute("SELECT display_name FROM actors WHERE actor_id=?",
                                     (actor_id,)).fetchone()
            names[actor_id] = row["display_name"] if row else actor_id
        return names

    def _responsible(self, connection, case, lease, now: str) -> dict[str, Any]:
        """按案例状态推导当前责任人，结果只取决于已持久化状态与时钟。"""

        status = case["status"]
        if status == CASE_CLOSED:
            return {"role": None, "actor_id": None, "reason": "案例已关闭"}
        if status == CASE_OPEN:
            return {"role": None, "actor_id": None, "reason": "待学员领取"}
        if status == CASE_LEASED:
            if lease and lease["expires_at"] > now:
                return {"role": "trainee", "actor_id": lease["trainee_id"],
                        "reason": "租约处置中", "lease_expires_at": lease["expires_at"]}
            return {"role": None, "actor_id": None, "reason": "租约已超时待回收"}
        revision = connection.execute(
            "SELECT * FROM case_revisions WHERE case_id=? AND revision_no=?",
            (case["case_id"], case["current_revision"])).fetchone()
        if status == CASE_IN_REVIEW:
            return {"role": "instructor", "actor_id": None, "reason": "待教员复核",
                    "excluded_actor_ids": [revision["submitted_by"]]}
        return {"role": "instructor", "actor_id": None, "reason": "待二次签署",
                "excluded_actor_ids": [revision["submitted_by"], revision["reviewed_by"]]}

    def case_report(self, case_id: str) -> dict[str, Any]:
        """还原单个案例的版本、修订、租约、部件流转和判断依据。"""

        connection = self.database.connection
        case = connection.execute("SELECT * FROM defect_cases WHERE case_id=?", (case_id,)).fetchone()
        if case is None:
            raise NotFoundError("缺陷案例不存在")
        now = self._now()
        vehicle = connection.execute("SELECT * FROM vehicles WHERE vehicle_id=?",
                                     (case["vehicle_id"],)).fetchone()
        station = connection.execute("SELECT * FROM stations WHERE station_id=?",
                                     (case["station_id"],)).fetchone()
        versions = []
        for version in range(1, case["version"] + 1):
            items = connection.execute(
                "SELECT ci.item_id, ci.system, ci.title, ci.safety_critical, ci.criteria "
                "FROM case_items JOIN check_items ci ON ci.item_id=case_items.item_id "
                "WHERE case_items.case_id=? AND case_items.version=? ORDER BY ci.item_id",
                (case_id, version)).fetchall()
            measurements = connection.execute(
                "SELECT m.measurement_id, m.item_id, m.value, m.unit, m.passed, m.measured_by, m.created_at "
                "FROM case_measurements JOIN measurements m ON m.measurement_id=case_measurements.measurement_id "
                "WHERE case_measurements.case_id=? AND case_measurements.version=? ORDER BY m.measurement_id",
                (case_id, version)).fetchall()
            versions.append({
                "version": version,
                "check_items": [{"item_id": row["item_id"], "system": row["system"],
                                 "title": row["title"],
                                 "safety_critical": bool(row["safety_critical"]),
                                 "criteria": row["criteria"]} for row in items],
                "measurements": [{"measurement_id": row["measurement_id"], "item_id": row["item_id"],
                                  "value": row["value"], "unit": row["unit"],
                                  "passed": bool(row["passed"]), "measured_by": row["measured_by"],
                                  "created_at": row["created_at"]} for row in measurements]})
        revision_rows = connection.execute(
            "SELECT * FROM case_revisions WHERE case_id=? ORDER BY revision_no", (case_id,)).fetchall()
        lease_rows = connection.execute(
            "SELECT * FROM leases WHERE case_id=? ORDER BY acquired_at, rowid", (case_id,)).fetchall()
        movement_rows = connection.execute(
            "SELECT cm.*, cb.part_number FROM component_movements cm "
            "JOIN component_batches cb ON cb.batch_id=cm.batch_id "
            "WHERE cm.case_id=? ORDER BY cm.created_at, cm.rowid", (case_id,)).fetchall()
        actor_ids = [case["created_by"]]
        for row in revision_rows:
            actor_ids += [row["submitted_by"], row["reviewed_by"], row["second_signed_by"]]
        for row in lease_rows:
            actor_ids.append(row["trainee_id"])
        for row in movement_rows:
            actor_ids.append(row["actor_id"])
        names = self._actor_names(connection, actor_ids)

        def person(actor_id: str | None) -> dict[str, Any] | None:
            if actor_id is None:
                return None
            return {"actor_id": actor_id, "display_name": names.get(actor_id, actor_id)}

        revisions = [{
            "revision_no": row["revision_no"],
            "case_version": row["case_version"],
            "status": row["status"],
            "submitted_by": person(row["submitted_by"]),
            "submitted_at": row["submitted_at"],
            "diagnosis": row["diagnosis"],
            "isolation": row["isolation"],
            "retest": row["retest"],
            "review_note": row["review_note"],
            "reviewed_by": person(row["reviewed_by"]),
            "reviewed_at": row["reviewed_at"],
            "second_note": row["second_note"],
            "second_signed_by": person(row["second_signed_by"]),
            "second_signed_at": row["second_signed_at"],
        } for row in revision_rows]
        leases = [{
            "lease_id": row["lease_id"],
            "trainee": person(row["trainee_id"]),
            "acquired_at": row["acquired_at"],
            "expires_at": row["expires_at"],
            "status": row["status"],
        } for row in lease_rows]
        movements = [{
            "movement_id": row["movement_id"],
            "batch_id": row["batch_id"],
            "part_number": row["part_number"],
            "action": row["action"],
            "quantity": row["quantity"],
            "revision_no": row["revision_no"],
            "note": row["note"],
            "actor": person(row["actor_id"]),
            "created_at": row["created_at"],
        } for row in movement_rows]
        lease = self._active_lease(connection, case_id)
        responsible = self._responsible(connection, case, lease, now)
        if responsible.get("actor_id"):
            responsible["actor_name"] = names.get(responsible["actor_id"], responsible["actor_id"])
        resource_ids = [case_id]
        resource_ids += [row["lease_id"] for row in lease_rows]
        resource_ids += [row["movement_id"] for row in movement_rows]
        resource_ids += [f"{case_id}#{row['revision_no']}" for row in revision_rows]
        placeholders = ",".join("?" for _ in resource_ids)
        audit_rows = connection.execute(
            f"SELECT * FROM audit_events WHERE resource_id IN ({placeholders}) ORDER BY sequence",
            resource_ids).fetchall()
        audit_events = [{"sequence": row["sequence"], "event_id": row["event_id"],
                         "actor_id": row["actor_id"], "action": row["action"],
                         "resource_type": row["resource_type"], "resource_id": row["resource_id"],
                         "detail": json.loads(row["detail_json"]), "event_hash": row["event_hash"],
                         "occurred_at": row["occurred_at"]} for row in audit_rows]
        return {
            "generated_at": now,
            "case": {
                "case_id": case["case_id"],
                "title": case["title"],
                "system": case["system"],
                "vehicle_id": case["vehicle_id"],
                "vehicle_name": vehicle["name"] if vehicle else None,
                "station_id": case["station_id"],
                "station_name": station["name"] if station else None,
                "safety_critical": bool(case["safety_critical"]),
                "status": case["status"],
                "version": case["version"],
                "current_revision": case["current_revision"],
                "created_by": person(case["created_by"]),
                "created_at": case["created_at"],
                "closed_at": case["closed_at"],
                "closed_revision": case["closed_revision"],
                "closed_case_version": case["closed_case_version"],
            },
            "versions": versions,
            "revisions": revisions,
            "leases": leases,
            "component_movements": movements,
            "responsible": responsible,
            "audit_events": audit_events,
        }

    def pending_report(self, site_id: str | None = None) -> dict[str, Any]:
        """汇总重启后仍然存在的未完工作：待领取、进行中、待回收、待复核、待签署。"""

        connection = self.database.connection
        now = self._now()
        site_filter = ""
        parameters: list[Any] = []
        if site_id:
            site_filter = " AND v.site_id=?"
            parameters.append(site_id)
        base_from = "FROM defect_cases c JOIN vehicles v ON v.vehicle_id=c.vehicle_id "

        def case_brief(row) -> dict[str, Any]:
            return {"case_id": row["case_id"], "title": row["title"], "system": row["system"],
                    "vehicle_id": row["vehicle_id"], "station_id": row["station_id"],
                    "safety_critical": bool(row["safety_critical"]), "version": row["version"]}

        open_cases = [case_brief(row) for row in connection.execute(
            f"SELECT c.* {base_from} WHERE c.status=?{site_filter} ORDER BY c.case_id",
            [CASE_OPEN, *parameters])]
        lease_rows = connection.execute(
            "SELECT l.*, c.title, c.system, c.vehicle_id, c.station_id, c.safety_critical, c.version "
            "FROM leases l JOIN defect_cases c ON c.case_id=l.case_id "
            "JOIN vehicles v ON v.vehicle_id=c.vehicle_id "
            f"WHERE l.status=?{site_filter} ORDER BY l.lease_id",
            [LEASE_ACTIVE, *parameters]).fetchall()
        active_leases = []
        expired_leases = []
        for row in lease_rows:
            entry = {"lease_id": row["lease_id"], "case_id": row["case_id"], "title": row["title"],
                     "trainee_id": row["trainee_id"], "expires_at": row["expires_at"],
                     "vehicle_id": row["vehicle_id"], "station_id": row["station_id"]}
            if row["expires_at"] > now:
                active_leases.append(entry)
            else:
                expired_leases.append(entry)
        awaiting_review = []
        for row in connection.execute(
                f"SELECT c.*, r.submitted_by, r.submitted_at {base_from} "
                "JOIN case_revisions r ON r.case_id=c.case_id AND r.revision_no=c.current_revision "
                f"WHERE c.status=?{site_filter} ORDER BY c.case_id",
                [CASE_IN_REVIEW, *parameters]):
            entry = case_brief(row)
            entry.update({"revision_no": row["current_revision"], "submitted_by": row["submitted_by"],
                          "submitted_at": row["submitted_at"]})
            awaiting_review.append(entry)
        awaiting_second = []
        for row in connection.execute(
                f"SELECT c.*, r.reviewed_by, r.reviewed_at {base_from} "
                "JOIN case_revisions r ON r.case_id=c.case_id AND r.revision_no=c.current_revision "
                f"WHERE c.status=?{site_filter} ORDER BY c.case_id",
                [CASE_AWAITING_SECOND, *parameters]):
            entry = case_brief(row)
            entry.update({"revision_no": row["current_revision"],
                          "first_signed_by": row["reviewed_by"], "first_signed_at": row["reviewed_at"]})
            awaiting_second.append(entry)
        counts = {"open_cases": len(open_cases), "active_leases": len(active_leases),
                  "expired_leases": len(expired_leases), "awaiting_review": len(awaiting_review),
                  "awaiting_second_signature": len(awaiting_second)}
        counts["unfinished"] = sum(counts.values())
        return {"generated_at": now, "site_id": site_id, "open_cases": open_cases,
                "active_leases": active_leases, "expired_leases": expired_leases,
                "awaiting_review": awaiting_review, "awaiting_second_signature": awaiting_second,
                "counts": counts}

    def component_flow(self, batch_id: str) -> dict[str, Any]:
        """还原一个部件批次在案例与车辆之间的全部流转。"""

        connection = self.database.connection
        batch = connection.execute("SELECT * FROM component_batches WHERE batch_id=?",
                                   (batch_id,)).fetchone()
        if batch is None:
            raise NotFoundError("部件批次不存在")
        rows = connection.execute(
            "SELECT cm.*, c.title AS case_title FROM component_movements cm "
            "JOIN defect_cases c ON c.case_id=cm.case_id "
            "WHERE cm.batch_id=? ORDER BY cm.created_at, cm.rowid", (batch_id,)).fetchall()
        names = self._actor_names(connection, [row["actor_id"] for row in rows])
        movements = [{
            "movement_id": row["movement_id"],
            "case_id": row["case_id"],
            "case_title": row["case_title"],
            "vehicle_id": row["vehicle_id"],
            "revision_no": row["revision_no"],
            "action": row["action"],
            "quantity": row["quantity"],
            "note": row["note"],
            "actor": {"actor_id": row["actor_id"],
                      "display_name": names.get(row["actor_id"], row["actor_id"])},
            "created_at": row["created_at"],
        } for row in rows]
        return {"generated_at": self._now(),
                "batch": {"batch_id": batch["batch_id"], "part_number": batch["part_number"],
                          "description": batch["description"], "quantity": batch["quantity"],
                          "created_at": batch["created_at"]},
                "movements": movements, "movement_count": len(movements)}
