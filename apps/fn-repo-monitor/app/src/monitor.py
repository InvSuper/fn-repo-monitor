#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub仓库更新监控 (fn-repo-monitor)
- 定时检测多个 GitHub 仓库的更新（任意提交或仅正式版 Release）
- AI 解读变更内容与影响，判断是否建议跟进
- 推送到 Server酱，可附自定义跳转链接

本模块既可作为命令行入口独立运行，也可被 server.py 导入复用。
"""
import http.client
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


def parse_daily_time(value):
    """解析每天固定检测时间，返回 (hour, minute)，非法或留空返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def compute_daily_due(daily, last_run, now=None):
    """返回下一次固定检测时间点。
    已到今天的检测点、且上次检测早于该时间点时，返回 now 表示应立刻执行。
    """
    now = now or datetime.now()
    today = now.replace(hour=daily[0], minute=daily[1], second=0, microsecond=0)
    if now < today:
        return today
    if last_run is None or last_run < today:
        return now
    return today + timedelta(days=1)


def load_config():
    if not CONFIG_PATH.exists():
        # 首次运行：从模板生成配置，并提示用户填写
        if CONFIG_EXAMPLE.exists():
            CONFIG_PATH.write_text(CONFIG_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
            log(f"[配置] 已生成默认配置文件: {CONFIG_PATH}，请打开应用界面填写 Server酱 SendKey 并添加监控仓库")
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
    """请求 GitHub API，网络中断或 5xx 时自动重试 2 次。"""
    last_error = None
    for attempt in range(3):
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "fn-repo-monitor")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < 2:
                wait = 2 * (attempt + 1)
                log(f"[GitHub] 服务端错误 {e.code}，{wait} 秒后重试（{attempt + 1}/2）")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, http.client.IncompleteRead, OSError) as e:
            last_error = e
            if attempt < 2:
                wait = 2 * (attempt + 1)
                log(f"[GitHub] 请求失败({e})，{wait} 秒后重试（{attempt + 1}/2）")
                time.sleep(wait)
                continue
            raise
    raise last_error


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


def get_latest_stable_release(repo, token=None):
    """取最近一个正式版 Release（跳过草稿和预发布）。"""
    url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
    data = gh_api(url, token)
    if not isinstance(data, list):
        return None
    for release in data:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name") or ""
        if not tag:
            continue
        return {
            "tag": tag,
            "name": release.get("name") or tag,
            "body": release.get("body") or "",
            "url": release.get("html_url") or f"https://github.com/{repo}/releases/tag/{tag}",
            "published_at": release.get("published_at") or release.get("created_at") or "",
        }
    return None


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
    commit_texts = []
    for c in commits:
        sha = c["sha"][:10]
        msg = c["commit"]["message"].split("\n")[0]
        commit_texts.append(f"{sha} {msg}")
        if len(commit_lines) < 15:
            author = c["commit"]["author"]["name"]
            date = c["commit"]["author"]["date"][:16].replace("T", " ")
            commit_lines.append(f"[{sha}] {msg}  ({author}, {date})")
    return {
        "total_commits": len(commits),
        "total_files": len(files),
        "key_files": key_files,
        "other_files": other_files,
        "commits": commit_lines,
        "commit_texts": commit_texts,
    }


class AIError(Exception):
    """AI 接口调用失败，message 保留可直接展示给用户的原因。"""


def _ai_available(ai_conf):
    return bool(ai_conf and ai_conf.get("enabled") and str(ai_conf.get("api_key", "")).strip())


def _parse_ai_meta(text):
    suggestion = "可以暂缓"
    urgency = "中"
    for line in text.splitlines():
        if line.startswith("建议："):
            suggestion = line.replace("建议：", "").strip()
        elif line.startswith("紧迫度："):
            urgency = line.replace("紧迫度：", "").strip()
    return {"suggestion": suggestion, "urgency": urgency}


