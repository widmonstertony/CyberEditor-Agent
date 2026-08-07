"use strict";

const $ = (selector) => document.querySelector(selector);
const state = {
  config: {}, environment: null, videos: [], lastLogId: 0, polling: null,
  workerPolling: null, mode: "local", workers: [], workerId: "", jobId: ""
};
const pageParameters = new URLSearchParams(window.location.search);
const hostedPortfolio = ["tonytan.me", "www.tonytan.me"].includes(window.location.hostname);
const localCompanionMode = hostedPortfolio && pageParameters.get("remote") !== "1";
const localCompanionBase = "http://127.0.0.1:8765/";

const copy = {
  zh: {
    tagline: "本地 · 隐私 · 严格串行 AI 剪辑", localFirst: "本地优先", theme: "主题", language: "语言",
    accessToken: "访问令牌", projectSetup: "项目设置", projectMedia: "项目与素材", applyAuto: "应用自动配置",
    workflowMode: "工作流模式", cameraProfile: "相机色彩", creativeBrief: "成片主题 / AI 导演要求（可选）",
    briefPlaceholder: "例如：剪成一支有明确起承转合的夜骑短片，强调人物准备、机械细节和最终集结。留空则 AI 自由发现主题。",
    briefHelp: "这是导演最高优先级的创作意图；留空不等于随机拼接。", sourceMedia: "源视频素材", noMedia: "尚未选择素材",
    pickerLocal: "本机模式会打开 Windows 原生选择器；素材不会被复制或上传。", chooseVideos: "选择多个视频", chooseFolder: "选择文件夹",
    serverPaths: "本机 Worker 路径 / 高级输入", videoPaths: "视频绝对路径（每行一个）", folderPath: "素材文件夹路径", dataDir: "运行数据目录",
    fps: "项目 FPS", aiHardware: "AI 与硬件", visionModel: "视觉 / Ollama 模型", directorModel: "文本导演模型", context: "上下文",
    chunk: "分块分钟", detectingHardware: "正在检测硬件并计算质量优先配置…", musicOutput: "配乐与输出", musicSource: "音乐来源",
    musicFolder: "本地音乐目录", rightsWarning: "我确认拥有下载、改编、同步和发布所选音乐的权利，并自行遵守来源平台条款。",
    sendResolve: "发送至 DaVinci Resolve", renderPreview: "生成审片预览", renderFinal: "Resolve 最终渲染", strictFps: "严格匹配 FPS",
    missionControl: "任务控制", taskCenter: "任务中心", ready: "准备就绪", readyHelp: "选择素材后启动；任意时刻只运行一个重型模块。",
    liveLog: "实时日志", clear: "清空", start: "开始串行工作流", stop: "停止", outputPreview: "成片与预览", refresh: "刷新",
    noOutput: "完成工作流后可在这里直接播放成片。", tokenHelp: "部署版需要管理令牌；它只保存在当前浏览器会话，不会发送给本机 Worker。",
    cancel: "取消", save: "保存", selectedVideos: "已选择 {count} 个视频", selectedFolder: "已选择素材文件夹", running: "工作流运行中",
    succeeded: "工作流已完成", failed: "工作流失败", stopped: "工作流已停止", play: "播放", refreshEnvironment: "环境检测完成",
    remoteControl: "远程控制 · 本机执行", waitingWorker: "正在等待本机 Worker 连接…", executionComputer: "执行电脑",
    refreshWorkers: "刷新电脑", workerOnline: "{name} 在线 · 素材与 AI 留在此电脑", workerOffline: "{name} 离线",
    noWorkers: "尚无本机 Worker；请先在 Windows 电脑运行 worker.py", chooseWorker: "请选择一台在线执行电脑",
    pickerRemote: "选择框会在所选 Windows 电脑上打开；RAW 素材不会上传云端。",
    localApp: "真实本机应用", localConnecting: "正在连接这台电脑上的 CyberEditor companion…", localConnected: "已连接本机源码与 AI 工作流",
    localDisconnected: "尚未连接。请在本机源码目录运行 launch_companion.command（macOS）或 launch_companion.bat（Windows）。", retryLocal: "重新连接", getSource: "获取源码与启动器 ↗"
  },
  en: {
    tagline: "Local · Private · Strict-serial AI editing", localFirst: "LOCAL FIRST", theme: "Theme", language: "Language",
    accessToken: "Access token", projectSetup: "PROJECT SETUP", projectMedia: "Project & media", applyAuto: "Apply auto settings",
    workflowMode: "Workflow mode", cameraProfile: "Camera color", creativeBrief: "Film theme / AI director brief (optional)",
    briefPlaceholder: "Example: Create a coherent night-riding short with preparation, mechanical details, escalation, and a final gathering. Leave blank for free direction.",
    briefHelp: "This is the director's highest-priority intent; blank never means random assembly.", sourceMedia: "Source media", noMedia: "No media selected",
    pickerLocal: "Local mode opens the native Windows picker; media is neither copied nor uploaded.", chooseVideos: "Choose videos", chooseFolder: "Choose folder",
    serverPaths: "Local worker paths / advanced input", videoPaths: "Absolute video paths (one per line)", folderPath: "Media folder path", dataDir: "Run data directory",
    fps: "Project FPS", aiHardware: "AI & hardware", visionModel: "Vision / Ollama model", directorModel: "Text director model", context: "Context",
    chunk: "Chunk minutes", detectingHardware: "Detecting hardware and computing quality-first settings…", musicOutput: "Music & output", musicSource: "Music source",
    musicFolder: "Local music folder", rightsWarning: "I confirm I have download, adaptation, synchronization, and publishing rights and will follow the source platform's terms.",
    sendResolve: "Send to DaVinci Resolve", renderPreview: "Create review preview", renderFinal: "Final Resolve render", strictFps: "Strict FPS match",
    missionControl: "MISSION CONTROL", taskCenter: "Task center", ready: "Ready", readyHelp: "Select media and start; only one heavy module runs at a time.",
    liveLog: "Live log", clear: "Clear", start: "Start serial workflow", stop: "Stop", outputPreview: "Films & previews", refresh: "Refresh",
    noOutput: "Completed previews and films will be playable here.", tokenHelp: "Deployed mode requires the admin token. It stays in this browser session and is never sent to the local worker.",
    cancel: "Cancel", save: "Save", selectedVideos: "{count} videos selected", selectedFolder: "Media folder selected", running: "Workflow running",
    succeeded: "Workflow complete", failed: "Workflow failed", stopped: "Workflow stopped", play: "Play", refreshEnvironment: "Environment check complete",
    remoteControl: "REMOTE CONTROL · LOCAL EXECUTION", waitingWorker: "Waiting for a local worker…", executionComputer: "Execution PC",
    refreshWorkers: "Refresh PCs", workerOnline: "{name} online · media and AI stay on this PC", workerOffline: "{name} offline",
    noWorkers: "No local worker yet; run worker.py on the Windows PC first", chooseWorker: "Choose an online execution PC",
    pickerRemote: "The picker opens on the selected Windows PC; RAW media is never uploaded.",
    localApp: "REAL LOCAL APP", localConnecting: "Connecting to the CyberEditor companion on this computer…", localConnected: "Connected to the local source and AI workflow",
    localDisconnected: "Not connected. Run launch_companion.command on macOS or launch_companion.bat on Windows from the source folder.", retryLocal: "Reconnect", getSource: "Get source and launcher ↗"
  }
};

