// fn-repo-monitor 前端逻辑
// 接口基址相对当前文档目录解析：
// - 飞牛窗口模式：/cgi/ThirdParty/fn-repo-monitor/index.cgi/ -> .../index.cgi/api/status
// - 本地调试：http://127.0.0.1:3869/ -> /api/status
const API_ROOT = new URL(".", document.baseURI);
function apiUrl(path) {
  return new URL(String(path).replace(/^\//, ""), API_ROOT).href;
}

async function requestJson(path, options = {}) {
  const response = await fetch(apiUrl(path), {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    throw new Error(response.ok ? "服务返回了无法解析的数据" : `服务不可用（HTTP ${response.status}）`);
  }
  if (!response.ok) {
    throw new Error(payload.error || `请求失败（HTTP ${response.status}）`);
  }
  return payload;
}

const state = {
  status: null,
  rawConfig: null,
  polling: null,
};

const $ = (id) => document.getElementById(id);

function showBanner(type, text) {
  const banner = $("banner");
  banner.className = `banner ${type}`;
  banner.textContent = text;
}

function hideBanner() {
  $("banner").className = "banner hidden";
  $("banner").textContent = "";
}

function statusBadge(repo) {
  if (repo.enabled === false) return { cls: "idle", text: "已停用" };
  if (repo.checking) return { cls: "run", text: "检测中" };
  switch (repo.result) {
    case "no_update":
      return { cls: "ok", text: "无更新" };
    case "first_run":
      return { cls: "idle", text: "已建基线" };
    case "updated": {
      const label = repo.watch === "release" ? "新版本" : "有更新";
      return { cls: "warn", text: repo.suggestion ? `${label} · ${repo.suggestion}` : label };
    }
    case "error":
      return { cls: "err", text: "检测失败" };
    default:
      return { cls: "idle", text: "待检测" };
  }
}

function formatBaseline(value) {
  if (!value) return "未建立";
  if (value.startsWith("release:")) return value.slice("release:".length);
  return value.slice(0, 10);
}

function renderRepoList(repos) {
  const container = $("repo-list");
  container.innerHTML = "";
  if (!repos.length) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "还没有配置监控仓库，请在下方配置中添加。";
    container.appendChild(p);
    return;
  }

  repos.forEach((repo) => {
    const item = document.createElement("div");
    item.className = "repo-item";

    const main = document.createElement("div");
    main.className = "repo-main";

    const nameRow = document.createElement("div");
    nameRow.className = "repo-name";
    const link = document.createElement("a");
    link.href = `https://github.com/${repo.upstream}`;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = repo.name;
    nameRow.appendChild(link);

    const badgeInfo = statusBadge(repo);
    const badge = document.createElement("span");
    badge.className = `badge ${badgeInfo.cls}`;
    badge.textContent = badgeInfo.text;
    nameRow.appendChild(badge);

    const sub = document.createElement("div");
    sub.className = "repo-sub";
    const mode = repo.watch === "release" ? "仅正式版" : "任意提交";
    const source = repo.watch === "release" ? repo.upstream : `${repo.upstream} @ ${repo.branch}`;
    sub.textContent = `${source} · ${mode} · 基线 ${formatBaseline(repo.baseline)}`;
    sub.title = "基线是上次检测到的上游状态；首次检测只记录基线，之后与它比较才判断是否有更新";

    main.appendChild(nameRow);
    main.appendChild(sub);
    item.appendChild(main);

    const message = document.createElement("div");
    message.className = "repo-sub";
    message.textContent = repo.message || "";
    item.appendChild(message);

    const meta = document.createElement("div");
    meta.className = "repo-meta";
    meta.textContent = repo.last_check_at || "尚未检测";
    item.appendChild(meta);

    const actions = document.createElement("div");
    actions.className = "repo-actions";

    const toggle = document.createElement("label");
    toggle.className = "repo-toggle";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = repo.enabled !== false;
    checkbox.addEventListener("change", () => toggleRepo(repo, checkbox));
    toggle.appendChild(checkbox);
    const toggleText = document.createElement("span");
    toggleText.textContent = checkbox.checked ? "启用" : "停用";
    toggle.appendChild(toggleText);
    actions.appendChild(toggle);

    const retry = document.createElement("button");
    retry.className = "btn small";
    retry.type = "button";
    retry.textContent = repo.checking ? "检测中…" : "重试";
    retry.disabled = Boolean(repo.checking) || repo.enabled === false;
    retry.addEventListener("click", () => retryRepo(repo, retry));
    actions.appendChild(retry);
    item.appendChild(actions);

    container.appendChild(item);
  });
}

function renderStatus(data) {
  state.status = data;
  const schedule = data.schedule || {};
  const repos = data.repos || [];

  $("stat-service").textContent = schedule.running ? "检测中" : "运行中";
  $("stat-version").textContent = `v${data.server.version} · PID ${data.server.pid}`;

  $("stat-last").textContent = schedule.last_run_at || "尚未检测";
  const triggerMap = { scheduled: "定时触发", manual: "手动触发" };
  $("stat-trigger").textContent = schedule.last_run_trigger
    ? triggerMap[schedule.last_run_trigger] || schedule.last_run_trigger
    : "";

  $("stat-next").textContent = schedule.next_run_at || "尚未安排";
  $("stat-interval").textContent = schedule.daily_time
    ? `每天 ${schedule.daily_time}`
    : `每 ${schedule.interval_hours} 小时`;

  $("stat-repos").textContent = `${repos.length} 个`;
  const updateCount = repos.filter((r) => r.has_update).length;
  $("stat-updates").textContent = updateCount ? `${updateCount} 个有更新` : "全部无更新";

  $("subtitle").textContent = `数据目录：${data.server.data_dir}`;
  $("repo-hint").textContent = schedule.running ? "正在检测，请稍候…" : "";

  renderRepoList(repos);

  if (Array.isArray(data.warnings) && data.warnings.length) {
    showBanner("warn", data.warnings.join("；"));
  } else if ($("banner").classList.contains("warn")) {
    hideBanner();
  }

  const busy = Boolean(schedule.running);
  $("btn-check").disabled = busy;
  $("btn-check").textContent = busy ? "检测中…" : "立即检测";
}

async function loadStatus() {
  try {
    renderStatus(await requestJson("api/status"));
  } catch (error) {
    showBanner("error", error.message);
    $("stat-service").textContent = "服务不可用";
  }
}

function createField(labelText, value, placeholder, dataField, type = "text") {
  const label = document.createElement("label");
  label.className = "field";
  const span = document.createElement("span");
  span.textContent = labelText;
  const input = document.createElement("input");
  input.type = type;
  input.value = value || "";
  if (placeholder) input.placeholder = placeholder;
  input.dataset.field = dataField;
  label.appendChild(span);
  label.appendChild(input);
  return label;
}

function createSelect(labelText, value, dataField, options) {
  const label = document.createElement("label");
  label.className = "field";
  const span = document.createElement("span");
  span.textContent = labelText;
  const select = document.createElement("select");
  select.dataset.field = dataField;
  options.forEach(([optionValue, optionText]) => {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = optionText;
    if ((value || "commit") === optionValue) option.selected = true;
    select.appendChild(option);
  });
  label.appendChild(span);
  label.appendChild(select);
  return label;
}

function addRepoRow(repo = {}) {
  const row = document.createElement("div");
  row.className = "repo-row";
  row.dataset.enabled = repo.enabled === false ? "false" : "true";

  const head = document.createElement("div");
  head.className = "row-head";
  const title = document.createElement("span");
  title.className = "row-title";
  title.textContent = repo.name ? `仓库：${repo.name}` : "新仓库";
  const del = document.createElement("button");
  del.type = "button";
  del.className = "btn small danger";
  del.textContent = "删除";
  del.addEventListener("click", () => {
    row.remove();
    refreshRowTitles();
  });
  head.appendChild(title);
  head.appendChild(del);

  const grid = document.createElement("div");
  grid.className = "row-grid";
  grid.appendChild(createField("名称", repo.name, "示例项目", "name"));

  const upstream = createField("上游仓库", repo.upstream, "owner/repo", "upstream");
  grid.appendChild(upstream);

  grid.appendChild(createField("分支", repo.branch || "main", "main", "branch"));
  grid.appendChild(createSelect("监控方式", repo.watch, "watch", [
    ["commit", "任意新提交"],
    ["release", "仅正式版 Release"],
  ]));
  grid.appendChild(createField("关键词（逗号分隔，可留空）", (repo.keywords || []).join(", "), "src, config, docs", "keywords"));
  grid.appendChild(createField("编译跳转链接（可留空）", repo.build_url, "https://github.com/.../actions", "build_url"));

  row.appendChild(head);
  row.appendChild(grid);

  // 名称变化时同步行标题，便于识别
  grid.querySelector('[data-field="name"]').addEventListener("input", (event) => {
    title.textContent = event.target.value ? `仓库：${event.target.value}` : "新仓库";
  });

  $("repo-editor").appendChild(row);
}

function refreshRowTitles() {
  const rows = document.querySelectorAll("#repo-editor .repo-row");
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.id = "repo-empty";
    empty.textContent = "还没有仓库，点击“添加仓库”开始配置。";
    $("repo-editor").appendChild(empty);
    return;
  }
  const empty = $("repo-empty");
  if (empty) empty.remove();
}

function renderConfig(config) {
  state.rawConfig = config;
  $("cfg-interval").value = config.interval_hours || 24;
  $("cfg-daily-time").value = config.daily_time || "";
  $("cfg-sendkey").value = (config.serverchan || {}).sendkey || "";
  $("cfg-token").value = config.github_token || "";
  $("ai-test-hint").textContent = "";
  $("ai-test-hint").className = "hint";

  const ai = config.ai || {};
  $("cfg-ai-enabled").checked = Boolean(ai.enabled);
  $("cfg-ai-base").value = ai.api_base || "";
  $("cfg-ai-key").value = ai.api_key || "";
  $("cfg-ai-model").value = ai.model || "";

  $("repo-editor").innerHTML = "";
  (config.repos || []).forEach((repo) => addRepoRow(repo));
  refreshRowTitles();
}

function collectConfig() {
  const repos = Array.from(document.querySelectorAll("#repo-editor .repo-row")).map((row) => {
    const get = (field) => (row.querySelector(`[data-field="${field}"]`) || {}).value || "";
    const name = get("name").trim();
    const upstream = get("upstream").trim();
    const known = (state.status && state.status.repos ? state.status.repos : []).find(
      (item) => item.name === name && item.upstream === upstream
    );
    return {
      name,
      upstream,
      branch: get("branch").trim() || "main",
      watch: get("watch") || "commit",
      enabled: known ? known.enabled !== false : row.dataset.enabled !== "false",
      keywords: get("keywords")
        .split(/[,，]/)
        .map((k) => k.trim())
        .filter(Boolean),
      build_url: get("build_url").trim(),
    };
  });

  return {
    ...(state.rawConfig || {}),
    interval_hours: Number($("cfg-interval").value),
    daily_time: $("cfg-daily-time").value || "",
    github_token: $("cfg-token").value.trim(),
    serverchan: { sendkey: $("cfg-sendkey").value.trim() },
    ai: {
      enabled: $("cfg-ai-enabled").checked,
      api_base: $("cfg-ai-base").value.trim(),
      api_key: $("cfg-ai-key").value.trim(),
      model: $("cfg-ai-model").value.trim(),
    },
    repos,
  };
}

async function loadConfig() {
  try {
    renderConfig(await requestJson("api/config"));
  } catch (error) {
    showBanner("error", `读取配置失败：${error.message}`);
  }
}

async function saveConfig(event) {
  event.preventDefault();
  const button = $("btn-save");
  const hint = $("save-hint");
  button.disabled = true;
  hint.textContent = "保存中…";
  try {
    await requestJson("api/config", {
      method: "POST",
      body: JSON.stringify(collectConfig()),
    });
    hint.textContent = `已保存 ${new Date().toLocaleTimeString()}`;
    showBanner("ok", "配置已保存，定时任务会按新配置执行。");
    await loadConfig();
    await loadStatus();
  } catch (error) {
    hint.textContent = "";
    showBanner("error", `保存失败：${error.message}`);
  } finally {
    button.disabled = false;
  }
}

function stopPolling() {
  if (state.polling) {
    clearInterval(state.polling);
    state.polling = null;
  }
}

async function triggerCheck() {
  const button = $("btn-check");
  button.disabled = true;
  try {
    await requestJson("api/check", { method: "POST", body: "{}" });
    showBanner("ok", "已开始检测，完成后会自动刷新结果。");
    stopPolling();
    state.polling = setInterval(async () => {
      await loadStatus();
      if (state.status && state.status.schedule && !state.status.schedule.running) {
        stopPolling();
        await loadStatus();
      }
    }, 2000);
  } catch (error) {
    showBanner("error", error.message);
    button.disabled = false;
  }
}

async function toggleRepo(repo, checkbox) {
  const enabled = checkbox.checked;
  checkbox.disabled = true;
  try {
    await requestJson("api/repo/toggle", {
      method: "POST",
      body: JSON.stringify({ name: repo.name, upstream: repo.upstream, enabled }),
    });
    showBanner("ok", `已${enabled ? "启用" : "停用"}「${repo.name}」`);
    await loadStatus();
  } catch (error) {
    checkbox.checked = !enabled;
    checkbox.disabled = false;
    showBanner("error", `切换失败：${error.message}`);
  }
}

async function retryRepo(repo, button) {
  const before = repo.last_check_at || "";
  if (button) {
    button.disabled = true;
    button.textContent = "检测中…";
  }
  try {
    await requestJson("api/repo/check", {
      method: "POST",
      body: JSON.stringify({ name: repo.name, upstream: repo.upstream }),
    });
    showBanner("ok", `已开始重新检测「${repo.name}」，完成后自动刷新。`);
    stopPolling();
    const startedAt = Date.now();
    state.polling = setInterval(async () => {
      await loadStatus();
      const target = (state.status && state.status.repos ? state.status.repos : []).find(
        (entry) => entry.name === repo.name
      );
      if (!target) {
        stopPolling();
        return;
      }
      const finished = !target.checking && (target.last_check_at !== before || Date.now() - startedAt > 4000);
      if (finished) {
        stopPolling();
        await loadStatus();
      } else if (Date.now() - startedAt > 60000) {
        stopPolling();
        showBanner("warn", "重试等待超时，请稍后刷新查看结果。");
      }
    }, 1500);
  } catch (error) {
    if (button) {
      button.disabled = false;
      button.textContent = "重试";
    }
    showBanner("error", `重试失败：${error.message}`);
  }
}

async function testPush() {
  const button = $("btn-test");
  button.disabled = true;
  try {
    await requestJson("api/test-push", { method: "POST", body: "{}" });
    showBanner("ok", "测试消息已发送，请在微信中确认是否收到。");
  } catch (error) {
    showBanner("error", `测试推送失败：${error.message}`);
  } finally {
    button.disabled = false;
  }
}

async function testAI() {
  const button = $("btn-test-ai");
  const hint = $("ai-test-hint");
  button.disabled = true;
  hint.className = "hint";
  hint.textContent = "测试中…";
  try {
    const data = await requestJson("api/test-ai", {
      method: "POST",
      body: JSON.stringify({
        ai: {
          enabled: $("cfg-ai-enabled").checked,
          api_base: $("cfg-ai-base").value.trim(),
          api_key: $("cfg-ai-key").value.trim(),
          model: $("cfg-ai-model").value.trim(),
        },
      }),
    });
    const reply = (data.reply || "").replace(/\s+/g, " ").slice(0, 60);
    hint.className = "hint ok";
    hint.textContent = `连接正常：${reply}`;
  } catch (error) {
    hint.className = "hint err";
    hint.textContent = `测试失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function loadLog() {
  try {
    const data = await requestJson("api/log?lines=300");
    const view = $("log-view");
    view.textContent = data.log || "（暂无日志）";
    view.scrollTop = view.scrollHeight;
  } catch (error) {
    $("log-view").textContent = `读取日志失败：${error.message}`;
  }
}

function bindEvents() {
  $("btn-refresh").addEventListener("click", () => {
    loadStatus();
    loadLog();
  });
  $("btn-check").addEventListener("click", triggerCheck);
  $("btn-test").addEventListener("click", testPush);
  $("btn-test-ai").addEventListener("click", testAI);
  document.querySelectorAll(".toggle-eye").forEach((button) => {
    button.addEventListener("click", () => {
      const input = $(button.dataset.target);
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      button.classList.toggle("active", show);
      const label = show ? "隐藏" : "显示";
      button.title = `${label} ${input.dataset.name || "密钥"}`;
      button.setAttribute("aria-label", label);
    });
  });
  $("btn-log").addEventListener("click", loadLog);
  $("btn-add-repo").addEventListener("click", () => {
    addRepoRow();
    refreshRowTitles();
  });
  $("config-form").addEventListener("submit", saveConfig);
}

async function init() {
  bindEvents();
  await loadStatus();
  await loadConfig();
  await loadLog();
  setInterval(loadStatus, 15000);
}

init();
