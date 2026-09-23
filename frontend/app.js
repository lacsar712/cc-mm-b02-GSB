const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";
let currentTab = "main";
let quarantineList = [];

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const qrows = document.querySelector("#qrows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const tabMain = document.querySelector("#tab-main");
const tabQ = document.querySelector("#tab-q");
const viewMain = document.querySelector("#view-main");
const viewQ = document.querySelector("#view-q");
const qempty = document.querySelector("#qempty");

const isWriter = role === "writer";

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmtTime(value) {
  if (!value) return "";
  const d = new Date(value);
  return d.toLocaleString("zh-CN", { hour12: false });
}

function paintMain(list) {
  document.querySelector("#main-head").innerHTML = isWriter
    ? "<th>测点</th><th>甲烷 %</th><th>状态</th><th>说明</th><th colspan='2'>误报隔离</th>"
    : "<th>测点</th><th>甲烷 %</th><th>状态</th><th>说明</th>";
  rows.innerHTML = list
    .map((r) => {
      const action = isWriter
        ? `<td><input class="qreason" id="reason-${r.id}" placeholder="隔离原因（必填）" maxlength="200" /></td>
           <td><button onclick="quarantine(${r.id})">隔离</button></td>`
        : "";
      return `<tr>
        <td>${esc(r.site)}</td>
        <td>${esc(r.ch4_pct)}</td>
        <td class="${r.level === "报警" ? "alarm" : "ok"}">${esc(r.level)}</td>
        <td>${esc(r.note)}</td>${action}
      </tr>`;
    })
    .join("");
}

function remainingText(r, nowMs) {
  if (r.quarantine_permanent) return `<span class="permanent">已永久隔离</span>`;
  if (!r.quarantine_expires_at) return `<span class="permanent">已永久隔离</span>`;
  const left = Math.max(0, Math.floor((new Date(r.quarantine_expires_at).getTime() - nowMs) / 1000));
  if (left <= 0) return `<span class="permanent">已永久隔离</span>`;
  const mm = String(Math.floor(left / 60)).padStart(2, "0");
  const ss = String(left % 60).padStart(2, "0");
  return `剩 ${mm}:${ss}，可恢复`;
}

function paintQ(list, nowMs = Date.now()) {
  document.querySelector("#q-head").innerHTML = isWriter
    ? "<th>测点</th><th>甲烷 %</th><th>状态</th><th>隔离原因</th><th>隔离人</th><th>隔离时间</th><th>恢复时限</th><th>操作</th>"
    : "<th>测点</th><th>甲烷 %</th><th>状态</th><th>隔离原因</th><th>隔离人</th><th>隔离时间</th><th>恢复时限</th>";
  qrows.innerHTML = list
    .map((r) => {
      const permanent = r.quarantine_permanent
        || !r.quarantine_expires_at
        || new Date(r.quarantine_expires_at).getTime() <= nowMs;
      const action = isWriter
        ? `<td>${permanent ? '<span class="permanent">不可恢复</span>' : `<button onclick="restore(${r.id})">恢复</button>`}</td>`
        : "";
      return `<tr>
        <td>${esc(r.site)}</td>
        <td>${esc(r.ch4_pct)}</td>
        <td class="${r.level === "报警" ? "alarm" : "ok"}">${esc(r.level)}</td>
        <td>${esc(r.quarantine_reason)}</td>
        <td>${esc(r.quarantined_by)}</td>
        <td>${esc(fmtTime(r.quarantined_at))}</td>
        <td>${remainingText(r, nowMs)}</td>${action}
      </tr>`;
    })
    .join("");
  qempty.hidden = list.length > 0;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

async function loadMain() {
  paintMain(await api("/api/readings"));
}

async function loadQ() {
  quarantineList = await api("/api/quarantine");
  paintQ(quarantineList);
}

async function refreshActive() {
  if (currentTab === "main") {
    await loadMain();
  } else {
    await loadQ();
  }
}

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "旁观";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  connect();
  loadMain();
  loadQ();
}

function switchTab(tab) {
  currentTab = tab;
  tabMain.classList.toggle("active", tab === "main");
  tabQ.classList.toggle("active", tab === "q");
  viewMain.hidden = tab !== "main";
  viewQ.hidden = tab !== "q";
  refreshActive();
}
tabMain.onclick = () => switchTab("main");
tabQ.onclick = () => switchTab("q");

let ws;
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = async (ev) => {
    const msg = JSON.parse(ev.data);
    const r = msg.reading || {};
    if (msg.type === "reading") {
      live.textContent = `刚推送：${r.site} ${r.level}`;
      if (currentTab === "main") await loadMain();
    } else if (msg.type === "quarantined") {
      live.textContent = `${r.site} 已隔离：${r.quarantine_reason || ""}`;
      // 已打开的总表立即刷掉该行
      await Promise.all([loadQ(), currentTab === "main" ? loadMain() : Promise.resolve()]);
    } else if (msg.type === "restored") {
      live.textContent = `${r.site} 已恢复回总表`;
      await Promise.all([loadQ(), currentTab === "main" ? loadMain() : Promise.resolve()]);
    } else if (msg.type === "quarantine_expired") {
      live.textContent = `${r.site} 隔离超时，已永久隔离`;
      await loadQ();
    }
  };
  ws.onclose = () => setTimeout(connect, 2000);
}

async function quarantine(id) {
  const input = document.querySelector(`#reason-${id}`);
  const reason = input.value.trim();
  if (!reason) {
    live.textContent = "隔离必须填写原因";
    input.focus();
    return;
  }
  try {
    await api(`/api/readings/${id}/quarantine`, { method: "POST", body: JSON.stringify({ reason }) });
  } catch (err) {
    live.textContent = err.message;
  }
}

async function restore(id) {
  try {
    await api(`/api/readings/${id}/restore`, { method: "POST" });
  } catch (err) {
    live.textContent = err.message;
    await loadQ();
  }
}

document.querySelector("#go").onclick = async () => {
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: document.querySelector("#user").value,
      password: document.querySelector("#pass").value,
    }),
  });
  token = data.access_token;
  role = data.role;
  localStorage.setItem(tokenKey, token);
  localStorage.setItem("methane_role", role);
  location.reload();
};

form.onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4_pct: Number(document.querySelector("#ch4").value),
      }),
    });
  } catch (err) {
    live.textContent = err.message;
  }
};

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

// 隔离区倒计时每秒刷新；到点后与服务器同步一次
setInterval(() => {
  if (currentTab !== "q" || !quarantineList.length) return;
  const now = Date.now();
  paintQ(quarantineList, now);
  if (quarantineList.some(
    (r) => !r.quarantine_permanent && r.quarantine_expires_at
      && new Date(r.quarantine_expires_at).getTime() <= now,
  )) {
    loadQ();
  }
}, 1000);

if (token) showApp();
