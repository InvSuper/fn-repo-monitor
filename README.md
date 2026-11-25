# 飞牛仓库更新监控 (fn-repo-monitor)

定时检测 GitHub 上游仓库更新，AI 解读变更内容与影响，判断是否需要跟进更新固件/客户端，推送到 **Server酱**，并附上手动编译的跳转链接。

## 功能

- 🕒 定时检测多个 GitHub 仓库的上游更新（默认每 24 小时一轮）
- 🔍 自动提取相关变更（按关键词筛选，如 `ipq60xx`、`qca`、`nss`）
- 🤖 AI 解读变更做了什么、是否影响你的固件、建议是否更新
- 📱 推送到 Server酱（Server酱³ `sctp` 开头 / Turbo `SCT` 开头均支持）
- 🔗 推送消息带「去编译」跳转链接，点过去手动 Run workflow
- 💾 本地状态记录，无更新不打扰，省 Server酱 免费额度
- 🖥️ 飞牛桌面图形界面：查看检测状态、编辑配置、手动触发、测试推送、查看日志

## 内置监控的仓库

| 仓库 | 上游 | 分支 | 跳转编译 |
|------|------|------|----------|
| OpenWrt-IPQ60XX | LiBwrt/openwrt-6.x | 25.12-nss | IPQ60XX-NOWIFI-LibWrt.yml |
| mygo-cloudmusic | egoist/mygo-cloudmusic | main | release.yml |
| fnmusic | waygu-music-plugin/fnmusic | main | release.yml |

> 可在界面或 `config.json` 中任意增删仓库。

## 一、编译出 .fpk（GitHub Actions）

1. 把本仓库推到你的 GitHub（保持 `apps/fn-repo-monitor/` 目录结构）
2. 打开 **Actions → Build fn-repo-monitor FPK**
3. 点 **Run workflow** 手动触发
4. 编译完成后，在 run 详情页的 **Artifacts** 里下载 `fn-repo-monitor-fpk`，解压得到 `.fpk` 文件
5. （可选）打 tag 如 `v1.1.0` 推送，会自动发布到 Release

## 二、安装到飞牛 NAS

1. 飞牛 **应用中心 → 离线安装**，选择下载的 `.fpk` 文件安装
2. 安装完成后，桌面会出现 **仓库更新监控** 图标；若没出现，执行
   `systemctl restart trim_app_center.service` 刷新应用中心
3. 点开图标即可进入图形界面，在「配置」区域填写 Server酱 SendKey、AI Key 等，点「保存配置」立即生效
4. 想马上验证效果，点「测试推送」确认 Server酱是否收到消息，再点「立即检测」跑一轮

> 应用以飞牛桌面窗口（iframe）方式打开，HTTP 内网访问和 HTTPS 域名访问都可用，不需要额外开放端口。
> 如需 SSH 改配置，文件仍位于持久数据目录 `var/config.json`（`TRIM_PKGVAR`）。

## 三、图形界面说明

| 区域 | 作用 |
|------|------|
| 顶部状态卡片 | 服务状态、上次/下次检测时间、检测间隔、有更新的仓库数 |
| 监控仓库 | 每个仓库的基线提交、上次检测结果、有更新时显示 AI 建议 |
| 配置 | 检测间隔、Server酱 SendKey、GitHub Token、AI 接口与模型、仓库增删改 |
| 运行日志 | 最近 300 行日志，便于排查推送失败、GitHub 限流等问题 |
| 立即检测 / 测试推送 | 手动跑一轮检测 / 发送一条测试消息 |

配置保存后无需重启应用，后台调度会按新的间隔执行。

## 四、config.json 字段说明

界面保存的也是这份文件，字段含义如下：

```json
{
  "interval_hours": 24,
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
| `interval_hours` | 检测间隔（小时），默认 24 |
| `github_token` | 可选。GitHub PAT，填了可提高 API 限流（60→5000 次/小时），不填也能用 |
| `serverchan.sendkey` | Server酱 SendKey，`sctp` 或 `SCT` 开头均可 |
| `ai.enabled` | 是否启用 AI 解读。`api_key` 留空时自动关闭 AI，只推送原始摘要 |
| `ai.api_base` / `model` | 兼容 OpenAI 格式的接口地址和模型名，可换任意同类服务 |
| `repos[].keywords` | 筛选相关变更文件的关键词（小写匹配），不填则全部变更都算相关 |

## 五、获取 Server酱 SendKey

1. 打开 https://sct.ftqq.com ，微信扫码登录
2. 首页顶部复制你的 **SendKey**（免费版每天 5 条，每天一次检测足够用）

## 六、免费 AI 方案

- **DeepSeek**：https://platform.deepseek.com 注册即送免费额度，`deepseek-chat` 便宜且中文好。把 `api_key` 填入即可。
- 也可换成任意兼容 OpenAI 格式的接口（改 `api_base` 和 `model`）。
- 不想用 AI：关闭「启用 AI 解读」，程序只推送变更文件列表和提交记录，不解读。

## 七、本地测试

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

```
🟢 建议更新（紧迫度：高）

OpenWrt-IPQ60XX 上游有新提交
范围：000a4e74 → a1b2c3d4
统计：5 commits / 12 files

🎯 相关变更
target/linux/qualcommax/ipq60xx/config-default
package/qca/nss/qca-nss-sfe/Makefile

📝 提交记录
- [a1b2c3d4] fix: nss 驱动内存泄漏 (author, 2026-11-25 10:00)

🤖 AI 解读
作用：修复 NSS 转发驱动的内存泄漏问题
详情：...
建议：建议更新
紧迫度：高

🔗 链接
- 完整 Diff
- 👉 去编译固件
```
