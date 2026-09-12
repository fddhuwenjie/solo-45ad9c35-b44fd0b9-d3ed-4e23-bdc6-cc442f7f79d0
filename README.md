# 环境检测样品时限判定 API

为每个“样品—项目”建立独立的保存时限时钟，处理连续采样、分样/合样共享历史、
前处理与分析两段期限、温度断档/越界、防腐缺失、时间倒置、来源成环等情形。

**规则按适用范围自动匹配**：规则项带生效区间与样品基质/分析方法/容器/保存条件，
服务沿来源链解析每个时钟的**原始采样时刻**与实际条件筛选候选；只有唯一命中的
规则才交给时钟计算。无匹配、多同等候选、规则时段重叠或保存动作不足时，时钟为
`indeterminate`，**不形成合规结论**，响应列出全部候选、各候选未满足条件与字段
来源。规则内容以哈希版本化，**规则更新不改写旧判定**；补录事件生成新版本。

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
| POST | `/api/v1/judgments/trial` | 试算：完整推导但不持久化；可用 `selected_candidates` 强制候选对照（`package_id` 以 `trial-` 开头） |
| POST | `/api/v1/judgments` | 正式接收：只冻结各时钟唯一匹配的规则及内容哈希；任一时钟无唯一结论即 422；同 `idempotency_key` 幂等 |
| POST | `/api/v1/samples/{sample_id}/supplement` | 复录事件：对该样品最近判定生成新版本（规则冻结不重新挑选），列出状态变化 |
| GET  | `/api/v1/judgments/{package_id}` | 取指定版本 JSON 判定包 |
| GET  | `/api/v1/samples/{sample_id}/latest` | 取样品当前最新判定 |
| GET  | `/api/v1/samples/{sample_id}/versions` | 样品判定版本列表 |
| GET  | `/api/v1/judgments/{package_id}/diff/{other_package_id}` | 两个判定包版本比较 |
| GET  | `/api/v1/rules` / `/{version}` | 规则版本列表 / 详情 |
| POST | `/api/v1/rules` | 登记规则版本；`?dry_run=true` 只做适用范围冲突预检（不写库）；冲突返回 409 |
| POST | `/api/v1/rules/impact-preview` | 影响预览：用全部已保存请求试算携带的新规则，列出会改选规则的样品—项目，不写库、不改旧判定 |
| POST | `/api/v1/rules/diff` | 两套规则（或版本）逐规则项差异 |
| GET  | `/healthz` | 健康检查 |

## 核心语义

1. **独立时钟**：每个 (样品, 项目) 一个时钟，按其唯一适用规则分别计算保存温度、
   预处理期限（`pretreatment_minutes`）与分析期限（`analysis_minutes`）。
2. **规则适用性匹配**：
   - 规则项字段 `rule_id`、`effective_from`/`effective_to`（半开区间
     `[from, to)`，端点可空表示无界）、`matrices`、`methods`、`containers`、
     `storage_conditions`；四个条件列表为空表示该维度**通配**。
   - 样品侧提交 `matrix`、`storage_condition`、`container` 与按项目的
     `item_methods`（键为项目）；条件未提交时沿分样/合样来源链回退到祖先。
   - 筛选基准是时钟沿来源链解析出的**原始采样时刻**（合样取最早组成样；连续样
     按候选规则自身的 `continuous_basis` 取端）。
3. **不形成合规结论的情形**（时钟 `status="indeterminate"`、`conclusive=false`，
   违规码 `rule_applicability` / `missing_preservation`）：
   - 无候选满足生效区间与条件（`match_status="none"`）；
   - 两条及以上规则同等适用（`ambiguous`，通常源于规则时段/范围重叠）；
   - 唯一规则要求的保存/防腐动作不足（期限仍照算供操作参考，但不下结论）。
   - 响应时钟内 `candidates` 给出每条候选、是否命中、逐条 `unmet` 条件及
     `field_sources`（字段取值与实际来源样品）；`match_context` 给出筛选基准与
     全部字段来源。
4. **试算对照**：`selected_candidates` 以 `"样品id/项目"` 为键，显式指定
   `rule_id`（可附 `version`/`content_hash`）强制使用某候选；强制结果
   `match_status="forced"` 且 `conclusive=false`，仅用于对照。正式接收携带该
   字段直接 422。
5. **正式接收只冻结唯一匹配**：判定包为每个时钟记录 `matched_rule`
   （`version`/`content_hash`/`rule_id`），包级 `rules` 列出涉及的全部规则集。
   任一时钟不是唯一可结论状态即整体 422，`detail.blocked_clocks` 列出原因与候选。
6. **登记冲突预检**：同一项目的规则若生效区间重叠且基质/方法/容器/保存条件四维
   都可能同时命中（通配与任何值重叠），登记返回 409 并列冲突明细；完全相同内容
   的重复发布幂等。规则集内部重叠在请求校验阶段即 422。
7. **影响预览**：`/rules/impact-preview` 把新规则集并入候选池，用所有已保存正式
   请求只读重放，`reselected` 逐条列出会改选（或变为歧义/失配）的样品—项目及
   前后规则；不登记规则、不改写任何旧判定。
8. **连续采样**：默认以采样结束（`sampling_end`）为基准；规则可指定
   `continuous_basis = start|end`。
9. **分样**：继承母体的基准时刻（同一段共享历史）与未提交的适用条件；**合样**
   取组成样中**最早**的基准时刻，`merged_at` 之前历史共享。
10. **前处理只结束预处理阶段**：分析期限始终从原始基准起算，前处理不清零。
11. 其它校验：时间倒置、来源成环/断档、温度越界与记录断档、交接晚于截止时刻，
    均返回受影响项目与完整 `derivation`。
12. **版本化**：规则以内容哈希标识（适用范围是哈希的一部分）；旧判定包永不被
    新规则覆盖。

## 示例规则项

```json
{
  "item": "cod",
  "rule_id": "cod-surface-hj828",
  "effective_from": "2026-01-01T00:00:00+08:00",
  "effective_to": "2027-01-01T00:00:00+08:00",
  "matrices": ["surface_water", "groundwater"],
  "methods": ["hj828"],
  "containers": ["glass_amber"],
  "storage_conditions": ["refrigerated_4c"],
  "min_temp_c": 0, "max_temp_c": 4,
  "pretreatment_minutes": 120, "analysis_minutes": 1440,
  "required_preservation": ["cool_4c"]
}
```

## 示例

```bash
.venv/bin/python scripts/demo.py        # 正常 / 临界 / 超时 / 成环 / 补录 / 冲突预检 全流程
.venv/bin/pytest -q                     # 单元 + API 测试
```
