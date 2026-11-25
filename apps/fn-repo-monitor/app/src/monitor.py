#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞牛仓库更新监控 (fn-repo-monitor)
- 定时检测多个 GitHub 上游仓库的更新
- AI 解读变更内容与影响
- 判断是否建议更新固件/客户端
- 推送到 Server酱，带手动编译跳转链接
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import traceback
from pathlib import Path
from datetime import datetime

# 应用目录（飞牛环境由 TRIM_APPDEST 注入，本地测试回退到脚本所在目录）
APP_DIR = Path(os.environ.get("TRIM_APPDEST", Path(__file__).resolve().parent.parent))
CONFIG_PATH = APP_DIR / "config.json"
STATE_DIR = APP_DIR / "state"
LOG_PATH = APP_DIR / "monitor.log"

MAX_LOG_LINES = 2000


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # 控制日志大小
        if LOG_PATH.exists():
            txt = LOG_PATH.read_text(encoding="utf-8").splitlines()
            if len(txt) > MAX_LOG_LINES:
                LOG_PATH.write_text("\n".join(txt[-MAX_LOG_LINES:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def load_config():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"配置文件不存在: {CONFIG_PATH}")
    with open(CONFIG_PATH, encoding="utf-8-sig") as f:
        return json.load(f)


def gh_api(url, token=None):
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "fn-repo-monitor")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_latest_commit(repo, branch, token=None):
    url = f"https://api.github.com/repos/{repo}/commits/{urllib.parse.quote(branch)}"
    data = gh_api(url, token)
    return {
        "sha": data["sha"],
        "message": data["commit"]["message"].split("\n")[0],
        "author": data["commit"]["author"]["name"],
        "date": data["commit"]["author"]["date"],
    }


def get_compare(repo, base, head, token=None):
    url = f"https://api.github.com/repos/{repo}/compare/{base}...{head}"
    return gh_api(url, token)


def summarize_diff(compare, keywords):
    commits = compare.get("commits", [])
    files = compare.get("files", [])
    key_files = []
    other_files = []
    kw_lower = [k.lower() for k in keywords]
    for f in files:
        fn = f.get("filename", "")
        if any(k in fn.lower() for k in kw_lower):
            key_files.append(fn)
        else:
            other_files.append(fn)
    commit_lines = []
    for c in commits[:15]:
        sha = c["sha"][:10]
        msg = c["commit"]["message"].split("\n")[0]
        author = c["commit"]["author"]["name"]
        date = c["commit"]["author"]["date"][:16].replace("T", " ")
        commit_lines.append(f"[{sha}] {msg}  ({author}, {date})")
    return {
        "total_commits": len(commits),
        "total_files": len(files),
        "key_files": key_files,
        "other_files": other_files,
        "commits": commit_lines,
    }


def ai_analyze(repo_name, summary, base, head, ai_conf):
    """调用 AI 解读变更，返回解读文本 + 是否建议更新"""
    if not ai_conf or not ai_conf.get("enabled"):
        return None, None
    api_key = ai_conf.get("api_key", "").strip()
    if not api_key:
        return None, None

    api_base = ai_conf.get("api_base", "https://api.deepseek.com/v1").rstrip("/")
    model = ai_conf.get("model", "deepseek-chat")

    files_text = "\n".join(summary["key_files"] + summary["other_files"][:15])
    commits_text = "\n".join(summary["commits"])
    diff_url = f"https://github.com/{repo_name}/compare/{base}...{head}"

    prompt = f"""你是开源项目维护助手。下面是仓库 {repo_name} 的上游更新（{base[:10]} → {head[:10]}）。

提交记录：
{commits_text}

变更文件：
{files_text}

完整 diff：{diff_url}

请分析：
1. 这些变更主要做了什么（用中文，分点简述）
2. 是否包含重要修复、新功能、依赖升级或安全更新
3. 对于正在使用该项目的固件/客户端，是否建议跟进更新（必须明确回答：建议更新 / 可以暂缓 / 无需更新）
4. 建议的紧迫程度（高/中/低）

输出格式（严格按此格式，不要多余内容）：
作用：<一句话概括>
详情：<2-4个要点>
建议：<建议更新|可以暂缓|无需更新>
紧迫度：<高|中|低>
"""

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 800,
    }).encode("utf-8")

    req = urllib.request.Request(f"{api_base}/chat/completions", data=payload)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"].strip()
            suggestion = "可以暂缓"
            urgency = "中"
            for line in text.splitlines():
                if line.startswith("建议："):
                    suggestion = line.replace("建议：", "").strip()
                if line.startswith("紧迫度："):
                    urgency = line.replace("紧迫度：", "").strip()
            return text, {"suggestion": suggestion, "urgency": urgency}
    except Exception as e:
        log(f"[AI] 解读失败: {e}")
        return None, None


