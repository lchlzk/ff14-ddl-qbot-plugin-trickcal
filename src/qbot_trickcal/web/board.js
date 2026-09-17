"use strict";

const GROUP_LABELS = {
  "攻击": "攻击力",
  "防御": "防御力",
  "生命": "血量",
  "暴击": "暴击/暴伤",
  "暴抗": "暴抗/暴伤抗",
};
const state = {
  data: null,
  csrf: "",
  filter: "owned",
  hideSelected: false,
  hideUnselected: false,
  query: "",
  collectionQuery: "",
  collectionFilter: "all",
  layer: 1,
  group: "攻击",
  busy: false,
  pairingCsrf: "",
  username: "",
};
let pairingTimer = null;
let pairingCompleting = false;
const $ = (selector) => document.querySelector(selector);

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function formatNumber(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
}

function formatDate(timestamp) {
  if (!timestamp) return "尚未修改";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date(timestamp * 1000));
}

async function api(path, options = {}) {
  const config = { credentials: "same-origin", ...options };
  config.headers = { ...(options.headers || {}) };
  if (config.method && config.method !== "GET" && state.csrf) {
    config.headers["X-Trickcal-CSRF"] = state.csrf;
  }
  const response = await fetch(`/tr-board/api/${path}`, config);
  let payload = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("json")) payload = await response.json();
  if (!response.ok) {
    const message = payload && (payload.detail || payload.error);
    const error = new Error(message || "请求没有完成，请稍后重试。");
    error.status = response.status;
    throw error;
  }
  return payload ? payload.data : null;
}

function jsonBody(value) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) };
}

let toastTimer;
function toast(message, error = false) {
  if ($("#collectionDialog").open) {
    const notice = $("#collectionNotice");
    notice.textContent = message;
    notice.classList.toggle("error", error);
  }
  const item = $("#toast");
  item.textContent = message;
  item.classList.toggle("error", error);
  item.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { item.hidden = true; }, 2800);
}

function setBusy(value) {
  state.busy = value;
  document.querySelectorAll("button").forEach((button) => { button.disabled = value; });
}

function showAuth(message = "") {
  closeMobilePanels();
  if ($("#collectionDialog").open) $("#collectionDialog").close();
  $("#app").hidden = true;
  $("#authShell").hidden = false;
  $("#authError").textContent = message;
}

function showApp() {
  $("#authShell").hidden = true;
  $("#app").hidden = false;
}

function closeMobilePanels() {
  document.querySelectorAll(".side-panel.mobile-open").forEach((panel) => {
    panel.classList.remove("mobile-open");
  });
  document.querySelectorAll("[data-mobile-panel]").forEach((button) => {
    button.setAttribute("aria-expanded", "false");
  });
  const backdrop = $("#mobileBackdrop");
  if (backdrop) backdrop.hidden = true;
  document.body.classList.remove("mobile-panel-visible");
}

function toggleMobilePanel(name) {
  if (!window.matchMedia("(max-width: 920px)").matches) return;
  const panel = name === "overview" ? $("#overviewPanel") : $("#resourcePanel");
  const button = document.querySelector(`[data-mobile-panel="${name}"]`);
  if (!panel || !button) return;
  const opening = !panel.classList.contains("mobile-open");
  closeMobilePanels();
  if (!opening) return;
  panel.classList.add("mobile-open");
  button.setAttribute("aria-expanded", "true");
  $("#mobileBackdrop").hidden = false;
  document.body.classList.add("mobile-panel-visible");
  panel.querySelector("[data-mobile-close]")?.focus();
}

function confirmAction(title, copy) {
  const dialog = $("#confirmDialog");
  $("#confirmTitle").textContent = title;
  $("#confirmCopy").textContent = copy;
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
  });
}

function activeNode(unit) {
  const layer = unit.layers.find((item) => item.layer === state.layer);
  return layer ? layer.nodes.find((item) => item.group === state.group) || null : null;
}

