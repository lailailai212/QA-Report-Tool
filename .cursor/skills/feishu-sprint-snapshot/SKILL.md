---
name: feishu-sprint-snapshot
description: >-
  按 Sprint 从飞书项目 MCP 全量拉取 User Story / Bug，写入 exports/feishu 快照且保证无丢页、无丢条、字段精确。
  用于「抓取飞书」「刷新飞书快照」「更新 *_latest.json」「拉 Sprint 数据」「feishu snapshot」等场景。
---

# 飞书 Sprint 快照拉取（无丢失 / 精确）

## 何时使用

用户要求刷新或抓取飞书项目数据、更新 `exports/feishu/{Sprint}_latest.json`，或日报预览缺飞书列时：**必须先读本 skill 再拉数**。

## 硬约束（违反即失败，禁止覆盖 latest）

1. **口径唯一**：仅 `WHERE Sprint = '{sprint}'` 的工作项类型  
   - Story：`User Story`  
   - Bug：`Bug`（禁止用已弃用 `Bug（即将弃用）` / `issue`）  
   - **禁止**用「关联 Story 的 Sprint」替代 Bug 自身 Sprint 字段
2. **全量分页**：以首次响应 `list[0].count` 为期望总数；翻页直到 `collected == count`
3. **无丢条**：`len(stories) == storyCount` 且 `unique(id) == len`；Bug 同理
4. **先校验再覆盖**：先写归档 / 临时文件 → 跑校验脚本 → 通过后才覆盖 `_latest.json`
5. **不得静默截断**：任一类 `collected < count` 必须报错并停止，不得写入 latest

## 固定常量

| 项 | 值 |
|----|-----|
| MCP server | `user-FeishuProjectMcp` |
| tool | `search_by_mql` |
| project_key | `67f5e379dd7f8a00d58f4b0e` |
| simpleName | `obis` |
| 空间名（MQL） | `OBIS` |
| 输出目录 | `exports/feishu/` |

当前默认 Sprint（用户未指定时）：`OBIS-20260622-20260703`  
用户指定了其他 Sprint 名则用用户的。

## MQL（不得改 SELECT / WHERE 语义）

**Stories**

```sql
SELECT `Item Id`, `Summary`, `Status`, status_time('待测试'), get_node_attribute('开发','__排期_结束时间')
FROM `OBIS`.`User Story`
WHERE `Sprint` = '{sprint}'
```

**Bugs**

```sql
SELECT `Item Id`, `Summary`, `Status`, `Priority`
FROM `OBIS`.`Bug`
WHERE `Sprint` = '{sprint}'
```

## 分页规程（无丢失关键）

```
1. CallMcpTool search_by_mql: { project_key, mql }
2. 记录:
   - expected = list[0].count
   - session_id
   - group_id = list[0].group_infos[0].group_id（通常 "1"）
   - items = data[group_id]（本页）
3. page_num = 2
4. while len(items) < expected:
     CallMcpTool: {
       project_key,
       session_id,          # 必填；此时 mql 传空或不传以走 session
       group_pagination_list: [{ group_id, page_num }]
     }
     追加本页 data[group_id]
     若本页 0 条且仍不足 → FAIL（丢页）
     page_num += 1
     安全上限：page_num > 100 → FAIL
5. ASSERT len(items) == expected
```

每页最多约 50 条。Story/Bug **各自独立** session，勿混用。

## 字段推导（精确）

### Story

| 字段 | 规则 |
|------|------|
| `id` | `Item Id` → string |
| `name` | Summary 标题 |
| `status` | Status **label**（勿用 key） |
| `ready` | status ∈ {待测试, 测试中, 待验收} → `"Yes"`，否则 `"No"` |
| `readyDate` | `status_time('待测试')` 取 `YYYY-MM-DD`；无则 `""` |
| `expectedReadyDate` | 开发节点 `__排期_结束时间` 列表日期的 **max**；无则 `""` |
| `comment` | 两者非空且 `readyDate > expectedReadyDate` → `"提测Delay"`，否则 `""` |
| `url` | `https://project.feishu.cn/obis/userstory/detail/{id}` |

MCP 返回字段名可能是 `待测试 进入时间`、`开发排期 结束时间` 等，按 `moql_field_list` 的 `name` / `key` 解析，不要假设固定下标。

### Bug

| 字段 | 规则 |
|------|------|
| `id` | string |
| `name` **与** `summary` | 同为标题（缺一会让模板空标题） |
| `status` | 规范化：`测试中`→`Testing`；保留 `To Do`/`Testing`/`Fixing`/`Confirming`/`Clarifying`/`Done`/`Closed` |
| `priority` | `P0`–`P3`；空则 `P3` |
| `reopenTimes` | **固定 0**（op_record 仅约 7 天窗，禁止瞎填） |
| `url` | `https://project.feishu.cn/obis/bug/detail/{id}` |

## 写入规程

1. 组装 JSON（schema 见下）
2. 写归档：`exports/feishu/{sprint}_{YYYYMMDD_HHMMSS}.json`（时区 Asia/Shanghai）
3. 跑校验（必须）：

```bash
python .cursor/skills/feishu-sprint-snapshot/scripts/validate_snapshot.py exports/feishu/{sprint}_{stamp}.json --expect-stories N --expect-bugs M
```

其中 `N`/`M` 为 MCP 返回的 `count`。
4. 校验通过后，**再**覆盖 `exports/feishu/{sprint}_latest.json`（内容与归档一致）
5. 向用户回报：`fetchedAt`、story/bug 数量、Ready=Yes、提测Delay、两个文件路径

校验失败：**禁止**覆盖 `_latest.json`；说明缺页/重复/缺字段。

## JSON 顶层 schema

```json
{
  "sprint": "{sprint}",
  "source": "mcp",
  "fetchedAt": "ISO8601",
  "projectKey": "67f5e379dd7f8a00d58f4b0e",
  "simpleName": "obis",
  "rules": {
    "readyYesStatuses": ["待测试", "测试中", "待验收"],
    "readyDateFrom": "status_enter_待测试",
    "expectedReadyDateFrom": "开发节点排期结束日_max",
    "delayComment": "提测Delay",
    "reopen": "Testing/测试中 then To Do count; currently stubbed to 0 due to MCP op_record 7-day limit"
  },
  "stories": [],
  "bugs": []
}
```

完整字段说明：`exports/feishu/README.md`。

## 完成检查清单

复制并勾选：

```
- [ ] Story：collected == list.count，无重复 id
- [ ] Bug：collected == list.count，无重复 id
- [ ] 每条 story 含 id/name/status/ready/readyDate/expectedReadyDate/comment/url
- [ ] 每条 bug 含 id/name/summary/status/priority/reopenTimes/url
- [ ] validate_snapshot.py 退出码 0
- [ ] 已写归档 + 已覆盖 _latest.json
- [ ] 未把飞书 UI 其它筛选口径的数字当成失败依据（UI 可能 ≠ Sprint 字段 count）
```

## 可选自动化脚本

若环境有 `MCP_USER_TOKEN`，可用：

```bash
node exports/feishu/_fetch_snapshot.mjs
```

跑完后仍须执行 `validate_snapshot.py` 再确认 `_latest.json`。

## 附加资源

| 文件 | 用途 |
|------|------|
| [scripts/validate_snapshot.py](scripts/validate_snapshot.py) | 无丢失 / schema 校验（强制） |
| [reference.md](reference.md) | MCP 字段解析与常见失败 |
