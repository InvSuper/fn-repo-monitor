#!/bin/bash

# index.cgi — 飞牛桌面窗口的同源入口
# 访问路径：/cgi/ThirdParty/fn-repo-monitor/index.cgi/
#   1. 静态文件（HTML/CSS/JS/图片）直接由本脚本返回
#   2. /api/ 请求转发到本机后端（只监听 127.0.0.1）
# 因为入口是相对路径，所以内网 HTTP 与公网 HTTPS 访问都能正常工作。

# ========== 配置 ==========

# 静态文件根目录：CGI 脚本自身所在目录（target/ui）
BASE_PATH="${SCRIPT_FILENAME:+$(dirname "$SCRIPT_FILENAME")}"
[ -z "$BASE_PATH" ] && BASE_PATH="/var/apps/fn-repo-monitor/target/ui"

# 后端端口：从 manifest 的 service_port 读取，默认 3869
APP_ROOT="$(cd "$BASE_PATH/../.." 2>/dev/null && pwd)"
MANIFEST_FILE="${APP_ROOT}/manifest"
BACKEND_PORT="3869"
if [ -f "$MANIFEST_FILE" ]; then
    _port=$(grep -E '^service_port[[:space:]]*=' "$MANIFEST_FILE" | awk -F '=' '{print $2}' | tr -d ' \r')
    [ -n "$_port" ] && BACKEND_PORT="$_port"
fi
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"

# ========== 解析请求路径 ==========

URI_NO_QUERY="${REQUEST_URI%%\?*}"
REL_PATH="/"
case "$URI_NO_QUERY" in
    *index.cgi*)
        REL_PATH="${URI_NO_QUERY#*index.cgi}"
        ;;
esac
[ -z "$REL_PATH" ] && REL_PATH="/"
[ "$REL_PATH" = "/" ] && REL_PATH="/index.html"

# ========== /api 反向代理 ==========

case "$REL_PATH" in
    /api/*)
        TARGET_URL="${BACKEND_URL}${REL_PATH}"
        [ -n "$QUERY_STRING" ] && TARGET_URL="${TARGET_URL}?${QUERY_STRING}"

        case "$REQUEST_METHOD" in
            POST|PUT)
                POST_DATA=""
                if [ -n "$CONTENT_LENGTH" ] && [ "$CONTENT_LENGTH" -gt 0 ] 2>/dev/null; then
                    POST_DATA=$(dd bs=1 count="$CONTENT_LENGTH" 2>/dev/null)
                fi
                RESPONSE=$(curl -s -w $'\n%{http_code}' -X "$REQUEST_METHOD" \
                    -H "Content-Type: ${CONTENT_TYPE:-application/json}" \
                    --data-binary "$POST_DATA" \
                    "$TARGET_URL" 2>/dev/null)
                ;;
            *)
                RESPONSE=$(curl -s -w $'\n%{http_code}' -X GET "$TARGET_URL" 2>/dev/null)
                ;;
        esac

        HTTP_CODE=$(printf '%s' "$RESPONSE" | tail -n 1)
        BODY=$(printf '%s' "$RESPONSE" | sed '$d')
        # curl 连不上后端时返回 000，转换成合法状态码，前端才能给出明确提示
        case "$HTTP_CODE" in
            [1-5][0-9][0-9])
                ;;
            *)
                HTTP_CODE=502
                BODY='{"error": "fn-repo-monitor 服务未运行，请在应用中心启动应用"}'
                ;;
        esac

        echo "Status: $HTTP_CODE"
        echo "Content-Type: application/json; charset=utf-8"
        echo "Cache-Control: no-store"
        echo ""
        printf '%s' "$BODY"
        exit 0
        ;;
esac

# ========== 静态文件 ==========

TARGET_FILE="${BASE_PATH}${REL_PATH}"

case "$TARGET_FILE" in
    *..*)
        echo "Status: 400 Bad Request"
        echo "Content-Type: text/plain; charset=utf-8"
        echo ""
        echo "Bad Request"
        exit 0
        ;;
esac

if [ ! -f "$TARGET_FILE" ]; then
    echo "Status: 404 Not Found"
    echo "Content-Type: text/plain; charset=utf-8"
    echo ""
    echo "404 Not Found: ${REL_PATH}"
    exit 0
fi

ext="${TARGET_FILE##*.}"
case "$ext" in
    html|htm) mime="text/html; charset=utf-8" ;;
    css)      mime="text/css; charset=utf-8" ;;
    js)       mime="application/javascript; charset=utf-8" ;;
    json)     mime="application/json; charset=utf-8" ;;
    png)      mime="image/png" ;;
    jpg|jpeg) mime="image/jpeg" ;;
    gif)      mime="image/gif" ;;
    svg)      mime="image/svg+xml" ;;
    ico)      mime="image/x-icon" ;;
    woff2)    mime="font/woff2" ;;
    woff)     mime="font/woff" ;;
    txt|log)  mime="text/plain; charset=utf-8" ;;
    *)        mime="application/octet-stream" ;;
esac

echo "Content-Type: $mime"
# 页面与脚本每次向服务端确认，升级后不会继续使用浏览器缓存
case "$ext" in
    html|htm|css|js) echo "Cache-Control: no-cache" ;;
esac
echo ""
cat "$TARGET_FILE"
