"""提供写入接口共用的幂等回执处理。"""

from __future__ import annotations

from typing import Any, Callable

from .audit import canonical_json, digest
from .models import WriteReceipt


def idempotent(connection, *, request_id: str, action: str, payload: dict[str, Any],
               now: Callable[[], str],
               create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
    """相同 request_id 回放原回执，不同内容复用编号时抛出由调用方转换的冲突。

    可能抛出 sqlite3.IntegrityError（request_id 主键竞争），调用方应将其
    转换为 ConflictError。
    """

    payload_hash = digest(payload)
    row = connection.execute(
        "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
    ).fetchone()
    if row:
        if row["action"] != action or row["payload_hash"] != payload_hash:
            from .errors import ConflictError

            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
    resource_type, resource_id, response = create()
    connection.execute(
        "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
        "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (request_id, action, payload_hash, resource_type, resource_id,
         canonical_json(response), now()),
    )
    return WriteReceipt(request_id, resource_type, resource_id, False)
