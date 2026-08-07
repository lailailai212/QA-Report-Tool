# QA Report Tool

Web 工具集：首页（`/`）提供工具入口；**快速建 Bug**（`/bug`）按 JSON 模板预填飞书字段并一键创建；**QA 测试执行日报**（`/report`）打开即自动拉取并预览当前模块日报（MeterSphere 执行数据 + 本地飞书 Story/Bug 快照）；**Sprint QA 结项总汇报**（`/completion`）汇总整 Sprint 测试完成情况，支持预览与手动发信（无定时）。

**组员上手（含注意事项）：** 日报页右上角 **「使用教程」**（`/help`），或看 [docs/组员使用教程.md](docs/组员使用教程.md)。

## 启动（单 worker，定时才可靠）

```powershell
pip install -r requirements.txt
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

浏览器打开：http://127.0.0.1:8000/（Home）· 建 Bug：http://127.0.0.1:8000/bug · 日报：http://127.0.0.1:8000/report · 结项：http://127.0.0.1:8000/completion

**开机自启动（Windows）：** 见 [docs/开机自启动.md](docs/开机自启动.md)。快速注册：

```powershell
.\scripts\register_autostart.ps1
```

改了 `backend/app/*.py` 需**重启** uvicorn；仅改 `static/*.html` / 邮件模板一般刷新即可。

## 配置

复制 `.env.example` 为 `.env`，填写：

- MeterSphere：`METERSPHERE_ACCESS_KEY` / `METERSPHERE_SECRET_KEY`（及可选 `BASE_URL` / `ORGANIZATION` / `PROJECT`）
- 飞书 SMTP：`SMTP_USER` / `SMTP_PASSWORD`（专用密码）；可选 `SMTP_FROM`
- 飞书快照刷新 / 快速建 Bug：`MCP_USER_TOKEN`；本机需有 **Node / npx**（后端通过 `@lark-project/mcp` 拉数、创建工作项、上传描述图片）
  - 定时默认：工作日 `FEISHU_SNAPSHOT_TIME=20:20`（可用 `FEISHU_SNAPSHOT_WEEKDAYS` / `FEISHU_SNAPSHOT_SPRINT` 覆盖）

## 快速建 Bug（/bug）

- **创建流程**：选模板 → 填/改 Summary、Severity、Priority、Description、Component Versions → 创建
- **模板预填**：空间、Sprint、飞书 Bug Template、Issue Type、Execution Method、Labels、Issue Stage、Bug Environment、Version Found、Severity / Priority 默认值、Owners（Dev/QC/Reporter），以及预置 **Summary / Description**
- **模板管理**：同页可新建 / 编辑 / **复制** / 删除；持久化 `backend/data/bug_templates.json`
- **Description 截图**：在编辑区内光标处 Ctrl+V 粘贴（图文混排）；创建时按文档顺序自动上传到飞书（最多 12 张，单张 ≤ 8MB）
- **连续创建**：创建成功后自动重新套用当前模板预置
- **依赖**：与快照刷新相同的 `MCP_USER_TOKEN`（MCP `create_workitem` / `upload_file`）。若报「user has not enabled this MCP feature」，需在飞书项目个人偏好中开通对应 MCP 写能力

## 飞书 Story / Bug 数据（本地快照）

日报合并读本地文件：`exports/feishu/{Sprint名}_latest.json`。

**日常路径（推荐）：**

1. **工作日 20:20** 服务端自动刷新（需配置 `MCP_USER_TOKEN` 且 uvicorn 常驻）
2. **发信人按需**：日报预览标题旁的 **飞书图标** → 弹窗内「刷新飞书快照」（MCP 有限流，勿连点、勿多人同时刷）

仍可用 Cursor + Skill 手动拉数：对话中说「刷新飞书快照」或 `@feishu-sprint-snapshot`。拉取约束（全量分页、`count` 对齐、校验通过才覆盖 latest）：

[`.cursor/skills/feishu-sprint-snapshot/SKILL.md`](.cursor/skills/feishu-sprint-snapshot/SKILL.md)

字段与规则说明见 [exports/feishu/README.md](exports/feishu/README.md)。

- Ready = 状态是否为「待测试 / 测试中 / 待验收」
- Ready Date = 进入「待测试」的日期
- 预期提测日 = 「开发」节点排期结束日；晚于预期则 Comment=`提测Delay`
- Bug Reopen 次数：当前快照因 MCP 操作记录仅 7 天窗暂为 0

本地校验示例：

```powershell
python .cursor/skills/feishu-sprint-snapshot/scripts/validate_snapshot.py exports/feishu/OBIS-20260622-20260703_latest.json --expect-stories 61 --expect-bugs 201
```

（`--expect-*` 以当次 MCP `list.count` 为准。）

## 行为说明

- **默认预览**：进入页面后自动选中**名称中日期最新**的 Module（如 `OBIS-20260706-20260717`）并拉取预览；切换模块也会自动刷新。
- **表格行来源**：行 = MeterSphere 模块下的**子计划（TEST_PLAN）**；飞书 Story / Ready 按标题合并（**忽略大小写**、规范化空白/全半角、去掉 `[数字]` 前缀，相似度 ≥ 0.88；歧义则不匹配）。MS 无对应子计划的飞书 Story **不会单独成行**。
- **Sprint 维护数据**（服务端持久化，路径 `backend/data/overrides/{Sprint}.json`）：
  - **Test ENV / Risk/Block**：「编辑 Test ENV / Risk」弹窗维护并「保存 ENV/Risk」（打开时会重新拉取最新数据）。Test ENV 为多选标签（`SIT / UAT1 / UAT2 / PRE1 / PRE2 / PROD`），日报以标签展示；同 Sprint 下次预览、发信、**定时任务**都会用。
  - **Ready for Test**（Yes/No、Ready Date、Comment）：「编辑 Ready for Test」独立弹窗维护；**人工值优先于飞书推导**。
  - **Bug Reopen**：「编辑 Bug Reopen」独立弹窗维护；保存后覆盖飞书快照 Reopen；可「恢复飞书 Reopen」。
- **工具按钮**：手动发送、定时任务、收件人以弹窗打开。
- **收件人存储**：默认 To / CC 仅在「收件人设置」写入浏览器 `localStorage`；手动发送 / 创建定时任务**不会**改默认收件人。
- **手动发送**：使用当前表单 ENV/Risk + 已保存 Ready/Reopen；**会重新拉取** MS + 读本地飞书快照。
- **定时发送**：读取该模块已保存的维护数据 + 飞书快照 + MS（需事先有可用快照或依赖工作日 20:20 / 发信人按需刷新）。
- 进程需常驻，关闭后定时任务（含飞书快照 cron）不会触发。
- 主表列分组：Story（Name/Status）· Testing（含 Case Num / No Run 等）· Ready for Test。
