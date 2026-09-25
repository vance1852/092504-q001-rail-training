# 轨道车辆实训缺陷闭环服务

本项目在技能赛训协作基础服务（机构、操作者、场所、幂等回执、哈希链审计）之上，提供轨道车辆实训中心的缺陷闭环能力：接收标准化检查项、测量记录和部件批次，按车辆与训练工位形成带版本的缺陷案例；学员领取案例获得有期限的处置租约，提交诊断、隔离措施和复测结果后由不同教员复核，退回保留旧修订并生成新修订号；安全关键案例必须经第二名教员二次签署才能关闭。并发领取、超时回收、相同回执重放和不同内容复用编号都返回确定结果，后台接口与命令行报告可还原每次判断依据、部件流转、当前责任人以及重启后的未完工作。

## 目录

- `src/skills_workspace/`：领域模型、SQLite 存储、权限服务、审计链、缺陷闭环服务、HTTP 路由、命令行报告和离线验收；
- `tests/`：核心规则、事务边界、接口路由、并发与端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m skills_workspace.acceptance
```

命令会在临时 SQLite 数据库中登记机构、操作者、场所和领域资料，并执行一条完整缺陷闭环：建档车辆与工位、登记检查项和测量记录、开设安全关键案例、学员领取租约、登记部件流转、两次提交处置（第一次被退回并保留结论）、教员复核、第二名教员二次签署关闭。成功时输出一行 `status` 为 `ok` 且 `case_status` 为 `closed` 的 JSON，并以退出码 `0` 结束。

## 角色

- `admin`：机构管理与全部业务动作；
- `operator`：登记车辆、工位、检查项、测量记录、部件批次，开设与升版案例，回收超时租约；
- `trainee`：领取案例租约、提交处置、在持有租约期间登记部件流转；
- `instructor`：复核处置（不得与提交人相同）、对安全关键案例二次签署（不得与首签教员相同）；
- `reviewer`、`auditor`：基础资料登记与只读查询。

## 缺陷闭环规则

- 案例按车辆与训练工位建档，`version` 表示车辆状态版次，每个版本快照当时的检查项与测量记录；仅待领取状态可以升版，关闭后不可变更；
- 学员领取案例获得有期限租约（`duration_minutes` 限定 1 至 1440），同一案例同一时刻仅允许一个活动租约（数据库部分唯一索引保证）；租约超时后在领取、提交、复核或显式回收时被确定性回收，未进入复核的案例回到待领取；
- 每次提交生成新修订号并记录提交时的案例版本；教员复核必须填写依据，退回保留旧修订结论，学员在租约有效期内可直接再次提交；
- 非安全关键案例经一名教员批准即关闭；安全关键案例批准后进入待二次签署，必须由另一名教员签署才能关闭；
- 所有写接口按 `request_id` 幂等：相同请求返回首次回执（含租约到期时间等明细），不同内容复用编号返回 409；写事务使用 `BEGIN IMMEDIATE` 并按进程内写锁串行，并发领取与并发复核只有一个成功。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills_workspace.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者。除基础登记接口外，缺陷闭环提供：

- `POST /vehicles`、`/stations`、`/check-items`、`/measurements`、`/component-batches`：登记车辆、工位、检查项、测量记录、部件批次；
- `POST /cases`：开设缺陷案例；`POST /cases/{id}/versions`：升版；
- `POST /cases/{id}/claims`：学员领取租约；`POST /cases/{id}/dispositions`：提交处置；
- `POST /cases/{id}/reviews`：教员复核；`POST /cases/{id}/cosign`：二次签署；
- `POST /cases/{id}/movements`：登记部件装机/拆下流转；`POST /leases/reap`：回收超时租约；
- `GET /cases/{id}/report`：案例闭环报告（版本快照、修订结论、租约、部件流转、当前责任人、关联审计事件）；
- `GET /reports/pending?site_id=`：未完工作汇总（待领取、进行中、待回收、待复核、待二次签署）；
- `GET /reports/component-flow?batch_id=`：部件批次流转报告。

服务重启后，SQLite 中的业务状态和审计链继续保留，报告接口可直接还原未完工作。

## 命令行报告

```bash
PYTHONPATH=src python3 -m skills_workspace.report --database skills_workspace.sqlite3 --case-id case-001
PYTHONPATH=src python3 -m skills_workspace.report --database skills_workspace.sqlite3 --batch-id batch-001
PYTHONPATH=src python3 -m skills_workspace.report --database skills_workspace.sqlite3 --pending [--site-id site-001]
```

报告以 JSON 输出，数据直接来自 SQLite，可用于交接班核对与离线审计。
