const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const qrows = document.querySelector("#qrows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const quarBox = document.querySelector("#quarantine");
const quarToggle = document.querySelector("#quarToggle");
const actionHead = document.querySelector("#actionHead");
const quarActionHead = document.querySelector("#quarActionHead");

function paint(list) {
  rows.innerHTML = list
    .map((r) => {
      const action =
        role === "writer" ? `<td><button data-quar="${r.id}">隔离</button></td>` : "";
      return `<tr><td>${r.site}</td><td>${r.ch4_pct}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td><td>${r.note}</td>${action}</tr>`;
    })
    .join("");
}

function paintQuarantine(list) {
  qrows.innerHTML = list
    .map((r) => {
      const left = Math.max(
        0,
        Math.floor((new Date(r.restore_deadline).getTime() - Date.now()) / 1000),
      );
      const deadline = r.permanent ? "永久隔离" : `剩余 ${left} 秒`;
      const action =
        role === "writer" && !r.permanent
          ? `<td><button data-restore="${r.id}">恢复</button></td>`
          : role === "writer"
            ? "<td></td>"
            : "";
      return `<tr><td>${r.site}</td><td>${r.ch4_pct}</td><td>${r.level}</td><td>${r.quarantine_reason}</td><td>${r.quarantined_by}</td><td>${deadline}</td>${action}</tr>`;
    })
    .join("");
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

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  actionHead.hidden = role !== "writer";
  quarActionHead.hidden = role !== "writer";
  connect();
  load();
}

async function load() {
  paint(await api("/api/readings"));
}

async function loadQuarantine() {
  if (quarBox.hidden) return;
  paintQuarantine(await api("/api/quarantine"));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "reading") {
      live.textContent = `刚推送：${msg.reading.site} ${msg.reading.level}`;
    } else if (msg.type === "quarantined") {
      live.textContent = "一条记录被移入隔离区";
    } else if (msg.type === "restored") {
      live.textContent = `已恢复：${msg.reading.site}`;
    }
    load();
    loadQuarantine();
  };
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
  showApp();
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

rows.onclick = async (e) => {
  const id = e.target.dataset && e.target.dataset.quar;
  if (!id) return;
  const reason = prompt("请输入隔离原因（必填）");
  if (!reason || !reason.trim()) return;
  try {
    await api(`/api/readings/${id}/quarantine`, {
      method: "POST",
      body: JSON.stringify({ reason: reason.trim() }),
    });
    live.textContent = "已隔离该记录";
    load();
    loadQuarantine();
  } catch (err) {
    live.textContent = err.message;
  }
};

qrows.onclick = async (e) => {
  const id = e.target.dataset && e.target.dataset.restore;
  if (!id) return;
  try {
    await api(`/api/readings/${id}/restore`, { method: "POST" });
    live.textContent = "已恢复该记录";
    load();
    loadQuarantine();
  } catch (err) {
    live.textContent = err.message;
  }
};

quarToggle.onclick = () => {
  quarBox.hidden = !quarBox.hidden;
  quarToggle.textContent = quarBox.hidden ? "隔离区" : "收起隔离区";
  loadQuarantine();
};

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