function renderMetrics(data) {
  const values = [
    ["已拥有角色", `${data.summary.owned} / ${data.summary.catalog_units}`],
    ["百分比节点", `${data.summary.selected} / ${data.summary.total}`],
    ["已用金币", formatNumber(data.summary.gold)],
    ["已用金蜡笔", `${formatNumber(data.summary.gold_crayons)} 支`],
  ];
  const grid = $("#metricGrid");
  grid.replaceChildren();
  values.forEach(([label, value]) => {
    const card = node("article", "metric");
    card.append(node("span", "", label), node("strong", "", value));
    grid.append(card);
  });

  const percent = data.summary.total
    ? Math.round((data.summary.selected / data.summary.total) * 100)
    : 0;
  $("#totalPercent").textContent = `${percent}%`;
  $("#totalProgressCopy").textContent = `已点 ${data.summary.selected} / ${data.summary.total}`;
  const progress = $("#totalProgress");
  progress.max = Math.max(1, data.summary.total);
  progress.value = data.summary.selected;
}

function renderLayers(data) {
  const root = $("#layerOverview");
  root.replaceChildren();
  data.layers.forEach((layer) => {
    const selected = layer.groups.reduce((sum, item) => sum + item.selected, 0);
    const total = layer.groups.reduce((sum, item) => sum + item.total, 0);
    const button = node("button", `layer-summary${state.layer === layer.layer ? " active" : ""}`);
    button.type = "button";
    button.dataset.layer = String(layer.layer);
    const head = node("span", "layer-summary-head");
    head.append(node("span", "", layer.label), node("span", "", `${selected} / ${total}`));
    const progress = node("progress");
    progress.max = Math.max(1, total);
    progress.value = selected;
    progress.setAttribute("aria-label", `${layer.label}完成度`);
    button.append(head, progress);
    root.append(button);
  });

  const bonus = $("#bonusList");
  bonus.replaceChildren();
  if (data.summary.stats.length) {
    data.summary.stats.forEach((value) => bonus.append(node("span", "", value)));
  } else {
    bonus.append(node("span", "muted", "尚未点亮百分比节点"));
  }
}

function matchesRoleQuery(unit, value) {
  const query = value.trim().normalize("NFKC").toLocaleLowerCase("zh-CN");
  const names = Array.isArray(unit.search_names) ? unit.search_names.join(" ") : "";
  const text = `${unit.name} ${unit.alias} ${unit.id} ${names}`.normalize("NFKC").toLocaleLowerCase("zh-CN");
  return !query || text.includes(query);
}

function unitMatches(unit) {
  const entry = activeNode(unit);
  if (!entry) return false;
  const selected = Boolean(unit.owned && entry.selected);
  if (state.hideSelected && selected) return false;
  if (state.hideUnselected && !selected) return false;
  if (!matchesRoleQuery(unit, state.query)) return false;
  if (state.filter === "owned") return unit.owned;
  if (state.filter === "unowned") return !unit.owned;
  return true;
}

function nameGlyph(unit) {
  const value = (unit.name || unit.alias || "角").replace(/[\s·・]/g, "");
  return Array.from(value).slice(0, 2).join("");
}

