# 飞书快照拉取 — 参考

## MCP 字段解析

`search_by_mql` 每条记录在 `data[group_id][].moql_field_list`：

| 关注 | 常见 name | value 形态 |
|------|-----------|------------|
| ID | `Item Id` | `long_value` |
| 标题 | `Summary` | `string_value` |
| 状态/优先级 | `Status` / `Priority` | `key_label_value` 或 list，取 **label** |
| 待测试进入 | `待测试 进入时间` | 字符串时间，取前 10 位日期 |
| 开发排期结束 | `开发排期 结束时间` | `string_value_list`，取 max 日期 |

不要按数组下标硬编码字段顺序（顺序可能变）。

## 分页常见失败

| 现象 | 原因 | 处理 |
|------|------|------|
| `collected < count` 且末页空 | session 过期或未传 session_id | 整类重拉 |
| 只写了第一页 50 条 | 忘记翻页 | 必须循环到 count |
| Bug 180 vs Sprint 字段 196 | 误用关联 Story 过滤 | 改回 `Bug.Sprint =` |
| UI 199 vs 快照 196 | 视图筛选 ≠ Sprint 字段 | 以 MQL count 为准并说明 |

## reopenTimes

`get_workitem_op_record` 约 7 天窗，不能用于整 Sprint Reopen 统计。快照一律 `0`，并在 `rules.reopen` 注明。

## 与日报关系

Web 只读 `exports/feishu/{sprint}_latest.json`，不调 MCP。  
刷新快照后刷新预览即可；改 `.py` 才需重启 uvicorn。
