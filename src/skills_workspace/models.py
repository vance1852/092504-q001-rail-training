"""定义基础服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Site:
    """表示训练或赛事组织下的业务场所。"""

    site_id: str
    organization_id: str
    name: str
    timezone_name: str
    version: int


@dataclass(frozen=True)
class DomainRecord:
    """表示已经持久化的领域资料记录。"""

    record_id: str
    site_id: str
    category: str
    external_key: str
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class Vehicle:
    """登记在某个工位场所下的轨道车辆。"""

    vehicle_id: str
    organization_id: str
    site_id: str
    name: str


@dataclass(frozen=True)
class Workstation:
    """训练场所内的排故工位。"""

    workstation_id: str
    site_id: str
    name: str


@dataclass(frozen=True)
class InspectionItem:
    """标准化检查项，带容差与安全关键标记。"""

    organization_id: str
    item_code: str
    title: str
    system_code: str
    safety_critical: bool
    unit: str
    lower_limit: float | None
    upper_limit: float | None


@dataclass(frozen=True)
class Measurement:
    """一次带判定结果的测量记录。"""

    measurement_id: str
    vehicle_id: str
    workstation_id: str
    item_code: str
    value: float
    unit: str
    in_tolerance: bool
    component_batch_id: str | None
    measured_by: str
    measured_at: str


@dataclass(frozen=True)
class ComponentBatch:
    """可追溯的部件批次。"""

    batch_id: str
    organization_id: str
    part_number: str
    description: str
    quantity: int
    location: str


@dataclass(frozen=True)
class ComponentMovement:
    """部件批次的一次流转记录。"""

    movement_id: str
    batch_id: str
    action: str
    from_location: str
    to_location: str
    actor_id: str
    note: str
    occurred_at: str


@dataclass(frozen=True)
class CaseRevision:
    """缺陷案例的一个不可变修订版本。"""

    revision_id: str
    case_id: str
    revision_no: int
    diagnosis: str
    isolation: str
    retest_result: str
    evidence: list[Any]
    submitted_by: str
    submitted_at: str


@dataclass(frozen=True)
class CaseReview:
    """教员对某个修订版本的复核结论。"""

    review_id: str
    case_id: str
    revision_no: int
    reviewer_id: str
    decision: str
    reason: str
    signed_at: str


@dataclass(frozen=True)
class LeaseView:
    """处置租约的对外视图。"""

    lease_id: str
    case_id: str
    revision_base_no: int
    holder_id: str
    granted_at: str
    expires_at: str
    active: bool
    released_at: str | None
    release_reason: str | None


@dataclass(frozen=True)
class DefectCase:
    """按车辆与工位形成的带版本缺陷案例。"""

    case_id: str
    case_key: str
    organization_id: str
    vehicle_id: str
    workstation_id: str
    item_code: str
    safety_critical: bool
    source_measurement_id: str | None
    status: str
    current_revision_no: int
    lease_holder_id: str | None
    lease_expires_at: str | None
    opened_by: str
    created_at: str
