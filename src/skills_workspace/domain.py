"""定义基础服务允许登记的资料类别。"""

ALLOWED_CATEGORIES = frozenset({
    "institution_profile",
    "venue_registry",
    "resource_registry",
    "participant_assignment",
})


def is_allowed_category(value: str) -> bool:
    return value in ALLOWED_CATEGORIES


# 轨道车辆排故训练覆盖的车辆系统。
TRAIN_SYSTEMS = frozenset({"door", "brake", "traction"})


def is_known_system(value: str) -> bool:
    return value in TRAIN_SYSTEMS
