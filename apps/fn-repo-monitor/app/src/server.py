#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fn-repo-monitor Web 后端
- 提供 /api/* 接口供 app/ui 页面调用
- 后台线程按 interval_hours 定时执行检测
- 静态文件仅在本地调试时直出；飞牛环境由 app/ui/index.cgi 同源转发

服务只监听 127.0.0.1，对外访问统一走飞牛 nginx 的
/cgi/ThirdParty/fn-repo-monitor/index.cgi/ 入口，避免额外暴露端口。
"""
import argparse
import json
import os
import re
import signal
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import monitor

MAX_BODY = 1024 * 1024  # 单次请求体上限 1MB
STARTED_AT = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
APP_VERSION = os.environ.get("TRIM_APPVER", "1.5.0")
WEB_DIR = monitor.APP_DIR / "ui"
STOP_EVENT = threading.Event()


def read_baseline(repo_name):
    try:
        sp = monitor.state_path_for(repo_name)
        return sp.read_text(encoding="utf-8").strip() if sp.exists() else ""
    except Exception:
        return ""


def load_config_safe():
    try:
        return monitor.load_config()
    except Exception as e:
        return {"_error": str(e), "repos": [], "serverchan": {}, "ai": {}}


def normalize_config(raw):
    """校验并规范化前端提交的配置，避免写入坏数据导致进程起不来。"""
    if not isinstance(raw, dict):
        raise ValueError("配置必须是 JSON 对象")

    try:
        interval = int(raw.get("interval_hours", 24) or 24)
    except Exception:
        raise ValueError("检测间隔必须是整数小时")
    if interval < 1 or interval > 720:
        raise ValueError("检测间隔需在 1 到 720 小时之间")

    daily_time = str(raw.get("daily_time", "") or "").strip()
    if daily_time:
        match = re.match(r"^(\d{1,2}):(\d{2})$", daily_time)
        if not match:
            raise ValueError("每天固定检测时间格式应为 HH:MM（例如 08:30），留空则按间隔执行")
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ValueError("每天固定检测时间格式应为 HH:MM（例如 08:30），留空则按间隔执行")
        daily_time = f"{hour:02d}:{minute:02d}"

    repos = raw.get("repos", [])
    if not isinstance(repos, list):
        raise ValueError("仓库列表格式不正确")

    normalized_repos = []
    for idx, item in enumerate(repos, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {idx} 个仓库格式不正确")
        name = str(item.get("name", "")).strip()
        upstream = str(item.get("upstream", "")).strip()
        if not name:
            raise ValueError(f"第 {idx} 个仓库缺少名称")
        if not re.match(r"^[\w.-]+/[\w.-]+$", upstream):
            raise ValueError(f"仓库「{name}」的 upstream 需为 owner/repo 格式")
        keywords = item.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.replace("，", ",").split(",") if k.strip()]
        if not isinstance(keywords, list):
            keywords = []
        watch = str(item.get("watch", "commit") or "commit").strip().lower()
        if watch not in ("commit", "release"):
            watch = "commit"
        enabled = item.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in ("false", "0", "no", "off", "")
        normalized_repos.append({
            "name": name,
            "upstream": upstream,
            "branch": str(item.get("branch", "main")).strip() or "main",
            "watch": watch,
            "enabled": bool(enabled),
            "keywords": [str(k).strip() for k in keywords if str(k).strip()],
            "build_url": str(item.get("build_url", "")).strip(),
        })

    config = dict(raw)
    config["interval_hours"] = interval
    config["daily_time"] = daily_time
    config["github_token"] = str(raw.get("github_token", "") or "").strip()
    serverchan = raw.get("serverchan") if isinstance(raw.get("serverchan"), dict) else {}
    config["serverchan"] = dict(serverchan)
    config["serverchan"]["sendkey"] = str(serverchan.get("sendkey", "") or "").strip()
    ai = raw.get("ai") if isinstance(raw.get("ai"), dict) else {}
    config["ai"] = dict(ai)
    config["ai"]["enabled"] = bool(ai.get("enabled", True))
    config["ai"]["api_base"] = str(ai.get("api_base", "https://api.deepseek.com/v1") or "").strip()
    config["ai"]["api_key"] = str(ai.get("api_key", "") or "").strip()
    config["ai"]["model"] = str(ai.get("model", "deepseek-chat") or "").strip()
    config["repos"] = normalized_repos
    return config


def build_status():
    config = load_config_safe()
    status = monitor.load_status()
    status_repos = status.get("repos") if isinstance(status.get("repos"), dict) else {}
    try:
        interval = max(1, int(config.get("interval_hours", 24) or 24))
    except Exception:
        interval = 24

    repos = []
    for repo in config.get("repos", []):
        if not isinstance(repo, dict):
            continue
        name = repo.get("name", "")
        entry = status_repos.get(name, {}) or {}
        repos.append({
            "name": name,
            "upstream": repo.get("upstream", ""),
            "branch": repo.get("branch", "main"),
            "watch": repo.get("watch", "commit"),
            "enabled": repo.get("enabled", True) is not False,
            "baseline": read_baseline(name),
            "checking": bool(entry.get("checking")),
            "last_check_at": entry.get("last_check_at", ""),
            "result": entry.get("result", ""),
            "message": entry.get("message", ""),
            "has_update": bool(entry.get("has_update")),
            "suggestion": entry.get("suggestion"),
            "urgency": entry.get("urgency"),
        })

    sendkey = (config.get("serverchan") or {}).get("sendkey", "") if isinstance(config.get("serverchan"), dict) else ""
    ai = config.get("ai") if isinstance(config.get("ai"), dict) else {}
    warnings = []
    if config.get("_error"):
        warnings.append(f"读取配置失败：{config['_error']}")
    if not sendkey:
        warnings.append("尚未配置 Server酱 SendKey，检测结果不会推送")
    if not config.get("repos"):
        warnings.append("尚未添加监控仓库，请在下方仓库列表中添加")
    if not ai.get("enabled"):
        warnings.append("AI 解读已关闭，仅推送原始摘要")
    elif not ai.get("api_key"):
        warnings.append("AI 未配置 api_key，将跳过解读")

    return {
        "server": {
            "version": APP_VERSION,
            "started_at": STARTED_AT,
            "pid": os.getpid(),
            "data_dir": str(monitor.DATA_DIR),
        },
        "schedule": {
            "interval_hours": interval,
            "daily_time": str(config.get("daily_time", "") or ""),
            "last_run_at": status.get("last_run_at", ""),
            "last_run_end": status.get("last_run_end", ""),
            "last_run_trigger": status.get("last_run_trigger", ""),
            "next_run_at": monitor.next_run_at(config) or "",
            "running": monitor.is_running(),
        },
        "repos": repos,
        "warnings": warnings,
    }


class QuietServer(ThreadingHTTPServer):
    """客户端主动断开连接时不打印异常，避免日志被噪音填满。"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "fn-repo-monitor"
    protocol_version = "HTTP/1.1"

    # ---------- 基础响应 ----------

    def _send(self, code, body=b"", content_type="application/json; charset=utf-8", extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False))

    def _read_raw(self):
        """读取完整请求体。任何 POST 都必须读干净，否则长连接会串请求。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        return self.rfile.read(length)

    def _read_json(self):
        raw = getattr(self, "_raw_body", b"")
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8-sig"))

    def log_message(self, fmt, *args):
        # 避免把每个请求写进监控日志
        pass

    # ---------- 路由 ----------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            self._json(200, build_status())
            return
        if path == "/api/config":
            config = load_config_safe()
            if config.get("_error"):
                self._json(500, {"error": config["_error"]})
                return
            self._json(200, config)
            return
        if path == "/api/log":
            lines = 200
            if "?" in self.path:
                query = self.path.split("?", 1)[1]
                for part in query.split("&"):
                    if part.startswith("lines="):
                        try:
                            lines = int(part.split("=", 1)[1])
                        except Exception:
                            pass
            self._json(200, {"log": monitor.read_log_tail(lines)})
            return
        if path.startswith("/api/"):
            self._json(404, {"error": "接口不存在"})
            return
        self._serve_static(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            self._raw_body = self._read_raw()
        except ValueError as e:
            self._json(413, {"error": str(e)})
            return
        if path == "/api/config":
            self._handle_save_config()
            return
        if path == "/api/check":
            if monitor.is_running():
                self._json(409, {"error": "已有一轮检测正在进行，请稍后再试"})
                return
            config = load_config_safe()
            if config.get("_error"):
                self._json(500, {"error": config["_error"]})
                return
            threading.Thread(target=monitor.run_once, args=(config, "manual"), daemon=True).start()
            self._json(200, {"started": True})
            return
        if path == "/api/repo/check":
            self._handle_repo_check()
            return
        if path == "/api/repo/toggle":
            self._handle_repo_toggle()
            return
        if path == "/api/test-push":
            self._handle_test_push()
            return
        if path == "/api/test-ai":
            self._handle_test_ai()
            return
        if path.startswith("/api/"):
            self._json(404, {"error": "接口不存在"})
            return
        self._json(405, {"error": "不支持的请求方法"})

    # ---------- 业务处理 ----------

    def _handle_save_config(self):
        try:
            raw = self._read_json()
        except Exception as e:
            self._json(400, {"error": f"请求体解析失败: {e}"})
            return
        try:
            config = normalize_config(raw)
            monitor.save_config(config)
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return
        except Exception as e:
            self._json(500, {"error": f"保存配置失败: {e}"})
            return
        monitor.log("[配置] 已通过 Web 界面更新配置")
        self._json(200, {"saved": True, "config": config})

    def _handle_repo_toggle(self):
        try:
            payload = self._read_json()
        except Exception as e:
            self._json(400, {"error": f"请求体解析失败: {e}"})
            return
        try:
            config = monitor.load_config()
        except Exception as e:
            self._json(500, {"error": str(e)})
            return

        name = str(payload.get("name", "")).strip()
        upstream = str(payload.get("upstream", "")).strip()
        enabled = bool(payload.get("enabled", True))
        target = None
        for repo in config.get("repos", []):
            if not isinstance(repo, dict):
                continue
            if repo.get("name", "") == name and (not upstream or repo.get("upstream", "") == upstream):
                target = repo
                break
        if target is None:
            self._json(404, {"error": "仓库配置已变化，请刷新页面后重试"})
            return

        target["enabled"] = enabled
        try:
            monitor.save_config(config)
        except Exception as e:
            self._json(500, {"error": f"保存配置失败: {e}"})
            return
        monitor.log(f"[{name}] 已{'启用' if enabled else '停用'}")
        self._json(200, {"saved": True, "name": name, "enabled": enabled})

    def _handle_repo_check(self):
        try:
            payload = self._read_json()
        except Exception as e:
            self._json(400, {"error": f"请求体解析失败: {e}"})
            return
        config = load_config_safe()
        if config.get("_error"):
            self._json(500, {"error": config["_error"]})
            return

        name = str(payload.get("name", "")).strip()
        upstream = str(payload.get("upstream", "")).strip()
        target = None
        for repo in config.get("repos", []):
            if not isinstance(repo, dict):
                continue
            if repo.get("name", "") == name and (not upstream or repo.get("upstream", "") == upstream):
                target = repo
                break
        if target is None:
            self._json(404, {"error": "仓库配置已变化，请刷新页面后重试"})
            return
        if target.get("enabled") is False:
            self._json(400, {"error": "该仓库已停用，请先启用再检测"})
            return
        if monitor.is_running():
            self._json(409, {"error": "已有一轮检测正在进行，请稍后再试"})
            return

        threading.Thread(target=monitor.run_repo_once, args=(target, config), daemon=True).start()
        self._json(200, {"started": True, "name": target.get("name", "")})

    def _handle_test_ai(self):
        try:
            payload = self._read_json()
        except Exception as e:
            self._json(400, {"error": f"请求体解析失败: {e}"})
            return
        ai = payload.get("ai") if isinstance(payload, dict) else None
        if not isinstance(ai, dict):
            config = load_config_safe()
            ai = config.get("ai") if isinstance(config.get("ai"), dict) else {}
        ok, message = monitor.test_ai_connection(ai)
        if ok:
            self._json(200, {"ok": True, "reply": message})
        else:
            self._json(502, {"error": message})

    def _handle_test_push(self):
        config = load_config_safe()
        sendkey = ""
        if isinstance(config.get("serverchan"), dict):
            sendkey = config["serverchan"].get("sendkey", "")
        if not sendkey:
            self._json(400, {"error": "尚未配置 Server酱 SendKey"})
            return
        ok = monitor.send_serverchan(
            "✅ fn-repo-monitor 测试推送",
            "如果你收到这条消息，说明 Server酱 SendKey 配置正确。\n\n"
            f"发送时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            sendkey,
        )
        if ok:
            self._json(200, {"sent": True})
        else:
            self._json(502, {"error": "推送失败，请查看日志确认原因（SendKey 是否正确、是否超出当日额度）"})

    def _serve_static(self, path):
        rel = path.lstrip("/") or "index.html"
        target = (WEB_DIR / rel).resolve()
        try:
            if not str(target).startswith(str(WEB_DIR.resolve())):
                self._send(400, "Bad Request", "text/plain; charset=utf-8")
                return
            if target.is_dir():
                target = target / "index.html"
            if not target.is_file():
                self._send(404, "Not Found", "text/plain; charset=utf-8")
                return
        except Exception:
            self._send(500, "Internal Error", "text/plain; charset=utf-8")
            return

        ext = target.suffix.lower()
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        self._send(200, target.read_bytes(), mime)


def main():
    global WEB_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3869)
    parser.add_argument("--web-dir", default=str(WEB_DIR))
    parser.add_argument("--no-scheduler", action="store_true")
    args = parser.parse_args()

    WEB_DIR = Path(args.web_dir)

    if not args.no_scheduler:
        threading.Thread(target=monitor.run_forever, args=(STOP_EVENT,), daemon=True).start()

    httpd = QuietServer((args.host, args.port), Handler)
    monitor.log(f"Web 服务已启动: http://{args.host}:{args.port} (数据目录 {monitor.DATA_DIR})")

    def _shutdown(signum, frame):
        STOP_EVENT.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    finally:
        STOP_EVENT.set()
        httpd.server_close()
        monitor.log("Web 服务已停止")


if __name__ == "__main__":
    main()