def send_serverchan(title, desp, sendkey):
    if not sendkey:
        log("[Server酱] 未配置 sendkey，跳过推送")
        return False
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    if sendkey.startswith("sctp"):
        # Server酱³: sctp{uid}t{secret}
        uid = ""
        for ch in sendkey[4:]:
            if ch == "t":
                break
            uid += ch
        url = f"https://{uid}.push.ft07.com/send/{sendkey}.send"
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            log(f"[Server酱] 推送响应: {body[:120]}")
            return True
    except Exception as e:
        log(f"[Server酱] 推送失败: {e}")
        return False


def build_message(repo_conf, summary, ai_text, ai_meta, base, head):
    name = repo_conf["name"]
    build_url = repo_conf.get("build_url", "")
    diff_url = f"https://github.com/{repo_conf['upstream']}/compare/{base}...{head}"

    lines = []
    if ai_meta:
        sug = ai_meta.get("suggestion", "可以暂缓")
        urg = ai_meta.get("urgency", "中")
        if sug == "建议更新":
            prefix = "🟢 建议更新"
        elif sug == "无需更新":
            prefix = "⚪ 无需更新"
        else:
            prefix = "🟡 可以暂缓"
        lines.append(f"## {prefix}（紧迫度：{urg}）")
    else:
        lines.append(f"## 🔔 检测到更新")
    lines.append("")
    lines.append(f"**{name}** 上游有新提交")
    lines.append(f"范围：`{base[:10]}` → `{head[:10]}`")
    lines.append(f"统计：{summary['total_commits']} commits / {summary['total_files']} files")
    lines.append("")

    if summary["key_files"]:
        lines.append("### 🎯 相关变更")
        lines.append("```")
        for f in summary["key_files"][:15]:
            lines.append(f)
        lines.append("```")
        lines.append("")

    lines.append("### 📝 提交记录")
    for c in summary["commits"][:10]:
        lines.append(f"- {c}")
    lines.append("")

    if ai_text:
        lines.append("### 🤖 AI 解读")
        lines.append(ai_text)
        lines.append("")

    lines.append("### 🔗 链接")
    lines.append(f"- [完整 Diff]({diff_url})")
    if build_url:
        lines.append(f"- [👉 去编译固件]({build_url})")
    return "\n".join(lines)


def state_path_for(repo_name):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = repo_name.replace("/", "_").replace(" ", "_")
    return STATE_DIR / f"{safe}.txt"


def check_one(repo_conf, config):
    name = repo_conf["name"]
    upstream = repo_conf["upstream"]
    branch = repo_conf.get("branch", "main")
    keywords = repo_conf.get("keywords", [])
    token = config.get("github_token", "") or None

    log(f"[{name}] 开始检测 {upstream}@{branch}")

    try:
        latest = get_latest_commit(upstream, branch, token)
    except Exception as e:
        log(f"[{name}] 获取最新 commit 失败: {e}")
        return

    sp = state_path_for(name)
    last = sp.read_text().strip() if sp.exists() else ""

    if not last:
        log(f"[{name}] 首次运行，记录基线: {latest['sha'][:10]}")
        sp.write_text(latest["sha"])
        send_serverchan(
            f"✅ {name} 监控已就绪",
            f"已开始监控 **{upstream}** (`{branch}`)，当前最新提交 `{latest['sha'][:10]}`：{latest['message']}",
            config.get("serverchan", {}).get("sendkey", ""),
        )
        return

    if last == latest["sha"]:
        log(f"[{name}] 无更新")
        return

    log(f"[{name}] 发现更新: {last[:10]} -> {latest['sha'][:10]}")
    try:
        compare = get_compare(upstream, last, latest["sha"], token)
        summary = summarize_diff(compare, keywords)
    except Exception as e:
        log(f"[{name}] 获取 diff 失败: {e}，仅推送基础信息")
        summary = {"total_commits": 0, "total_files": 0, "key_files": [], "other_files": [], "commits": [latest["message"]]}

    ai_text, ai_meta = ai_analyze(upstream, summary, last, latest["sha"], config.get("ai"))

    title = f"🔔 {name} 上游更新"
    if ai_meta and ai_meta.get("suggestion") == "建议更新":
        title = f"🟢 {name} 建议更新（{ai_meta.get('urgency','中')}）"
    desp = build_message(repo_conf, summary, ai_text, ai_meta, last, latest["sha"])

    send_serverchan(title, desp, config.get("serverchan", {}).get("sendkey", ""))
    sp.write_text(latest["sha"])
    log(f"[{name}] 处理完成")


def run_once(config):
    log("===== 开始一轮检测 =====")
    for repo in config.get("repos", []):
        try:
            check_one(repo, config)
        except Exception as e:
            log(f"[{repo.get('name','?')}] 异常: {e}\n{traceback.format_exc()}")
    log("===== 本轮检测结束 =====")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        run_once(load_config())
        return
    interval = max(1, int(load_config().get("interval_hours", 24)))
    log(f"监控启动，检测间隔 {interval} 小时")
    while True:
        try:
            run_once(load_config())
        except Exception as e:
            log(f"主循环异常: {e}\n{traceback.format_exc()}")
        time.sleep(interval * 3600)


if __name__ == "__main__":
    main()