function roleCard(unit) {
  const entry = activeNode(unit);
  const selected = Boolean(unit.owned && entry.selected);
  const card = node("article", `character-card${selected ? " selected" : ""}${unit.owned ? "" : " unowned"}`);

  const hit = node("button", "character-hit");
  hit.type = "button";
  hit.dataset.action = "node";
  hit.dataset.unit = String(unit.id);
  hit.setAttribute("aria-pressed", selected ? "true" : "false");
  hit.setAttribute("aria-label", `${unit.name}，${selected ? "已经点亮，点击撤销" : unit.owned ? "尚未点亮，点击点亮" : "尚未拥有，点击自动加入并点亮"}`);

  const personality = Number.isInteger(unit.personality) && unit.personality >= 0 && unit.personality <= 4
    ? unit.personality
    : "neutral";
  const avatar = node("div", `avatar-tile tone-${personality}`);
  const glyph = node("span", "avatar-glyph", nameGlyph(unit));
  avatar.append(glyph);
  if (unit.portrait) {
    const image = node("img", "avatar-image");
    image.src = unit.portrait;
    image.alt = "";
    image.loading = "lazy";
    image.decoding = "async";
    image.addEventListener("load", () => { glyph.hidden = true; });
    image.addEventListener("error", () => { image.hidden = true; });
    avatar.append(image);
  }
  const nodeState = node("span", "node-state", selected ? "✓" : "+");
  nodeState.setAttribute("aria-hidden", "true");
  avatar.append(nodeState);
  const info = node("span", "character-info");
  info.append(node("strong", "", unit.name || unit.alias || `角色 ${unit.id}`));
  let note = selected ? "已点亮" : unit.owned ? "未点亮" : "未拥有 · 点击加入";
  if (entry.gold_crayons) note += ` · 🖍${entry.gold_crayons}`;
  info.append(node("small", "", note));
  hit.append(avatar, info);
  card.append(hit);

  if (entry.planned && !selected) card.append(node("span", "planned-flag", "计划"));
  return card;
}

function collectionCard(unit) {
  const name = unit.name || unit.alias || `角色 ${unit.id}`;
  const button = node("button", `collection-card${unit.owned ? " owned" : " unowned"}`);
  button.type = "button";
  button.dataset.collectionUnit = String(unit.id);
  button.setAttribute("aria-pressed", String(unit.owned));
  button.setAttribute("aria-label", `${name}，${unit.owned ? "已拥有，点击取消拥有" : "未拥有，点击点亮角色"}`);
  button.title = name;
  button.disabled = state.busy;
  const personality = Number.isInteger(unit.personality) && unit.personality >= 0 && unit.personality <= 4
    ? unit.personality : "neutral";
  const avatar = node("span", `avatar-tile tone-${personality}`);
  const glyph = node("span", "avatar-glyph", nameGlyph(unit));
  avatar.append(glyph);
  if (unit.portrait) {
    const image = node("img", "avatar-image");
    image.src = unit.portrait;
    image.alt = "";
    image.loading = "lazy";
    image.decoding = "async";
    image.addEventListener("load", () => { glyph.hidden = true; });
    image.addEventListener("error", () => { image.hidden = true; });
    avatar.append(image);
  }
  const caption = node("span", "collection-caption");
  caption.append(node("strong", "", name), node("small", "", unit.owned ? "已拥有" : "未拥有"));
  button.append(avatar, caption);
  return button;
}