function language() { return localStorage.getItem("cybereditor-language") || (navigator.language.startsWith("zh") ? "zh" : "en"); }
function t(key, values = {}) { let value = (copy[language()] || copy.en)[key] || key; Object.entries(values).forEach(([name, item]) => { value = value.replace(`{${name}}`, item); }); return value; }
function translate() {
  document.documentElement.lang = language() === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => { node.placeholder = t(node.dataset.i18nPlaceholder); });
  $("#languageSelect").value = language();
  updateMediaSummary();
  renderWorkerStatus();
}

function token() { return sessionStorage.getItem("cybereditor-token") || ""; }
function apiUrl(path) {
  const base = localCompanionMode ? localCompanionBase : document.baseURI;
  return new URL(path.replace(/^\/+/, ""), base);
}
async function api(path, options = {}) {
  const headers = { "Accept": "application/json", ...(options.headers || {}) };
  if (token()) headers["X-CyberEditor-Token"] = token();
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const url = apiUrl(path);
  let response;
  try {
    response = await fetch(url, { ...options, headers, cache: "no-store" });
  } catch (error) {
    if (localCompanionMode) throw new Error(t("localDisconnected"));
    throw error;
  }
  let payload;
  try { payload = await response.json(); } catch { payload = { ok: false, error: `HTTP ${response.status}` }; }
  if (!response.ok || payload.ok === false) {
    if (response.status === 401) $("#tokenDialog").showModal();
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

let toastTimer;
function toast(message, error = false) {
  const node = $("#toast"); node.textContent = message; node.className = error ? "show error" : "show";
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { node.className = ""; }, 3500);
}
function setValue(selector, value) { const node = $(selector); if (node && value !== undefined && value !== null) node.value = String(value); }
function setChecked(selector, value) { const node = $(selector); if (node) node.checked = Boolean(value); }

function hydrate(config) {
  state.config = config;
  state.videos = Array.isArray(config.videos) ? config.videos : [];
  setValue("#flow", config.flow || "full"); setValue("#cameraProfile", config.camera_profile || "auto");
  setValue("#creativeBrief", config.creative_brief || ""); setValue("#videoPaths", state.videos.join("\n"));
  setValue("#inputFolder", config.input_folder || ""); setValue("#dataDir", config.data_dir || "data/ui-run");
  setValue("#fpsMode", config.fps_mode === "auto" ? "auto" : config.project_fps || 25);
  setValue("#whisperModel", config.whisper_model || "small"); setValue("#numCtx", config.num_ctx || 8192); setValue("#chunkMinutes", config.chunk_minutes || 12);
  setValue("#musicProvider", config.music_provider || "off"); setValue("#musicFolder", config.music_folder || "");
  setChecked("#musicRights", config.music_rights_confirmed); setChecked("#sendResolve", !config.skip_resolve);
  setChecked("#renderPreview", config.render_preview !== false); setChecked("#renderFinal", config.render_final); setChecked("#strictFps", config.strict_fps);
  updateMediaSummary();
}

function selectedWorker() { return state.workers.find((item) => item.worker_id === state.workerId) || null; }
function renderWorkerStatus() {
  if (state.mode !== "remote") return;
  const worker = selectedWorker();
  if (!worker) { $("#workerStatus").textContent = t("noWorkers"); return; }
  $("#workerStatus").textContent = worker.online
    ? t("workerOnline", { name: worker.name || worker.worker_id })
    : t("workerOffline", { name: worker.name || worker.worker_id });
}

async function refreshWorkers() {
  if (state.mode !== "remote") return;
  const payload = await api("/api/workers");
  state.workers = Array.isArray(payload.workers) ? payload.workers : [];
  const previous = state.workerId || localStorage.getItem("cybereditor-worker") || "";
  const preferred = state.workers.find((item) => item.worker_id === previous && item.online)
    || state.workers.find((item) => item.online) || state.workers[0] || null;
  state.workerId = preferred ? preferred.worker_id : "";
  const select = $("#workerSelect"); select.textContent = "";
  if (!state.workers.length) {
    const option = document.createElement("option"); option.value = ""; option.textContent = t("noWorkers"); select.append(option);
  } else {
    state.workers.forEach((worker) => {
      const option = document.createElement("option"); option.value = worker.worker_id;
      option.textContent = `${worker.online ? "●" : "○"} ${worker.name || worker.worker_id}`; select.append(option);
    });
    select.value = state.workerId;
  }
  localStorage.setItem("cybereditor-worker", state.workerId);
  renderWorkerStatus();
}

function requireWorker() {
  if (state.mode !== "remote") return "";
  const worker = selectedWorker();
  if (!worker || !worker.online) throw new Error(t("chooseWorker"));
  return worker.worker_id;
}

function updateMediaSummary() {
  const paths = $("#videoPaths").value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  const folder = $("#inputFolder").value.trim();
  state.videos = paths;
  if (paths.length) { $("#mediaSummary").textContent = t("selectedVideos", { count: paths.length }); $("#mediaDetail").textContent = paths.slice(0, 3).map((p) => p.split(/[\\/]/).pop()).join(" · ") + (paths.length > 3 ? " …" : ""); }
  else if (folder) { $("#mediaSummary").textContent = t("selectedFolder"); $("#mediaDetail").textContent = folder; }
  else { $("#mediaSummary").textContent = t("noMedia"); $("#mediaDetail").textContent = t(state.mode === "remote" ? "pickerRemote" : "pickerLocal"); }
}

function populateModels(models, selected, directorSelected, recommendation = {}) {
  const names = models.map((item) => item.name).filter(Boolean);
  [
    [$("#ollamaModel"), selected, recommendation.ollama_model],
    [$("#directorModel"), directorSelected || selected, recommendation.director_model],
  ].forEach(([select, wanted, recommended]) => {
    select.textContent = "";
    const chosen = names.includes(wanted) ? wanted : (names.includes(recommended) ? recommended : (names[0] || wanted));
    const values = names.length ? names : [wanted].filter(Boolean);
    values.forEach((name) => { const option = document.createElement("option"); option.value = name; option.textContent = name; select.append(option); });
    if (chosen) select.value = chosen;
  });
}

function health(name, text, mode) { const card = document.querySelector(`[data-health="${name}"]`); card.className = `health-card ${mode}`; card.querySelector("strong").textContent = text; }
async function refreshEnvironment() {
  try {
    const query = state.mode === "remote" ? `?worker_id=${encodeURIComponent(requireWorker())}` : "";
    const payload = await api(`/api/environment${query}`); state.environment = payload.environment;
    const env = state.environment, hw = env.hardware || {}, models = env.ollama.models || [];
    health("python", env.python.version, "ok"); health("ffmpeg", env.ffmpeg.ok ? "OK" : "Not found", env.ffmpeg.ok ? "ok" : "bad");
    health("cuda", hw.torch_cuda ? `${hw.torch_version} · CUDA` : (hw.torch_available ? `${hw.torch_version} · CPU` : "Not installed"), hw.torch_cuda ? "ok" : "warn");
    health("ollama", env.ollama.ok ? `${models.length} model${models.length === 1 ? "" : "s"}` : "Disconnected", env.ollama.ok ? "ok" : "bad");
    const resolveReady = Boolean(env.resolve && env.resolve.installed);
    const resolveText = resolveReady ? `${env.resolve.version || "Registered"}${env.resolve.user_registered ? " · Studio" : ""}` : "Not found";
    health("resolve", resolveText, resolveReady ? "ok" : "bad");
    populateModels(models, state.config.ollama_model, state.config.director_model, env.recommendation || {});
    $("#hardwareSummary").textContent = `${hw.gpu || "GPU unknown"} · ${hw.vram_gb || 0} GB VRAM · ${hw.ram_gb || 0} GB RAM · ${hw.cpu_threads || 0}T`;
  } catch (error) { toast(error.message, true); }
}

function applyAuto() {
  if (!state.environment) return toast(t("detectingHardware"), true);
  const value = state.environment.recommendation || {};
  setValue("#whisperModel", value.whisper_model); setValue("#numCtx", value.num_ctx); setValue("#chunkMinutes", value.chunk_minutes);
  if (value.ollama_model) setValue("#ollamaModel", value.ollama_model);
  if (value.director_model) setValue("#directorModel", value.director_model);
  toast(t("refreshEnvironment"));
}

function collect() {
  const videos = $("#videoPaths").value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  const fps = $("#fpsMode").value;
  const provider = $("#musicProvider").value;
  return {
    videos, video: videos[0] || "", input_folder: $("#inputFolder").value.trim(), data_dir: $("#dataDir").value.trim() || "data/ui-run",
    flow: $("#flow").value, hardware_profile: "auto", theme: $("#themeSelect").value, ui_language: language(), fps_mode: fps,
    project_fps: fps === "auto" ? Number(state.config.project_fps || 25) : Number(fps), whisper_model: $("#whisperModel").value,
    whisper_device: "auto", ollama_model: $("#ollamaModel").value, director_model: $("#directorModel").value,
    ollama_url: state.config.ollama_url || "http://localhost:11434", chunk_minutes: Number($("#chunkMinutes").value), num_ctx: Number($("#numCtx").value),
    creative_brief: $("#creativeBrief").value.trim(), target_duration_sec: Number(state.config.target_duration_sec || 0), camera_profile: $("#cameraProfile").value,
    music_provider: provider, music_folder: $("#musicFolder").value.trim(), music_candidate_limit: Number(state.config.music_candidate_limit || 8),
    music_rights_confirmed: $("#musicRights").checked, music_rights_claim: $("#musicRights").checked ? "Confirmed by user in CyberEditor Web Studio for this run." : "",
    jamendo_client_id: state.config.jamendo_client_id || "", timeline_name: state.config.timeline_name || "CyberEditor Timeline",
    project_name: state.config.project_name || "CyberEditor Project", skip_resolve: !$("#sendResolve").checked, strict_fps: $("#strictFps").checked,
    render_preview: $("#renderPreview").checked, drx_root: state.config.drx_root || "config/drx", fairlight_preset: state.config.fairlight_preset || "",
    macro_profile: state.config.macro_profile || "", render_final: $("#renderFinal").checked, render_dir: state.config.render_dir || "data/ui-run/final",
    render_name: state.config.render_name || "CyberEditor_final", render_preset: state.config.render_preset || ""
  };
}

async function pick(kind) {
  try {
    const workerId = requireWorker();
    const request = { kind }; if (state.mode === "remote") request.worker_id = workerId;
    const payload = await api("/api/picker", { method: "POST", body: JSON.stringify(request) });
    if (!payload.paths.length) return;
    if (kind === "videos") { $("#videoPaths").value = payload.paths.join("\n"); $("#inputFolder").value = ""; }
    else { $("#inputFolder").value = payload.paths[0]; $("#videoPaths").value = ""; }
    updateMediaSummary();
  } catch (error) { toast(error.message, true); }
}

function appendLogs(logs) {
  const target = $("#liveLog"), atBottom = target.scrollTop + target.clientHeight >= target.scrollHeight - 36;
  logs.forEach((item) => { const line = document.createElement("span"); line.className = `log-${item.level || "info"}`; line.textContent = item.message + "\n"; target.append(line); });
  if (atBottom) target.scrollTop = target.scrollHeight;
}
function formatTime(seconds) { const value = Math.max(0, Math.floor(seconds || 0)); return [Math.floor(value / 3600), Math.floor(value / 60) % 60, value % 60].map((item) => String(item).padStart(2, "0")).join(":"); }
function renderStatus(value) {
  if (value.job_id) state.jobId = value.job_id;
  const running = value.running; $("#startButton").disabled = running; $("#stopButton").disabled = !running;
  $("#progressBar").style.width = `${Math.max(1, value.progress || 0)}%`; $("#progressBar").classList.toggle("running", running);
  $("#elapsed").textContent = formatTime(value.elapsed_sec);
  const titles = { running: t("running"), starting: t("running"), succeeded: t("succeeded"), failed: t("failed"), stopped: t("stopped"), idle: t("ready") };
  $("#stageTitle").textContent = titles[value.state] || value.stage || t("ready");
  $("#stageSubtitle").textContent = running ? `${value.stage} · ${Math.round(value.progress || 0)}%` : t("readyHelp");
  appendLogs(value.logs || []); state.lastLogId = value.last_log_id || state.lastLogId;
  if (!running && ["succeeded", "failed", "stopped"].includes(value.state)) refreshOutputs();
}

async function poll() {
  try {
    const query = new URLSearchParams({ since: String(state.lastLogId) });
    if (state.mode === "remote") {
      if (!state.workerId) return;
      query.set("worker_id", state.workerId); if (state.jobId) query.set("job_id", state.jobId);
    }
    renderStatus(await api(`/api/status?${query}`));
  } catch (error) { if (!$("#tokenDialog").open) toast(error.message, true); }
}
async function startWorkflow() {
  try {
    const request = collect(); if (state.mode === "remote") request.worker_id = requireWorker();
    state.lastLogId = 0; $("#liveLog").textContent = "";
    renderStatus(await api("/api/workflow/start", { method: "POST", body: JSON.stringify(request) }));
  } catch (error) { toast(error.message, true); }
}
async function stopWorkflow() {
  try {
    const request = state.mode === "remote" ? { job_id: state.jobId } : {};
    renderStatus(await api("/api/workflow/stop", { method: "POST", body: JSON.stringify(request) }));
  } catch (error) { toast(error.message, true); }
}
function humanSize(size) { const units = ["B", "KB", "MB", "GB"]; let value = Number(size || 0), unit = 0; while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; } return `${value.toFixed(unit > 1 ? 1 : 0)} ${units[unit]}`; }
async function refreshOutputs() {
  try {
    const payload = await api("/api/outputs"), target = $("#outputList"); target.textContent = "";
    if (!payload.outputs.length) { const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = t("noOutput"); target.append(empty); return; }
    payload.outputs.forEach((output) => {
      const row = document.createElement("article"); row.className = "output-item";
      const copyNode = document.createElement("div"), name = document.createElement("strong"), meta = document.createElement("small"), button = document.createElement("button");
      name.textContent = output.name; meta.textContent = `${humanSize(output.size)} · ${new Date(output.modified * 1000).toLocaleString()}`; copyNode.append(name, meta);
      button.className = "button secondary small"; button.textContent = t("play");
      button.addEventListener("click", async () => {
        try {
          const url = apiUrl(String(output.url || ""));
          const response = await fetch(url, { headers: { "X-CyberEditor-Token": token() }, cache: "no-store" });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const objectUrl = URL.createObjectURL(await response.blob());
          const link = document.createElement("a");
          link.href = objectUrl; link.target = "_blank"; link.rel = "noopener";
          link.click();
          setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
        } catch (error) { toast(error.message, true); }
      });
      row.append(copyNode, button); target.append(row);
    });
  } catch (error) { toast(error.message, true); }
}

