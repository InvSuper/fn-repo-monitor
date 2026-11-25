# GitHub仓库更新监控 (fn-repo-monitor)

定时检测 GitHub 仓库更新，AI 解读变更内容与影响，判断是否建议跟进，推送到 **Server酱**。

支持两种监控方式：

- **任意新提交**：分支出现新 commit 就提醒，适合关注活跃开发进度
- **仅正式版 Release**：只在上游发布正式版时提醒（自动跳过草稿和预发布），适合“等官方稳定版再同步”

## 功能

- 🕒 定时检测多个 GitHub 仓库（按间隔或每天固定时间）
- 🎯 两种监控方式：任意提交 / 仅正式版 Release，逐个仓库独立设置
- 🔍 按关键词高亮与你相关的变更文件
- 🤖 AI 解读提交内容或发布说明，给出是否建议跟进及紧迫度，支持一键测试连接
- 📱 推送到 Server酱（Server酱³ `sctp` 开头 / Turbo `SCT` 开头均支持），支持测试推送
- 🔗 可配置跳转链接，指向你的构建页、发布页或说明文档
- 💾 本地状态记录，无更新不打扰，省 Server酱 免费额度
- 🖥️ 飞牛桌面图形界面：查看状态、编辑配置、手动触发、测试推送、查看日志

## 一、编译出 .fpk（GitHub Actions）

1. 把本仓库推到你的 GitHub（保持 `apps/fn-repo-monitor/` 目录结构）
2. 打开 **Actions → Build fn-repo-monitor FPK**
3. 点 **Run workflow** 手动触发
4. 编译完成后，在 run 详情页的 **Artifacts** 里下载 `fn-repo-monitor-fpk`，解压得到 `.fpk` 文件
5. （可选）打 tag 如 `v1.3.0` 推送，会自动发布到 Release

## 二、安装到飞牛 NAS

1. 飞牛 **应用中心 → 离线安装**，选择下载的 `.fpk` 文件安装
2. 安装完成后，桌面会出现 **GitHub仓库更新监控** 图标；若没出现，执行
   `systemctl restart trim_app_center.service` 刷新应用中心
3. 点开图标进入图形界面。安装包默认**不带任何监控仓库**，先在「配置 → 仓库列表」中添加
4. 填写 Server酱 SendKey、AI Key 等，点「保存配置」立即生效
5. 点「测试推送」确认微信能收到消息，再点「立即检测」跑一轮

> 应用以飞牛桌面窗口（iframe）方式打开，HTTP 内网访问和 HTTPS 域名访问都可用，不需要额外开放端口。
> 如需 SSH 改配置，文件位于持久数据目录 `var/config.json`（`TRIM_PKGVAR`）。

## 三、图形界面说明

| 区域 | 作用 |
|------|------|
| 顶部状态卡片 | 服务状态、上次/下次检测时间、检测间隔（或每天固定时间）、有更新的仓库数 |
| 监控仓库 | 每个仓库的监控方式、基线版本/提交、上次检测结果、有更新时显示 AI 建议 |
| 配置 | 检测间隔、每天固定检测时间、Server酱 SendKey（旁带测试推送）、GitHub Token、AI 接口与模型（旁带测试 AI）、仓库增删改 |
| 运行日志 | 最近 300 行日志，便于排查推送失败、GitHub 限流等问题 |
| 立即检测 / 刷新 | 手动跑一轮检测 / 重新读取状态和日志 |

配置保存后无需重启应用，后台调度会按新的时间设置执行。填写「每天固定检测时间」后按每天该时刻检测，检测间隔不再生效；留空则按间隔执行。

## 四、添加监控仓库

在「配置 → 仓库列表」点「添加仓库」，填写以下字段：

| 字段 | 说明 |
|------|------|
| 名称 | 显示用名称，可任意填写 |
| 上游仓库 | `owner/repo` 格式，例如 `owner/project` |
| 分支 | 提交模式使用，默认 `main` |
| 监控方式 | `任意新提交`（commit）或 `仅正式版 Release`（release） |
| 关键词 | 可选。按文件路径小写匹配，命中的文件会在推送里单独列出 |
| 跳转链接 | 可选。推送里附带的可点击链接，例如你的构建页、Release 页或文档 |