def _call_ai(prompt, ai_conf, retries=2):
    api_base = ai_conf.get("api_base", "https://api.deepseek.com/v1").rstrip("/")
    model = ai_conf.get("model", "deepseek-chat")
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 800,
    }).encode("utf-8")

    req = urllib.request.Request(f"{api_base}/chat/completions", data=payload)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {str(ai_conf.get('api_key', '')).strip()}")

    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace").strip()
            except Exception:
                pass
            message = f"HTTP {e.code} {e.reason}"
            if detail:
                message = f"{message}：{detail[:300]}"
            retry_after = 0
            try:
                header = e.headers.get("Retry-After") if e.headers else None
                if header:
                    retry_after = int(str(header).strip())
            except Exception:
                retry_after = 0
            if e.code == 429 and retry_after:
                message = f"{message}（建议 {retry_after} 秒后重试）"
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = retry_after if retry_after > 0 else min(30, 5 * (attempt + 1))
                log(f"[AI] 调用失败({message})，{wait} 秒后重试（{attempt + 1}/{retries}）")
                time.sleep(wait)
                attempt += 1
                continue
            log(f"[AI] 调用失败: {message}")
            raise AIError(message)
        except urllib.error.URLError as e:
            message = f"网络连接失败：{e.reason}"
            log(f"[AI] 调用失败: {message}")
            raise AIError(message)
        except Exception as e:
            if attempt < retries and isinstance(e, (TimeoutError, OSError)):
                wait = min(30, 5 * (attempt + 1))
                log(f"[AI] 调用超时或连接中断，{wait} 秒后重试（{attempt + 1}/{retries}）")
                time.sleep(wait)
                attempt += 1
                continue
            message = f"{type(e).__name__}：{e}"
            log(f"[AI] 调用失败: {message}")
            raise AIError(message)

    try:
        data = json.loads(raw)
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"[AI] 返回内容解析失败: {raw[:300]}")
        raise AIError(f"返回内容解析失败：{e}")


def keyword_hit(summary, keywords):
    """判断变更是否命中关键词：文件路径或提交标题任一命中即可。"""
    kw = [str(k).strip().lower() for k in (keywords or []) if str(k).strip()]
    if not kw:
        return True
    if summary.get("key_files"):
        return True
    for commit in summary.get("commit_texts") or summary.get("commits", []):
        text = str(commit).lower()
        if any(k in text for k in kw):
            return True
    return False


def ai_analyze(repo_name, summary, base, head, ai_conf, keywords=None):
    """调用 AI 解读提交变更，返回解读文本 + 是否建议更新"""
    if not _ai_available(ai_conf):
        return None, None

    files_text = "\n".join(summary["key_files"] + summary["other_files"][:15])
    commits_text = "\n".join(summary["commits"])
    diff_url = f"https://github.com/{repo_name}/compare/{base}...{head}"
    focus = ""
    if keywords:
        focus = f"\n用户重点关注：{', '.join(str(k) for k in keywords)}。请优先说明本次变更是否影响这些方向。\n"

    prompt = f"""你是开源项目维护助手。下面是仓库 {repo_name} 的上游更新（{base[:10]} → {head[:10]}）。
{focus}
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

    text = None
    try:
        text = _call_ai(prompt, ai_conf)
    except AIError:
        return None, None
    if not text:
        return None, None
    return text, _parse_ai_meta(text)


def ai_analyze_release(repo_name, release, ai_conf, keywords=None):
    """调用 AI 解读正式版发布说明。"""
    if not _ai_available(ai_conf):
        return None, None

    published = (release.get("published_at") or "")[:10]
    body = (release.get("body") or "").strip()[:6000] or "（发布说明为空）"
    focus = ""
    if keywords:
        focus = f"\n用户重点关注：{', '.join(str(k) for k in keywords)}。请优先说明这个版本是否影响这些方向。\n"

    prompt = f"""你是开源项目维护助手。下面是仓库 {repo_name} 的正式版发布说明（{release['tag']}，{published}）。
{focus}
{body}