function bind() {
  $("#themeSelect").value = localStorage.getItem("cybereditor-theme") || "system";
  document.documentElement.dataset.theme = $("#themeSelect").value;
  $("#themeSelect").addEventListener("change", (event) => { localStorage.setItem("cybereditor-theme", event.target.value); document.documentElement.dataset.theme = event.target.value; });
  $("#languageSelect").addEventListener("change", (event) => { localStorage.setItem("cybereditor-language", event.target.value); translate(); });
  $("#videoPaths").addEventListener("input", updateMediaSummary); $("#inputFolder").addEventListener("input", updateMediaSummary);
  $("#pickVideos").addEventListener("click", () => pick("videos")); $("#pickFolder").addEventListener("click", () => pick("folder"));
  $("#applyAutoButton").addEventListener("click", applyAuto); $("#startButton").addEventListener("click", startWorkflow); $("#stopButton").addEventListener("click", stopWorkflow);
  $("#clearLog").addEventListener("click", () => { $("#liveLog").textContent = ""; }); $("#refreshOutputs").addEventListener("click", refreshOutputs);
  $("#refreshWorkers").addEventListener("click", async () => { try { await refreshWorkers(); await refreshEnvironment(); } catch (error) { toast(error.message, true); } });
  $("#workerSelect").addEventListener("change", async (event) => {
    state.workerId = event.target.value; state.jobId = ""; state.lastLogId = 0; state.environment = null;
    localStorage.setItem("cybereditor-worker", state.workerId); renderWorkerStatus();
    try { await refreshEnvironment(); await poll(); } catch (error) { toast(error.message, true); }
  });
  $("#tokenButton").addEventListener("click", () => { $("#tokenInput").value = token(); $("#tokenDialog").showModal(); });
  $("#saveToken").addEventListener("click", () => { sessionStorage.setItem("cybereditor-token", $("#tokenInput").value.trim()); setTimeout(initialize, 0); });
  $("#localConnection").hidden = !localCompanionMode;
  if (localCompanionMode) {
    $("#localConnectionStatus").textContent = t("localConnecting");
    $("#retryLocal").addEventListener("click", initialize);
  }
}

let initialized = false;
async function initialize() {
  try {
    const capabilities = await api("/api/capabilities"); state.mode = capabilities.mode || "local";
    if (localCompanionMode) {
      $("#localConnection").classList.add("is-connected");
      $("#localConnectionStatus").textContent = t("localConnected");
    }
    $("#remoteConnection").hidden = state.mode !== "remote";
    const payload = await api("/api/config"); hydrate(payload.config); translate();
    if (state.mode === "remote") await refreshWorkers();
    await Promise.all([refreshEnvironment(), poll(), refreshOutputs()]);
    if (!state.polling) state.polling = setInterval(poll, 1000);
    if (state.mode === "remote" && !state.workerPolling) state.workerPolling = setInterval(() => refreshWorkers().catch(() => {}), 5000);
  } catch (error) {
    if (localCompanionMode) {
      $("#localConnection").classList.remove("is-connected");
      $("#localConnectionStatus").textContent = t("localDisconnected");
    }
    if (!$("#tokenDialog").open) toast(error.message, true);
  }
}

document.addEventListener("DOMContentLoaded", () => { bind(); translate(); initialize(); initialized = true; });
