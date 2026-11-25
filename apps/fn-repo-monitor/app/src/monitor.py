#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞牛仓库更新监控 (fn-repo-monitor)
- 定时检测多个 GitHub 上游仓库的更新
- AI 解读变更内容与影响
- 判断是否建议更新固件/客户端
- 推送到 Server酱，带手动编译跳转链接

本模块既可作为命令行入口独立运行，也可被 server.py 导入复用。
"""
import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
import traceback
from pathlib import Path
from datetime import datetime, timedelta

# 代码目录（飞牛环境由 TRIM_APPDEST 注入，本地测试回退到脚本所在目录）
APP_DIR = Path(os.environ.get("TRIM_APPDEST", Path(__file__).resolve().parent.parent))


def resolve_data_dir():
    """配置/状态/日志放在持久目录，升级应用不丢失。"""
    candidates = []
    if os.environ.get("FN_MONITOR_DATA"):
        candidates.append(Path(os.environ["FN_MONITOR_DATA"]))
    if os.environ.get("TRIM_PKGVAR"):
        candidates.append(Path(os.environ["TRIM_PKGVAR"]))
    if os.environ.get("TRIM_APPDATA"):
        candidates.append(Path(os.environ["TRIM_APPDATA"]))
    appname = os.environ.get("TRIM_APPNAME")
    if appname:
        candidates.append(Path("/var/apps") / appname / "var")
    candidates.append(APP_DIR)
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            if c.is_dir() and os.access(c, os.W_OK):
                return c
        except Exception:
            continue
    return APP_DIR


DATA_DIR = resolve_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
STATE_DIR = DATA_DIR / "state"
LOG_PATH = DATA_DIR / "monitor.log"
STATUS_PATH = DATA_DIR / "status.json"
CONFIG_EXAMPLE = APP_DIR / "config.json.example"

MAX_LOG_LINES = 2000

# 同一时刻只允许一轮检测，避免手动触发与定时任务并发
RUN_LOCK = threading.Lock()
STATUS_LOCK = threading.Lock()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = f"[{now_str()}] {msg}"
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


def read_log_tail(lines=200):
    """读取日志末尾若干行，供 Web 界面展示。"""
    try:
        if not LOG_PATH.exists():
            return ""
        txt = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(txt[-max(1, int(lines)):])
    except Exception as e:
        return f"读取日志失败: {e}"


def load_status():
    with STATUS_LOCK:
        try:
            if STATUS_PATH.exists():
                data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}


def save_status(status):
    with STATUS_LOCK:
        try:
            tmp = STATUS_PATH.with_name("status.json.tmp")
            tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, STATUS_PATH)
        except Exception as e:
            log(f"[状态] 写入失败: {e}")


def parse_time(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def load_config():
    if not CONFIG_PATH.exists():
        # 首次运行：从模板生成配置，并提示用户填写
        if CONFIG_EXAMPLE.exists():
            CONFIG_PATH.write_text(CONFIG_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
            log(f"[配置] 已生成默认配置文件: {CONFIG_PATH}，请填写 Server酱 SendKey 后重启应用")
        else:
            raise FileNotFoundError(f"配置文件不存在: {CONFIG_PATH}")
    with open(CONFIG_PATH, encoding="utf-8-sig") as f:
        return json.load(f)


def save_config(config):
    """原子写入配置，避免界面保存时产生半截文件。"""
    tmp = CONFIG_PATH.with_name("config.json.tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


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
    sendkey = (sendkey or "").strip()
    # SendKey 格式校验：sctp{uid}t{token} 或 SCT{uid}t{token}
    if not re.match(r"^(?:sctp|SCT)\d+[tT]\w+$", sendkey):
        log("[Server酱] 未配置有效的 SendKey，跳过推送（请在 config.json 填入 sctp 或 SCT 开头的 SendKey）")
        return False
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    if sendkey.startswith("sctp"):
        # Server酱³: sctp{uid}t{secret}
        uid = re.match(r"^sctp(\d+)t", sendkey).group(1)
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
    """检测单个仓库，返回结构化结果供界面展示。"""
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
        return {"result": "error", "message": f"获取最新提交失败: {e}", "has_update": False}

    sha = latest["sha"]
    sp = state_path_for(name)
    last = sp.read_text().strip() if sp.exists() else ""

    if not last:
        log(f"[{name}] 首次运行，记录基线: {sha[:10]}")
        sp.write_text(sha)
        send_serverchan(
            f"✅ {name} 监控已就绪",
            f"已开始监控 **{upstream}** (`{branch}`)，当前最新提交 `{sha[:10]}`：{latest['message']}",
            config.get("serverchan", {}).get("sendkey", ""),
        )
        return {
            "result": "first_run",
            "message": f"已记录基线 {sha[:10]}：{latest['message']}",
            "has_update": False,
            "sha": sha,
        }

    if last == sha:
        log(f"[{name}] 无更新")
        return {
            "result": "no_update",
            "message": f"无更新（{sha[:10]}）",
            "has_update": False,
            "sha": sha,
        }

    log(f"[{name}] 发现更新: {last[:10]} -> {sha[:10]}")
    try:
        compare = get_compare(upstream, last, sha, token)
        summary = summarize_diff(compare, keywords)
    except Exception as e:
        log(f"[{name}] 获取 diff 失败: {e}，仅推送基础信息")
        summary = {"total_commits": 0, "total_files": 0, "key_files": [], "other_files": [], "commits": [latest["message"]]}

    ai_text, ai_meta = ai_analyze(upstream, summary, last, sha, config.get("ai"))

    title = f"🔔 {name} 上游更新"
    if ai_meta and ai_meta.get("suggestion") == "建议更新":
        title = f"🟢 {name} 建议更新（{ai_meta.get('urgency','中')}）"
    desp = build_message(repo_conf, summary, ai_text, ai_meta, last, sha)

    push_ok = send_serverchan(title, desp, config.get("serverchan", {}).get("sendkey", ""))
    sp.write_text(sha)
    log(f"[{name}] 处理完成")

    detail = f"{last[:10]} → {sha[:10]}，{summary['total_commits']} commits / {summary['total_files']} files"
    if ai_meta:
        detail += f"，建议：{ai_meta.get('suggestion')}"
    if not push_ok:
        detail += "（推送未发送）"
    return {
        "result": "updated",
        "message": detail,
        "has_update": True,
        "sha": sha,
        "base": last,
        "suggestion": (ai_meta or {}).get("suggestion"),
        "urgency": (ai_meta or {}).get("urgency"),
    }


def is_running():
    return RUN_LOCK.locked()


def run_once(config=None, trigger="scheduled"):
    """执行一轮检测。已有任务在跑时直接返回 False，避免并发。"""
    if not RUN_LOCK.acquire(blocking=False):
        log("已有一轮检测正在进行，跳过本次触发")
        return False

    if config is None:
        config = load_config()

    started = now_str()
    status = load_status()
    status.update({
        "running": True,
        "last_run_at": started,
        "last_run_trigger": trigger,
        "last_run_error": None,
    })
    status.setdefault("repos", {})
    save_status(status)

    log(f"===== 开始一轮检测（{trigger}）=====")
    try:
        for repo in config.get("repos", []):
            if not isinstance(repo, dict):
                continue
            name = repo.get("name") or repo.get("upstream", "?")
            status["repos"].setdefault(name, {})["checking"] = True
            save_status(status)
            try:
                res = check_one(repo, config)
            except Exception as e:
                log(f"[{name}] 异常: {e}\n{traceback.format_exc()}")
                res = {"result": "error", "message": f"检测异常: {e}", "has_update": False}
            entry = status["repos"].setdefault(name, {})
            entry.pop("checking", None)
            entry.update({
                "name": name,
                "upstream": repo.get("upstream", ""),
                "branch": repo.get("branch", "main"),
                "last_check_at": now_str(),
            })
            entry.update(res)
            save_status(status)
    finally:
        status["running"] = False
        status["last_run_end"] = now_str()
        save_status(status)
        log("===== 本轮检测结束 =====")
        RUN_LOCK.release()
    return True


def next_run_at(config=None):
    """根据上次检测时间与间隔推算下次检测时间。"""
    st = load_status()
    last = parse_time(st.get("last_run_at", ""))
    if last is None:
        return None
    if config is None:
        try:
            config = load_config()
        except Exception:
            config = {}
    try:
        interval = max(1, int(config.get("interval_hours", 24) or 24))
    except Exception:
        interval = 24
    return (last + timedelta(hours=interval)).strftime("%Y-%m-%d %H:%M:%S")


def run_forever(stop_event=None):
    """后台调度循环：每分钟检查一次是否到达下次检测时间。"""
    if stop_event is None:
        stop_event = threading.Event()
    log("监控调度启动")
    while not stop_event.is_set():
        try:
            config = load_config()
        except Exception as e:
            log(f"读取配置失败: {e}")
            config = {}
        st = load_status()
        last = parse_time(st.get("last_run_at", ""))
        try:
            interval = max(1, int(config.get("interval_hours", 24) or 24))
        except Exception:
            interval = 24

        if last is None:
            wait_seconds = 0
        else:
            remain = (last + timedelta(hours=interval) - datetime.now()).total_seconds()
            wait_seconds = 0 if remain <= 0 else min(60, remain)

        if wait_seconds <= 0:
            run_once(config, trigger="scheduled")
        else:
            stop_event.wait(wait_seconds)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        run_once(load_config(), trigger="manual")
        return
    log("监控启动")
    try:
        run_forever()
    except KeyboardInterrupt:
        log("监控已停止")


if __name__ == "__main__":
    main()