对应 `config.json` 中的结构：

```json
{
  "name": "示例项目",
  "upstream": "owner/project",
  "branch": "main",
  "watch": "release",
  "keywords": ["src", "config"],
  "build_url": "https://github.com/owner/project/releases"
}
```

说明：

- `watch` 为 `commit` 时，比较的是分支最新提交；为 `release` 时，比较的是最近一个**非草稿、非预发布**的 Release。
- 监控方式按仓库单独设置，对所有新增仓库都适用；新添加的仓库默认是「任意新提交」，需要只提醒稳定版时手动切换。
- 正式版模式下，如果上游从未发布正式版，检测结果会提示“没有正式版 Release”，不会推送。
- 首次检测或切换监控方式后，只记录当前基线并发送一条「监控已就绪」，不会误报更新。
- 关键词只影响高亮，不影响是否提醒。

## 五、config.json 全局字段

```json
{
  "interval_hours": 24,
  "daily_time": "",
  "github_token": "",
  "serverchan": {
    "sendkey": "你的Server酱SendKey"
  },
  "ai": {
    "enabled": true,
    "api_base": "https://api.deepseek.com/v1",
    "api_key": "你的AI接口Key",
    "model": "deepseek-chat"
  },
  "repos": [ ... ]
}
```

| 字段 | 说明 |
|------|------|
| `interval_hours` | 检测间隔（小时），默认 24；`daily_time` 非空时不生效 |
| `daily_time` | 可选。每天固定检测时间，格式 `HH:MM`（如 `08:30`），留空则按间隔执行 |
| `github_token` | 可选。GitHub PAT，填了可提高 API 限流（60→5000 次/小时），不填也能用 |
| `serverchan.sendkey` | Server酱 SendKey，`sctp` 或 `SCT` 开头均可 |
| `ai.enabled` | 是否启用 AI 解读。`api_key` 留空时自动关闭 AI，只推送原始内容 |
| `ai.api_base` / `model` | 兼容 OpenAI 格式的接口地址和模型名，可换任意同类服务 |
| `repos` | 监控仓库列表，默认空，需要在界面中添加 |

## 六、获取 Server酱 SendKey

1. 打开 https://sct.ftqq.com ，微信扫码登录
2. 首页顶部复制你的 **SendKey**（免费版每天 5 条，每天一次检测足够用）

## 七、免费 AI 方案

- **DeepSeek**：https://platform.deepseek.com 注册即送免费额度，`deepseek-chat` 便宜且中文好。把 `api_key` 填入即可。
- 也可换成任意兼容 OpenAI 格式的接口（改 `api_base` 和 `model`）。
- 不想用 AI：关闭「启用 AI 解读」，程序只推送提交/发布说明原文，不解读。

## 八、本地测试

```bash
# 仅跑一轮检测（不启动 Web 服务和定时任务）
python3 apps/fn-repo-monitor/app/src/monitor.py once

# 带界面本地调试（数据目录用临时目录，避免污染真实配置）
FN_MONITOR_DATA=/tmp/fn-monitor-dev \
python3 apps/fn-repo-monitor/app/src/server.py --port 3869 \
  --web-dir apps/fn-repo-monitor/app/ui

# 浏览器打开 http://127.0.0.1:3869/
```

## 推送消息示例

提交模式：

```
🟢 建议更新（紧迫度：高）

示例项目 上游有新提交
范围：000a4e74 → a1b2c3d4
统计：5 commits / 12 files

🎯 相关变更
src/core/config.go
docs/release-notes.md

📝 提交记录
- [a1b2c3d4] fix: 修复内存泄漏 (author, 2026-11-25 10:00)

🤖 AI 解读
作用：修复核心模块的内存泄漏问题
详情：...
建议：建议更新
紧迫度：高

🔗 链接
- 完整 Diff
- 👉 去同步/编译
```

正式版模式：

```
🟡 可以暂缓（紧迫度：中）

示例项目 发布 v2.3.0
发布时间：2026-11-25

📄 发布说明
- 新增 ...
- 修复 ...

🤖 AI 解读
作用：...
建议：可以暂缓
紧迫度：中

🔗 链接
- Release 页面
- 👉 去同步/编译
```
