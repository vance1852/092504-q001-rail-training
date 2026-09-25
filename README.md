# 轨道车辆实训缺陷闭环服务

本项目面向轨道车辆实训中心，解决车门、制动和牵引系统排故训练中纸表分散、交接班重复拆检、
无法说明学员最终修复的是哪一版车辆状态等问题。服务接收标准化检查项、测量记录和部件批次，
按车辆与训练工位形成带版本的缺陷案例；学员凭有期限的处置租约作业，提交诊断、隔离措施和
复测结果后由**不同教员**复核，退回生成**新修订而不覆盖旧结论**，安全关键项必须经
**二次签署**才能关闭。并发领取、超时回收、相同回执重放、不同内容复用编号都有确定结果，
所有判断依据进入哈希串联审计链，重启后未完工作、当前责任人和部件流转均可还原。

## 能力概览

- 基础登记：机构、操作者（admin/operator/reviewer/auditor）、场所、领域资料；
- 实训资源：车辆、训练工位、标准化检查项（车门/制动/牵引，含容差上下限与安全关键标记）；
- 测量记录：按检查项自动判定是否超差，可关联部件批次；
- 部件追溯：批次建档、入库/转移/消耗，库存与位置随流转链更新；
- 缺陷案例：按车辆+工位+稳定编号开立，可引用超差测量作为开案依据；
- 处置租约：领取即获得带到期时间的独占租约，到期由系统回收，支持主动释放；
- 修订复核：诊断/隔离/复测结果作为不可变修订；退回后再提交形成新版本，历史结论保留；
- 安全关键：首签通过后进入等待二次签署，第二名不同教员签署才关闭；
- 确定性行为：相同 `request_id`+相同内容回放原回执；同编号不同内容返回 409；
  同一案例并发领取只有一人成功（数据库唯一索引 + 立即事务）；
- 可观测：审计哈希链、案例判断依据、部件流转、当前责任人、未完工作清单。

## 目录

- `src/skills_workspace/`：领域模型、SQLite 存储、基础服务与缺陷闭环服务、审计链、
  HTTP 路由、命令行报告和离线验收；
- `tests/`：核心规则、事务边界、并发租约、接口路由和端到端验收测试。

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

命令在临时 SQLite 数据库中完成登记、测量、部件流转、租约超时回收、修订退回、
双教员签署和模拟重启，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills_workspace.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。所有写入接口通过 `X-Actor-Id` 标识操作者并要求 `request_id`：
相同请求回放原回执（HTTP 200），首次成功返回 201，同编号不同内容返回 409。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/organizations` `/actors` `/sites` `/domain-records` | 基础登记 |
| POST | `/vehicles` `/workstations` | 车辆与训练工位 |
| POST/GET | `/inspection-items` | 标准化检查项登记/列出 |
| POST | `/measurements` | 测量记录（返回 `in_tolerance` 判定） |
| POST | `/component-batches` | 部件批次建档 |
| POST | `/component-movements` | 入库 receive / 转移 transfer / 消耗 consume |
| GET | `/component-batches/{batch_id}` | 部件批次与完整流转链 |
| POST | `/defect-cases` | 开立缺陷案例（可带 `measurement_id`） |
| POST | `/defect-cases/{case_id}/claim` | 领取带期限的处置租约 |
| POST | `/defect-cases/{case_id}/release` | 主动释放租约 |
| POST | `/defect-cases/{case_id}/revisions` | 提交诊断/隔离/复测修订 |
| POST | `/defect-cases/{case_id}/reviews` | 教员复核 `approved` 或 `returned` |
| POST | `/defect-cases/{case_id}/countersign` | 安全关键项二次签署 |
| POST | `/lease-expirations` | 立即执行到期租约回收 |
| GET | `/defect-cases/unfinished` | 未完工作与当前责任人 |
| GET | `/defect-cases/{case_id}` | 案例闭环报告 |
| GET | `/audit-events` | 审计事件（哈希链） |

### 闭环状态

`open → in_progress → in_review → closed`（普通项单签关闭）；
安全关键项为 `in_review → awaiting_countersign → closed`（二次签署）；
复核退回为 `returned`，重新领取后提交新修订。租约到期自动回到 `open`。

## 命令行报告

```bash
# 单个案例：每次判断依据、修订/签署、租约、当前责任人
PYTHONPATH=src python3 -m skills_workspace.reporting --database skills_workspace.sqlite3 case <case_id>
# 重启后仍未关闭的未完工作
PYTHONPATH=src python3 -m skills_workspace.reporting --database skills_workspace.sqlite3 unfinished
# 部件批次流转链
PYTHONPATH=src python3 -m skills_workspace.reporting --database skills_workspace.sqlite3 component <batch_id>
# 审计哈希链
PYTHONPATH=src python3 -m skills_workspace.reporting --database skills_workspace.sqlite3 audit
```

服务重启后，SQLite 中的案例版本、租约、签署、部件流转、未完工作和审计链继续保留。
