# 环境检测样品时限判定 API

为每个“样品—项目”建立独立的保存时限时钟，处理连续采样、分样/合样共享历史、
前处理与分析两段期限、温度断档/越界、防腐缺失、时间倒置、来源成环等情形。
规则随请求携带并版本化，**规则更新不改写旧判定**；补录事件生成新版本并列出状态变化。

## 运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
# API 文档: http://127.0.0.1:8000/docs
```

SQLite 数据库默认在 `data/app.db`（环境变量 `DEADLINE_DB` 可覆盖）。

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/judgments/trial` | 试算：完整推导但不持久化（`package_id` 以 `trial-` 开头） |
| POST | `/api/v1/judgments` | 正式接收：固化规则与判定包，返回版本号；同 `idempotency_key` 重复提交幂等 |
| POST | `/api/v1/samples/{sample_id}/supplement` | 补录事件：对该样品最近判定生成新版本，列出状态变化 |
| GET  | `/api/v1/judgments/{package_id}` | 取指定版本 JSON 判定包 |
| GET  | `/api/v1/samples/{sample_id}/latest` | 取样品当前最新判定 |
| GET  | `/api/v1/samples/{sample_id}/versions` | 样品判定版本列表 |
| GET  | `/api/v1/judgments/{package_id}/diff/{other_package_id}` | 两个判定包版本比较 |
| GET  | `/api/v1/rules` / `/{version}` | 规则版本列表 / 详情 |
| POST | `/api/v1/rules` | 登记规则版本（正式接收时也会自动登记） |
| GET  | `/healthz` | 健康检查 |

## 核心语义

1. **独立时钟**：每个 (样品, 项目) 一个时钟，按项目规则分别计算保存温度、
   预处理期限（`pretreatment_minutes`）与分析期限（`analysis_minutes`）。
2. **连续采样**：默认以采样结束（`sampling_end`）为基准；规则可指定
   `continuous_basis = start|end`。
3. **分样**：继承母体的基准时刻（同一段共享历史），合样时刻仅结束合样前阶段，
   不会重置分析期限。
4. **合样**：取组成样中**最早**的基准时刻作为合样样基准。
5. **前处理只结束预处理阶段**：分析期限始终从原始基准起算，前处理不清零。
6. 校验：时间倒置、来源成环/断档、温度越界与记录断档、防腐动作缺失、
   交接晚于截止时刻，均返回受影响项目与完整 `derivation`。
7. **版本化**：规则以内容哈希标识；旧判定包永不被新规则覆盖。

## 示例

```bash
.venv/bin/python scripts/demo.py        # 正常 / 临界 / 超时 / 成环 / 补录 全流程演示
.venv/bin/pytest -q                     # 单元 + API 测试
```