请分析：
1. 这个版本主要更新了什么（用中文，分点简述）
2. 是否包含重要修复、新功能、破坏性变更或安全更新
3. 对于基于该项目维护自定义分支的使用者，是否建议现在同步更新（必须明确回答：建议更新 / 可以暂缓 / 无需更新）
4. 建议的紧迫程度（高/中/低）

输出格式（严格按此格式，不要多余内容）：
作用：<一句话概括>
详情：<2-4个要点>
建议：<建议更新|可以暂缓|无需更新>
紧迫度：<高|中|低>
"""

    text = None
    try:
        text = _call_ai(prompt, ai_conf)
    except AIError:
        return None, None
    if not text:
        return None, None
    return text, _parse_ai_meta(text)


def test_ai_connection(ai_conf):
    """用一次最小请求验证 AI 接口是否可用，返回 (是否成功, 提示信息)。"""
    if not str((ai_conf or {}).get("api_key", "")).strip():
        return False, "请先填写 AI API Key"
    try:
        text = _call_ai("这是一次连接测试。请只回复：AI 连接正常", ai_conf or {}, retries=0)
    except AIError as e:
        message = str(e)
        if "HTTP 401" in message:
            return False, f"API Key 无效或已失效（{message}）"
        if "HTTP 402" in message:
            return False, f"账户余额不足，需要先充值（{message}）"
        if "HTTP 404" in message:
            return False, f"接口地址或模型名称不正确（{message}）"
        if "HTTP 429" in message:
            if "1305" in message:
                return False, f"免费模型当前访问量过大，请稍后再试或换用其他免费模型（{message}）"
            if "1302" in message:
                return False, f"账户触发速率限制，免费模型并发较低，请等 1 分钟后重试；检测正在运行时也不要点测试（{message}）"
            return False, f"请求被限流，请稍后重试（{message}）"
        return False, message
    if not text:
        return False, "接口返回了空内容"
    return True, text.strip()[:200]


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


def build_release_message(repo_conf, release, ai_text, ai_meta, summary=None, prev_tag=""):
    lines = []
    if ai_meta:
        sug = ai_meta.get("suggestion", "可以暂缓")
        urg = ai_meta.get("urgency", "中")
        if sug == "建议更新":
            prefix = "🟢 建议同步"
        elif sug == "无需更新":
            prefix = "⚪ 无需更新"
        else:
            prefix = "🟡 可以暂缓"
        lines.append(f"## {prefix}（紧迫度：{urg}）")
    else:
        lines.append("## 🚀 发布正式版")
    lines.append("")
    lines.append(f"**{repo_conf['name']}** 发布 `{release['tag']}`")
    if release.get("name") and release["name"] != release["tag"]:
        lines.append(f"版本名称：{release['name']}")
    if release.get("published_at"):
        lines.append(f"发布时间：{release['published_at'][:10]}")
    if prev_tag:
        lines.append(f"范围：`{prev_tag}` → `{release['tag']}`")
    if summary is not None:
        lines.append(f"统计：{summary['total_commits']} commits / {summary['total_files']} files")
    lines.append("")

    body = (release.get("body") or "").strip()
    if body:
        lines.append("### 📄 发布说明")
        lines.append(body[:3000])
        if len(body) > 3000:
            lines.append("")
            lines.append("（发布说明较长，已截断，完整内容见下方链接）")
        lines.append("")

    if summary and summary.get("key_files"):
        lines.append("### 🎯 相关变更")
        lines.append("```")
        for f in summary["key_files"][:15]:
            lines.append(f)
        lines.append("```")
        lines.append("")

    if summary and summary.get("commits"):
        lines.append("### 📝 提交记录")
        for c in summary["commits"][:10]:
            lines.append(f"- {c}")
        lines.append("")

    if ai_text:
        lines.append("### 🤖 AI 解读")
        lines.append(ai_text)
        lines.append("")

    lines.append("### 🔗 链接")
    lines.append(f"- [Release 页面]({release.get('url', '')})")
    if summary is not None and prev_tag:
        diff_url = f"https://github.com/{repo_conf['upstream']}/compare/{prev_tag}...{release['tag']}"
        lines.append(f"- [版本间完整 Diff]({diff_url})")
    build_url = repo_conf.get("build_url", "")
    if build_url:
        lines.append(f"- [👉 去同步/编译]({build_url})")
    return "\n".join(lines)


def state_path_for(repo_name):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = repo_name.replace("/", "_").replace(" ", "_")
    return STATE_DIR / f"{safe}.txt"


def check_one(repo_conf, config):
    """按监控方式分流：commit（任意提交）或 release（仅正式版）。"""
    if (repo_conf.get("watch") or "commit").lower() == "release":
        return check_release_repo(repo_conf, config)
    return check_commit_repo(repo_conf, config)


def check_release_repo(repo_conf, config):
    """只监控正式版 Release，忽略草稿和预发布。"""
    name = repo_conf["name"]
    upstream = repo_conf["upstream"]
    token = config.get("github_token", "") or None

    log(f"[{name}] 检测正式版 Release: {upstream}")

    try:
        release = get_latest_stable_release(upstream, token)
    except Exception as e:
        log(f"[{name}] 获取 Release 失败: {e}")
        return {"result": "error", "message": f"获取 Release 失败: {e}", "has_update": False}

    if not release:
        log(f"[{name}] 没有找到正式版 Release")
        return {
            "result": "error",
            "message": "上游没有正式版 Release（可能只发预发布或未发版）",
            "has_update": False,
        }

    tag = release["tag"]
    marker = f"release:{tag}"
    sp = state_path_for(name)
    last = sp.read_text().strip() if sp.exists() else ""

    # 首次使用 / 从提交模式切换过来：只记录基线
    if not last.startswith("release:"):
        log(f"[{name}] 首次使用正式版模式，记录基线: {tag}")
        sp.write_text(marker)
        send_serverchan(
            f"✅ {name} 正式版监控已就绪",
            f"将只在 **{upstream}** 发布正式版时提醒，当前最新正式版为 `{tag}`。",
            config.get("serverchan", {}).get("sendkey", ""),
        )
        return {
            "result": "first_run",
            "message": f"已记录正式版基线 {tag}",
            "has_update": False,
            "sha": tag,
        }

    if last == marker:
        log(f"[{name}] 无新正式版")
        return {
            "result": "no_update",
            "message": f"无新正式版（{tag}）",
            "has_update": False,
            "sha": tag,
        }

    prev_tag = last[len("release:"):]
    log(f"[{name}] 发现新正式版: {prev_tag} -> {tag}")

    keywords = repo_conf.get("keywords", [])
    summary = None
    try:
        compare = get_compare(upstream, prev_tag, tag, token)
        summary = summarize_diff(compare, keywords)
    except Exception as e:
        log(f"[{name}] 获取 {prev_tag}...{tag} 差异失败: {e}，回退到发布说明")

    if repo_conf.get("keyword_only"):
        if summary is not None:
            hit = keyword_hit(summary, keywords)
        else:
            body = (release.get("body") or "").lower()
            kw = [str(k).strip().lower() for k in keywords if str(k).strip()]
            hit = not kw or not body or any(k in body for k in kw)
            if not body:
                log(f"[{name}] 无法获取差异且发布说明为空，无法过滤关键词，仍推送")
        if not hit:
            sp.write_text(marker)
            log(f"[{name}] 未命中关键词，跳过推送（基线已更新）")
            return {
                "result": "skipped",
                "message": f"{prev_tag} → {tag} 无关键词相关变更，已跳过推送",
                "has_update": False,
                "sha": tag,
            }

    if summary is not None:
        ai_text, ai_meta = ai_analyze(upstream, summary, prev_tag, tag, config.get("ai"), keywords)
    else:
        ai_text, ai_meta = ai_analyze_release(upstream, release, config.get("ai"), keywords)

    title = f"🚀 {name} 发布 {tag}"
    if ai_meta and ai_meta.get("suggestion") == "建议更新":
        title = f"🟢 {name} {tag} 建议同步（{ai_meta.get('urgency','中')}）"
    desp = build_release_message(repo_conf, release, ai_text, ai_meta, summary, prev_tag)

    push_ok = send_serverchan(title, desp, config.get("serverchan", {}).get("sendkey", ""))
    sp.write_text(marker)
    log(f"[{name}] 正式版处理完成")

    detail = f"{prev_tag} → {tag}"
    if summary is not None:
        detail += f"，{summary['total_commits']} commits / {summary['total_files']} files"
    if ai_meta:
        detail += f"，建议：{ai_meta.get('suggestion')}"
    if not push_ok:
        detail += "（推送未发送）"
    return {
        "result": "updated",
        "message": detail,
        "has_update": True,
        "sha": tag,
        "base": prev_tag,
        "suggestion": (ai_meta or {}).get("suggestion"),
        "urgency": (ai_meta or {}).get("urgency"),
    }


def check_commit_repo(repo_conf, config):
    """监控分支上的任意新提交。"""
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

    if repo_conf.get("keyword_only") and not keyword_hit(summary, keywords):
        sp.write_text(sha)
        log(f"[{name}] 未命中关键词，跳过推送（基线已更新）")
        return {
            "result": "skipped",
            "message": f"无关键词相关变更（{summary['total_commits']} commits），已跳过推送",
            "has_update": False,
            "sha": sha,
        }

    ai_text, ai_meta = ai_analyze(upstream, summary, last, sha, config.get("ai"), keywords)

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
            if repo.get("enabled") is False:
                log(f"[{name}] 已停用，跳过检测")
                continue
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


def run_repo_once(repo, config=None):
    """单独重试一个仓库，与整轮检测共用锁。返回 (是否执行, 结果字典)。"""
    if repo.get("enabled") is False:
        return False, {"result": "error", "message": "该仓库已停用，请先启用再检测"}
    if not RUN_LOCK.acquire(blocking=False):
        log("已有一轮检测正在进行，跳过单仓库重试")
        return False, {"result": "error", "message": "已有一轮检测正在进行，请稍后再试"}

    if config is None:
        config = load_config()
    name = repo.get("name") or repo.get("upstream", "?")

    status = load_status()
    status.setdefault("repos", {})
    status["repos"].setdefault(name, {})["checking"] = True
    save_status(status)

    log(f"[{name}] 开始单仓库重试")
    try:
        try:
            res = check_one(repo, config)
        except Exception as e:
            log(f"[{name}] 异常: {e}\n{traceback.format_exc()}")
            res = {"result": "error", "message": f"检测异常: {e}", "has_update": False}

        status = load_status()
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
        log(f"[{name}] 单仓库重试结束: {res.get('message', '')}")
        return True, res
    finally:
        RUN_LOCK.release()


def next_run_at(config=None):
    """推算下次检测时间：优先使用每天固定时间，否则按间隔。"""
    st = load_status()
    last = parse_time(st.get("last_run_at", ""))
    if config is None:
        try:
            config = load_config()
        except Exception:
            config = {}

    daily = parse_daily_time(config.get("daily_time"))
    if daily:
        due = compute_daily_due(daily, last)
        if due <= datetime.now():
            return "即将执行"
        return due.strftime("%Y-%m-%d %H:%M:%S")

    if last is None:
        return None
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

        daily = parse_daily_time(config.get("daily_time"))
        if daily:
            due = compute_daily_due(daily, last)
            remain = (due - datetime.now()).total_seconds()
        else:
            try:
                interval = max(1, int(config.get("interval_hours", 24) or 24))
            except Exception:
                interval = 24
            if last is None:
                remain = 0
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
