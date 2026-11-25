# 飞牛仓库更新监控 (fn-repo-monitor)

定时检测 GitHub 上游仓库更新，AI 解读变更内容与影响，判断是否需要跟进更新固件/客户端，推送到 **Server酱**，并附上手动编译的跳转链接。

## 功能

- 🕒 定时检测多个 GitHub 仓库的上游更新（默认每 24 小时一轮）
- 🔍 自动提取相关变更（按关键词筛选，如 `ipq60xx`、`qca`、`nss`）
- 🤖 AI 解读变更做了什么、是否影响你的固件、建议是否更新
- 📱 推送到 Server酱（Server酱³ `sctp` 开头 / Turbo `SCT` 开头均支持）
- 🔗 推送消息带「去编译」跳转链接，点过去手动 Run workflow
- 💾 本地状态记录，无更新不打扰，省 Server酱 免费额度

## 内置监控的仓库

| 仓库 | 上游 | 分支 | 跳转编译 |
|------|------|------|----------|
| OpenWrt-IPQ60XX | LiBwrt/openwrt-6.x | 25.12-nss | IPQ60XX-NOWIFI-LibWrt.yml |
| mygo-cloudmusic | egoist/mygo-cloudmusic | main | release.yml |
| fnmusic | waygu-music-plugin/fnmusic | main | release.yml |

> 在 `config.json` 里可任意增删仓库。

## 一、编译出 .fpk（GitHub Actions）

1. 把本仓库推到你的 GitHub（保持 `apps/fn-repo-monitor/` 目录结构）
2. 打开 **Actions → Build fn-repo-monitor FPK**
3. 点 **Run workflow** 手动触发
4. 编译完成后，在 run 详情页的 **Artifacts** 里下载 `fn-repo-monitor-fpk`，解压得到 `.fpk` 文件
5. （可选）打 tag 如 `v1.0.0` 推送，会自动发布到 Release

## 二、安装到飞牛 NAS

1. 飞牛 **应用中心 → 离线安装**，选择下载的 `.fpk` 文件安装
2. 安装后应用会自动启动（首次运行会在应用目录生成 `state/` 并记录基线）
3. 编辑配置：飞牛 **文件管理器** 找到应用目录（通常 `/var/lib/fnapps/fn-repo-monitor/` 或应用中心「文件」入口），编辑 `config.json`

## 三、配置 config.json

安装时若 `config.json` 不存在，会自动从 `config.json.example` 复制一份。填好以下字段：

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

改完配置后，在应用中心**重启应用**生效（或等下一轮检测自动加载新配置）。

## 四、获取 Server酱 SendKey

1. 打开 https://sct.ftqq.com ，微信扫码登录
2. 首页顶部复制你的 **SendKey**（免费版每天 5 条，每天一次检测足够用）

## 五、免费 AI 方案

- **DeepSeek**：https://platform.deepseek.com 注册即送免费额度，`deepseek-chat` 便宜且中文好。把 `api_key` 填入即可。
- 也可换成任意兼容 OpenAI 格式的接口（改 `api_base` 和 `model`）。
- 不想用 AI：把 `ai.enabled` 设为 `false`，程序只推送变更文件列表和提交记录，不解读。

## 六、本地测试

```bash
# 立即跑一轮检测（不等待定时）
python3 /path/to/app/src/monitor.py once

# 查看日志
cat /path/to/app/monitor.log

# 重置某个仓库的基线（删除 state 后会重新记录）
rm /path/to/app/state/OpenWrt-IPQ60XX.txt
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