function renderCollection() {
  if (!state.data || !$("#collectionDialog").open) return;
  const all = state.data.units;
  const owned = all.filter((unit) => unit.owned).length;
  const summary = $("#collectionSummary");
  summary.replaceChildren(
    node("span", "", `全部 ${all.length}`),
    node("span", "owned-count", `已拥有 ${owned}`),
    node("span", "unowned-count", `未拥有 ${all.length - owned}`),
  );
  // Stable order: toggling ownership must not move the card under the pointer.
  // This collection deliberately ignores the layer, stat and node filters.
  const units = all.filter((unit) => matchesRoleQuery(unit, state.collectionQuery)
    && (state.collectionFilter === "all" || (state.collectionFilter === "owned" ? unit.owned : !unit.owned)))
    .sort((a, b) => a.id - b.id);
  $("#collectionGrid").replaceChildren(...units.map(collectionCard));
  $("#collectionEmpty").hidden = units.length > 0;
  $("#collectionResultCount").textContent = `显示 ${units.length} / ${all.length} 个角色`;
  document.querySelectorAll("#collectionFilterTabs button").forEach((button) => {
    const active = button.dataset.collectionFilter === state.collectionFilter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function openCollection() {
  if (!state.data || state.busy) return;
  closeMobilePanels();
  state.collectionQuery = "";
  state.collectionFilter = "all";
  $("#collectionSearchInput").value = "";
  $("#collectionNotice").textContent = "更改自动保存，与机器人同步。";
  $("#collectionNotice").classList.remove("error");
  $("#collectionDialog").showModal();
  document.body.classList.add("collection-visible");
  renderCollection();
  $(".collection-scroll").scrollTop = 0;
  // Focus the close control rather than opening the phone's keyboard immediately.
  $("#closeCollectionTopButton").focus();
}

async function handleCollectionAction(event) {
  const button = event.target.closest("button[data-collection-unit]");
  if (!button || state.busy) return;
  const unit = state.data.units.find((item) => item.id === Number(button.dataset.collectionUnit));
  if (!unit) return;
  const owned = !unit.owned;
  if (!owned) {
    const approved = await confirmAction(`取消拥有“${unit.name}”？`,
      "该角色会变为未拥有，同时清除它的已点和计划节点；其他角色不受影响。");
    if (!approved) return;
  }
  await mutate(() => api(`units/${unit.id}/owned`, jsonBody({ owned })),
    `${unit.name} · ${owned ? "已拥有（未增加节点）" : "已取消拥有"}`);
  if ($("#collectionDialog").open) {
    const next = document.querySelector(`#collectionGrid [data-collection-unit="${unit.id}"]`);
    (next || $("#collectionSearchInput")).focus({ preventScroll: true });
  }
}

function renderCurrentSummary() {
  const eligible = state.data.units.filter((unit) => unit.owned && activeNode(unit));
  const selected = eligible.filter((unit) => activeNode(unit).selected);
  const total = eligible.length;
  const rate = total ? Math.round((selected.length / total) * 100) : 0;
  const label = GROUP_LABELS[state.group];
  $("#boardTitle").textContent = `第${["", "一", "二", "三"][state.layer]}层 · ${label}`;
  $("#currentCount").textContent = `${selected.length} / ${total}`;
  $("#currentRate").textContent = `当前完成 ${rate}%`;
  const gold = selected.reduce((sum, unit) => sum + Number(activeNode(unit).gold || 0), 0);
  const crayons = selected.reduce((sum, unit) => sum + Number(activeNode(unit).gold_crayons || 0), 0);
  $("#currentCost").textContent = `当前属性已用金币 ${formatNumber(gold)} · 金蜡笔 ${formatNumber(crayons)}`;
}

function renderRoles() {
  const units = state.data.units.filter(unitMatches);
  $("#resultCount").textContent = `${units.length} 个角色有此节点`;
  const grid = $("#roleGrid");
  grid.replaceChildren(...units.map(roleCard));
  $("#emptyState").hidden = units.length > 0;
  renderCurrentSummary();
}

function syncControlState() {
  document.querySelectorAll("#layerTabs button").forEach((item) => {
    item.classList.toggle("active", Number(item.dataset.layer) === state.layer);
  });
  document.querySelectorAll("#groupTabs button").forEach((item) => {
    item.classList.toggle("active", item.dataset.group === state.group);
  });
  document.querySelectorAll("#filterTabs button").forEach((item) => {
    item.classList.toggle("active", item.dataset.filter === state.filter);
  });
  document.querySelectorAll("#nodeFilterTabs button").forEach((item) => {
    const active = item.dataset.nodeFilter === "selected" ? state.hideSelected : state.hideUnselected;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function render(data) {
  state.data = data;
  state.csrf = data.csrf;
  if (!data.summary.owned && state.filter === "owned") state.filter = "all";
  renderMetrics(data);
  renderLayers(data);
  syncControlState();
  const date = formatDate(data.catalog.fetched);
  const sources = Array.isArray(data.catalog.sources) ? data.catalog.sources.join(" + ") : "公开";
  $("#catalogNote").textContent = `${sources} 节点目录 ${date}${data.catalog.stale ? " · 使用缓存" : ""}`;
  const sync = $("#syncPill");
  sync.classList.toggle("stale", data.catalog.stale);
  sync.querySelector("span").textContent = data.catalog.stale ? "目录暂用缓存" : "已与机器人同步";
  renderRoles();
  renderCollection();
}

async function loadState() {
  const data = await api("state");
  render(data);
}

async function mutate(call, success) {
  if (state.busy) return;
  setBusy(true);
  try {
    const result = await call();
    await loadState();
    toast(typeof success === "function" ? success(result) : success);
  } catch (error) {
    if (error.status === 401) showAuth(error.message);
    else toast(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function handleRoleAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button || state.busy) return;
  const unitId = Number(button.dataset.unit);
  const unit = state.data.units.find((item) => item.id === unitId);
  if (!unit) return;

  const entry = activeNode(unit);
  if (!entry) return;
  const selected = !(unit.owned && entry.selected);
  await mutate(async () => {
    if (!unit.owned && selected) {
      await api(`units/${unitId}/owned`, jsonBody({ owned: true }));
    }
    return api("nodes", jsonBody({
      unit: unitId,
      layer: state.layer,
      group: state.group,
      selected,
    }));
  }, `${unit.name} · 第 ${state.layer} 层${GROUP_LABELS[state.group]}${selected ? "已点亮" : "已撤销"}`);
}

function chooseLayer(layer) {
  if (![1, 2, 3].includes(layer)) return;
  state.layer = layer;
  syncControlState();
  renderLayers(state.data);
  renderRoles();
}

function chooseGroup(group) {
  if (!GROUP_LABELS[group]) return;
  state.group = group;
  syncControlState();
  renderRoles();
}

async function bulk(selected) {
  if (!state.data.summary.owned) {
    toast("请先添加至少一个已拥有角色。", true);
    return;
  }
  if (!selected) {
    const approved = await confirmAction("撤销当前属性？", `将撤销全部已拥有角色第 ${state.layer} 层的${GROUP_LABELS[state.group]}。`);
    if (!approved) return;
  }
  await mutate(
    () => api("nodes/bulk", jsonBody({ layer: state.layer, group: state.group, selected })),
    (result) => `${result.roles} 个角色、${result.changed} 个节点已${selected ? "点亮" : "撤销"}`,
  );
}

async function importFile(file) {
  if (!file) return;
  if (!file.name.toLocaleLowerCase().endsWith(".json")) {
    toast("请选择 Soshage 导出的 .json 文件。", true);
    return;
  }
  const approved = await confirmAction("导入并替换当前记录？", "网页中的现有蜡笔板会被这份 JSON 完整替换。建议先导出备份。");
  if (!approved) return;
  await mutate(
    () => api("import", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: file,
    }),
    (result) => `已导入 ${result.owned} 个角色`,
  );
}

async function issueBotBinding() {
  if (state.busy) return;
  setBusy(true);
  try {
    const result = await api("bot-pairing", jsonBody({}));
    const command = `/tr 蜡笔板 绑定 ${result.code}`;
    $("#botBindingCommand").textContent = command;
    $("#botBindingExpiry").textContent = `绑定码将在 ${formatDate(result.expires)} 过期。绑定后，两个机器人和网页会共用同一份蜡笔板。`;
    $("#botBindingDialog").showModal();
  } catch (error) {
    if (error.status === 401) showAuth(error.message);
    else toast(error.message, true);
  } finally {
    setBusy(false);
  }
}

function renderApiTokens(tokens) {
  const list = $("#apiTokenList");
  list.replaceChildren();
  if (!tokens.length) {
    list.append(node("p", "", "还没有有效令牌。"));
    return;
  }
  tokens.forEach((item) => {
    const row = node("div", "api-token-row");
    const details = node("div");
    details.append(node("strong", "", item.label), node("span", "", `到期：${formatDate(item.expires)} · 最近使用：${formatDate(item.last_used)}`));
    const button = node("button", "button ghost", "撤销");
    button.type = "button";
    button.dataset.tokenId = item.id;
    row.append(details, button);
    list.append(row);
  });
}

async function showApiTokens() {
  if (state.busy) return;
  if (!state.username) {
    toast("请先创建蜡笔板账号，再生成 API 令牌。", true);
    return;
  }
  setBusy(true);
  try {
    renderApiTokens(await api("api-tokens"));
    $("#issuedApiToken").hidden = true;
    $("#apiTokenValue").textContent = "";
    $("#apiTokenDialog").showModal();
  } catch (error) {
    if (error.status === 401) showAuth(error.message);
    else toast(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function createApiToken() {
  if (state.busy) return;
  const label = $("#apiTokenLabel").value.trim();
  if (!label) { toast("请填写用途名称。", true); return; }
  setBusy(true);
  try {
    const issued = await api("api-tokens", jsonBody({ label }));
    $("#apiTokenValue").textContent = issued.token;
    $("#issuedApiToken").hidden = false;
    $("#apiTokenLabel").value = "";
    renderApiTokens(await api("api-tokens"));
    toast("令牌已生成，请立即复制并妥善保管。只显示这一次。");
  } catch (error) {
    if (error.status === 401) showAuth(error.message);
    else toast(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function revokeApiToken(identifier) {
  if (state.busy) return;
  const approved = await confirmAction("撤销 API 令牌？", "使用此令牌的第三方应用将立即无法读取蜡笔板。");
  if (!approved) return;
  setBusy(true);
  try {
    await api(`api-tokens/${identifier}`, { method: "DELETE" });
    renderApiTokens(await api("api-tokens"));
    toast("令牌已撤销");
  } catch (error) {
    if (error.status === 401) showAuth(error.message);
    else toast(error.message, true);
  } finally {
    setBusy(false);
  }
}

function closeApiTokens() {
  $("#apiTokenDialog").close();
  $("#apiTokenValue").textContent = "";
  $("#issuedApiToken").hidden = true;
}

async function copyBotBindingCommand() {
  const command = $("#botBindingCommand").textContent;
  if (!command) return;
  try {
    await navigator.clipboard.writeText(command);
    toast("绑定命令已复制");
  } catch (_) {
    const range = document.createRange();
    range.selectNodeContents($("#botBindingCommand"));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    toast("请长按或右键复制绑定命令");
  }
}

async function redeemFragment() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get("login");
  if (!token) return false;
  history.replaceState(null, "", `${location.pathname}${location.search}`);
  try {
    const result = await api("redeem", jsonBody({ token }));
    state.csrf = result.csrf;
    return true;
  } catch (error) {
    showAuth(error.message);
    return false;
  }
}

function confirmationCommand(code) {
  return `/tr 蜡笔板 绑定 ${code}`;
}

function showPairingWait(code = "") {
  $("#pairingConfirm").hidden = false;
  $("#accountCreateForm").hidden = true;
  if (code) {
    const command = confirmationCommand(code);
    $("#confirmationCommand").textContent = command;
    sessionStorage.setItem("trickcalPairingCommand", command);
  } else {
    $("#confirmationCommand").textContent = sessionStorage.getItem("trickcalPairingCommand") || "回群发送网页刚才显示的回执命令";
  }
  $("#pairingStatus").textContent = "正在等待群聊确认……";
}

function clearPairing() {
  clearTimeout(pairingTimer);
  pairingTimer = null;
  pairingCompleting = false;
  state.pairingCsrf = "";
  sessionStorage.removeItem("trickcalPairingCommand");
}

async function enterBoard(result) {
  state.csrf = result.csrf;
  state.username = result.username || "";
  clearPairing();
  showApp();
  await loadState();
}

async function completeExistingAccount() {
  if (pairingCompleting) return;
  pairingCompleting = true;
  try {
    const result = await api("pairing/complete", {
      ...jsonBody({}),
      headers: { "Content-Type": "application/json", "X-Trickcal-Pairing-CSRF": state.pairingCsrf },
    });
    await enterBoard(result);
    toast(`身份确认成功，已登录账号 ${result.username}`);
  } catch (error) {
    pairingCompleting = false;
    showAuth(error.message);
  }
}

async function checkPairing(schedule = false) {
  try {
    const result = await api("pairing/status");
    state.pairingCsrf = result.csrf;
    showPairingWait();
    if (!result.confirmed) {
      if (schedule) pairingTimer = setTimeout(() => checkPairing(true), 2000);
      return;
    }
    clearTimeout(pairingTimer);
    $("#pairingStatus").textContent = "群聊身份已确认。";
    if (result.account_exists) {
      $("#pairingStatus").textContent = `身份已确认，正在登录账号 ${result.username}……`;
      await completeExistingAccount();
    } else {
      $("#accountCreateForm").hidden = false;
      $("#accountCreateForm input[name='username']").focus();
    }
  } catch (error) {
    clearTimeout(pairingTimer);
    if (!schedule) showAuth(error.message);
    else $("#pairingStatus").textContent = "绑定已过期，请回群重新领取绑定码。";
  }
}

async function restorePairing() {
  try {
    const result = await api("pairing/status");
    state.pairingCsrf = result.csrf;
    showAuth();
    showPairingWait();
    await checkPairing(true);
    return true;
  } catch (_) {
    return false;
  }
}

async function boot() {
  try {
    await redeemFragment();
    const session = await api("session");
    state.csrf = session.csrf;
    state.username = session.username || "";
    showApp();
    await loadState();
  } catch (error) {
    if (error.status === 401 && await restorePairing()) return;
    showAuth(error.status === 401 ? "" : error.message);
  }
}

$("#accountLoginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  button.disabled = true;
  $("#authError").textContent = "";
  try {
    const result = await api("account/login", jsonBody({
      username: form.elements.username.value.trim(),
      password: form.elements.password.value,
    }));
    form.elements.password.value = "";
    await enterBoard(result);
  } catch (error) {
    showAuth(error.message);
  } finally {
    button.disabled = false;
  }
});
$("#pairingStartForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  button.disabled = true;
  $("#authError").textContent = "";
  clearTimeout(pairingTimer);
  try {
    const result = await api("pairing/start", jsonBody({
      code: form.elements.code.value.trim().toUpperCase(),
    }));
    state.pairingCsrf = result.csrf;
    form.elements.code.value = "";
    showPairingWait(result.confirmation);
    pairingTimer = setTimeout(() => checkPairing(true), 2000);
  } catch (error) {
    showAuth(error.message);
  } finally {
    button.disabled = false;
  }
});
$("#accountCreateForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  if (form.elements.password.value !== form.elements.password_confirm.value) {
    showAuth("两次输入的密码不一致。");
    return;
  }
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const result = await api("pairing/complete", {
      ...jsonBody({
        username: form.elements.username.value.trim(),
        password: form.elements.password.value,
      }),
      headers: { "Content-Type": "application/json", "X-Trickcal-Pairing-CSRF": state.pairingCsrf },
    });
    form.reset();
    await enterBoard(result);
    toast("蜡笔板账号创建成功");
  } catch (error) {
    showAuth(error.message);
  } finally {
    button.disabled = false;
  }
});
$("#retryButton").addEventListener("click", () => checkPairing(false));
document.querySelectorAll("[data-mobile-panel]").forEach((button) => {
  button.addEventListener("click", () => toggleMobilePanel(button.dataset.mobilePanel));
});
document.querySelectorAll("[data-mobile-close]").forEach((button) => {
  button.addEventListener("click", closeMobilePanels);
});
$("#mobileBackdrop").addEventListener("click", closeMobilePanels);
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeMobilePanels();
});
window.addEventListener("resize", () => {
  if (!window.matchMedia("(max-width: 920px)").matches) closeMobilePanels();
});
$("#searchInput").addEventListener("input", (event) => {
  state.query = event.target.value;
  renderRoles();
});
$("#layerTabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-layer]");
  if (button) chooseLayer(Number(button.dataset.layer));
});
$("#layerOverview").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-layer]");
  if (button) chooseLayer(Number(button.dataset.layer));
});
$("#groupTabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-group]");
  if (button) chooseGroup(button.dataset.group);
});
$("#filterTabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-filter]");
  if (!button) return;
  state.filter = button.dataset.filter;
  syncControlState();
  renderRoles();
});
$("#nodeFilterTabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-node-filter]");
  if (!button) return;
  if (button.dataset.nodeFilter === "selected") state.hideSelected = !state.hideSelected;
  else state.hideUnselected = !state.hideUnselected;
  syncControlState();
  renderRoles();
});
$("#roleGrid").addEventListener("click", handleRoleAction);
$("#openCollectionButton").addEventListener("click", openCollection);
$("#collectionGrid").addEventListener("click", handleCollectionAction);
$("#collectionSearchInput").addEventListener("input", (event) => {
  state.collectionQuery = event.target.value;
  renderCollection();
});
$("#collectionFilterTabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-collection-filter]");
  if (!button || state.busy) return;
  state.collectionFilter = button.dataset.collectionFilter;
  renderCollection();
});
for (const id of ["#closeCollectionButton", "#closeCollectionTopButton"]) {
  $(id).addEventListener("click", () => $("#collectionDialog").close());
}
$("#collectionDialog").addEventListener("close", () => {
  document.body.classList.remove("collection-visible");
  if (window.matchMedia("(max-width: 920px)").matches) {
    document.querySelector('[data-mobile-panel="resource"]').focus();
  }
});
$("#bulkAddButton").addEventListener("click", () => bulk(true));
$("#bulkRemoveButton").addEventListener("click", () => bulk(false));
$("#ownAllButton").addEventListener("click", async () => {
  const approved = await confirmAction("点亮所有角色？", "会把公开目录中的所有角色加入你的拥有列表，不会自动点亮任何节点。");
  if (!approved) return;
  await mutate(() => api("units/owned-all", jsonBody({})), (result) => `新增 ${result.changed} 个角色`);
});
$("#importButton").addEventListener("click", () => $("#importFile").click());
$("#importFile").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  await importFile(file);
});
$("#exportButton").addEventListener("click", () => { location.href = "/tr-board/api/export"; });
$("#issueBotBindingButton").addEventListener("click", issueBotBinding);
$("#copyBotBindingButton").addEventListener("click", copyBotBindingCommand);
$("#manageApiTokensButton").addEventListener("click", showApiTokens);
$("#createApiTokenButton").addEventListener("click", createApiToken);
$("#closeApiTokenDialogButton").addEventListener("click", closeApiTokens);
$("#copyApiTokenButton").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#apiTokenValue").textContent);
    toast("令牌已复制");
  } catch (_) { toast("请长按令牌文字复制。", true); }
});
$("#apiTokenList").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-token-id]");
  if (button) revokeApiToken(button.dataset.tokenId);
});
$("#apiTokenDialog").addEventListener("close", () => {
  $("#apiTokenValue").textContent = "";
  $("#issuedApiToken").hidden = true;
});
$("#logoutButton").addEventListener("click", async () => {
  try { await api("logout", jsonBody({})); } catch (_) { /* Session is local-only. */ }
  state.data = null;
  state.csrf = "";
  state.username = "";
  showAuth("已退出。可以使用蜡笔板账号密码再次登录。");
});

boot();
