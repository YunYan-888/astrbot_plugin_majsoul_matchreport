/* 雀魂战绩播报管理页面脚本 —— 与 index.html 同目录，用相对路径 ./app.js 引入 */
/* 依赖 index.html 里先加载的 bridge SDK（window.AstrBotPluginPage），所以本文件必须放在它后面 */

// ── 通用（常量 / 切页 / 主题） ──
var EP_LIST = "api/list";
var EP_DELETE = "api/delete";
var EP_GROUPS = "api/groups";
var EP_UPDATE = "api/update";
var EP_LOGS = "api/logs";
var EP_LOG_CONTENT = "api/log_content";
var EP_ACCOUNTS = "api/accounts";
var EP_ACCOUNTS_LOGIN = "api/accounts/login";
var EP_ACCOUNTS_DELETE = "api/accounts/delete";
var EP_ACCOUNTS_TOGGLE = "api/accounts/toggle";
var EP_ACCOUNTS_REORDER = "api/accounts/reorder";
var EP_LIVE_GAMES = "api/live_games";
var EP_FRIENDS = "api/friends";
var EP_FRIEND_REMOVE = "api/friend_remove";
var currentLogFile = "";

var currentTab = "config";
var _configDirty = false;
var _configOriginal = null;
function switchTab(name) {
  currentTab = name;
  document.querySelectorAll(".tab").forEach(function(t){
    t.classList.toggle("active", t.getAttribute("data-tab") === name);
  });
  document.querySelectorAll(".tab-content").forEach(function(c){
    c.classList.toggle("active", c.id === "tab-" + name);
  });
  if (name === "logs") { loadLogs(); startLogTimer(); }
  else { stopLogTimer(); }
  if (name === "config") loadConfig();
  if (name === "commands") loadCommands();
  if (name === "accounts") { loadAccounts(); startAccountsTimer(); }
  else { stopAccountsTimer(); }
  if (name === "friends") loadFriends();
  if (name === "monitor") loadData();
  if (name === "live") loadLiveGames();
}
function spinWhile(btn, promise) {
  if (btn) btn.classList.add("spinning");
  Promise.resolve(promise).finally(function () {
    if (btn) setTimeout(function(){ btn.classList.remove("spinning"); }, 400);
  });
}
function refreshCurrent(btn) {
  if (currentTab === "config") { spinWhile(btn, loadConfig()); return; }
  if (currentTab === "commands") { spinWhile(btn, loadCommands()); return; }
  if (currentTab === "accounts") { spinWhile(btn, refreshAccounts()); return; }
  if (currentTab === "friends") { spinWhile(btn, loadFriends()); return; }
  if (currentTab === "monitor") { refreshMonitor(btn); return; }
  if (currentTab === "live") { refreshLive(btn); return; }
  refreshLogs(btn);
}

function applyTheme(theme) {
  var dark = theme === "dark";
  document.documentElement.classList.toggle("dark", dark);
  var btn = document.getElementById("theme-toggle");
  if (btn) {
    btn.textContent = dark ? "☀️" : "🌙";
    btn.title = dark ? "切换到浅色" : "切换到深色";
  }
}
var _themeTouched = false;

function saveThemeToServer(theme) {
  try {
    if (!window.AstrBotPluginPage || !window.AstrBotPluginPage.apiPost) return;
    window.AstrBotPluginPage.apiPost("api/theme", { theme: theme }).catch(function() {});
  } catch(e) {}
}

async function loadServerTheme() {
  try {
    if (!window.AstrBotPluginPage || !window.AstrBotPluginPage.apiGet) return;
    await window.AstrBotPluginPage.ready();
    var res = await window.AstrBotPluginPage.apiGet("api/theme");
    if (_themeTouched) return;
    var serverTheme = res && res.theme;
    if (!serverTheme && res && res.data) serverTheme = res.data.theme;
    if (serverTheme === "dark" || serverTheme === "light") {
      try { localStorage.setItem("majsoul_theme", serverTheme); } catch(e) {}
      applyTheme(serverTheme);
    } else {
      // 第一次升级到服务端保存：把当前 localStorage 里的主题写回去
      var saved = "light";
      try { saved = localStorage.getItem("majsoul_theme") || "light"; } catch(e) {}
      saveThemeToServer(saved === "dark" ? "dark" : "light");
    }
  } catch(e) {}
}

function toggleTheme() {
  var dark = !document.documentElement.classList.contains("dark");
  var theme = dark ? "dark" : "light";
  _themeTouched = true;
  try { localStorage.setItem("majsoul_theme", theme); } catch(e) {}
  applyTheme(theme);
  saveThemeToServer(theme);
}
(function initTheme() {
  var saved = "light";
  try { saved = localStorage.getItem("majsoul_theme") || "light"; } catch(e) {}
  applyTheme(saved === "dark" ? "dark" : "light");
  loadServerTheme();
  window.addEventListener("load", function() { loadServerTheme(); });
})();

// ── 插件设置页 ──
async function loadConfig() {
  var el = document.getElementById("config-content");
  try {
    await window.AstrBotPluginPage.ready();
    var data = await window.AstrBotPluginPage.apiGet("api/config");
    _configOriginal = JSON.parse(JSON.stringify(data));
    _configDirty = false;
    renderConfig(data);
  } catch(e) {
    el.innerHTML = '<div class="empty">加载配置失败: ' + esc(e.message || String(e)) + '</div>';
  }
}
function extractNodeLabel(url) {
  try {
    var host = new URL(url).hostname;
    if (host.indexOf("sapk") !== -1) return "sapk.ch 主站";
    if (host.indexOf("amae-koromo") !== -1) {
      var sub = host.split(".")[0];
      if (sub === "1" || sub === "data") return "amae 备用1";
      if (sub === "4") return "amae 备用4";
      if (sub === "5-data" || sub === "5") return "amae 备用5";
      return "amae " + sub;
    }
    return host;
  } catch(e) { return "自定义节点"; }
}
function nonfriendLimitOptions(current) {
  var cur = (current === undefined || current === null) ? -1 : parseInt(current);
  if (isNaN(cur)) cur = -1;
  var html = '<option value="-1"' + (cur < 0 ? ' selected' : '') + '>无限制</option>';
  for (var i = 0; i <= 10; i++) {
    html += '<option value="' + i + '"' + (cur === i ? ' selected' : '') + '>' + i + ' 次</option>';
  }
  return html;
}
function monitorValidUnitOptions(current) {
  var cur = String(current || "month");
  var opts = [["day", "日"], ["week", "周"], ["month", "月"], ["forever", "不限制"]];
  var html = "";
  for (var i = 0; i < opts.length; i++) {
    html += '<option value="' + opts[i][0] + '"' + (cur === opts[i][0] ? ' selected' : '') + '>' + opts[i][1] + '</option>';
  }
  return html;
}
var _lastMonitorValidValue = 3;
function applyMonitorValidState() {
  var sel = document.getElementById("cfg-monitor-valid-unit");
  var inp = document.getElementById("cfg-monitor-valid-value");
  if (!sel || !inp) return;
  if (sel.value === "forever") {
    var v = parseInt(inp.value);
    if (v > 0) _lastMonitorValidValue = v;
    inp.value = "";
    inp.disabled = true;
    inp.title = "选「不限制」时不需要填写数值";
  } else {
    inp.disabled = false;
    inp.title = "";
    if (!inp.value) inp.value = _lastMonitorValidValue || 3;
  }
}
function onMonitorValidUnitChange() {
  applyMonitorValidState();
  markConfigDirty();
}
// 日志保存时间：回车 / 点别处保存；改小时先弹一行「是否确定」，点确定才真的删
var _savedLogRetain = 30;
var _pendingLogPurge = false;
function showLogPurgeRow(show) {
  var row = document.getElementById("log-purge-row");
  if (row) row.style.display = show ? "flex" : "none";
}
function onLogRetainChange(input) {
  var raw = parseInt(input.value, 10);
  if (!raw || raw < 1) {
    input.value = _savedLogRetain;
    showLogPurgeRow(false);
    showToast("日志保存天数至少为 1 天", false);
    return;
  }
  var days = Math.min(raw, 3650);
  input.value = days;
  if (days < _savedLogRetain) {
    // 改小：先不保存，等用户点「确定」
    showLogPurgeRow(true);
    return;
  }
  showLogPurgeRow(false);
  _pendingLogPurge = false;
  markConfigDirty();
}
function confirmLogPurge() {
  var input = document.getElementById("cfg-log-days");
  if (!input) return;
  _pendingLogPurge = true;
  showLogPurgeRow(false);
  markConfigDirty();
}
function cancelLogPurge() {
  var input = document.getElementById("cfg-log-days");
  if (input) input.value = _savedLogRetain;
  _pendingLogPurge = false;
  showLogPurgeRow(false);
}
function renderConfig(cfg) {
  _savedLogRetain = parseInt(cfg.log_retain_days, 10) || 30;
  _pendingLogPurge = false;
  var el = document.getElementById("config-content");
  var nodes = cfg.api_nodes || [];
  var nodeHtml = '';
  for (var i = 0; i < nodes.length; i++) {
    var lbl = extractNodeLabel(nodes[i]);
    nodeHtml += '<div class="api-node-item">' +
      '<span class="api-node-label" title="' + esc(nodes[i]) + '">' + esc(lbl) + '</span>' +
      '<input type="text" value="' + esc(nodes[i]) + '" data-idx="' + i + '" onchange="markConfigDirty()" onkeydown="if(event.key===\'Enter\'){event.preventDefault();this.blur();}">' +
      '<button class="btn-test" id="test-btn-' + i + '" onclick="testApiNode(' + i + ')" title="测试此节点">🔍</button>' +
      '<button class="btn-node btn-node-del" onclick="removeApiNode(' + i + ')" title="删除">×</button>' +
      '</div>';
  }
  var html = '' +
    '<div class="config-section">' +
      '<h3>🔌 插件开关</h3>' +
      '<div class="config-row">' +
        '<label>启用插件</label>' +
        '<label class="toggle"><input type="checkbox" id="cfg-enabled" ' + (cfg.enabled !== false ? 'checked' : '') + ' onchange="togglePollSettings(this)"><span class="slider"></span></label>' +
        '<span class="hint" id="enable-hint">' + (cfg.enabled !== false ? '已启用（监控运行中）' : '已停用（所有活动停止）') + '</span>' +
      '</div>' +
    '</div>' +
    '<div class="poll-settings-wrap' + (cfg.enabled === false ? ' disabled' : '') + '" id="poll-settings-wrap">' +
    '<div class="config-section">' +
      '<h3>🔍 轮询设置</h3>' +
      '<div class="config-row">' +
        '<label>好友轮询间隔（秒）</label>' +
        '<input type="number" id="cfg-friend-poll" value="' + (cfg.friend_poll_interval || 30) + '" min="10" max="600" onchange="markConfigDirty()" onkeydown="if(event.key===\'Enter\'){event.preventDefault();this.blur();}">' +
        '<span class="hint">轮询好友观战列表，每次轮询不消耗牌谱查询次数。</span>' +
      '</div>' +
      '<div class="config-row">' +
        '<label>非好友轮询间隔（秒）</label>' +
        '<input type="number" id="cfg-poll" value="' + cfg.poll_interval + '" min="30" max="3600" onchange="markConfigDirty()" onkeydown="if(event.key===\'Enter\'){event.preventDefault();this.blur();}">' +
        '<span class="hint">轮询查询牌谱，每次轮询消耗一次牌谱查询次数。</span>' +
      '</div>' +
    '</div>' +
    '<div class="config-section">' +
      '<h3>👥 播报限制</h3>' +
      '<div class="config-row">' +
        '<label>非好友每日播报次数上限</label>' +
        '<select id="cfg-nonfriend-limit" onchange="markConfigDirty()">' + nonfriendLimitOptions(cfg.nonfriend_daily_limit) + '</select>' +
        '<span class="hint">非好友监控会消耗大量牌谱查询次数，建议不要调整过高，默认为每天两次。</span>' +
      '</div>' +
      '<div class="config-row">' +
        '<label>监控有效期</label>' +
        '<input type="number" id="cfg-monitor-valid-value" value="' + (cfg.monitor_valid_value || 3) + '" min="1" max="3650" style="width:76px" onchange="markConfigDirty()" onkeydown="if(event.key===\'Enter\'){event.preventDefault();this.blur();}">' +
        '<select id="cfg-monitor-valid-unit" style="width:96px" onchange="onMonitorValidUnitChange()">' + monitorValidUnitOptions(cfg.monitor_valid_unit) + '</select>' +
        '<span class="hint">从添加监控开始算起，超过有效期后将停止监控，届时需重新添加监控方可继续播报。</span>' +
      '</div>' +
    '</div>' +
    '<div class="config-section">' +
      '<h3>🎮 播报范围（所有群聊的初始默认设置）</h3>' +
      '<div class="config-row">' +
        '<label>三麻播报</label>' +
        '<label class="toggle"><input type="checkbox" id="cfg-three-player" ' + (cfg.monitor_three_player !== false ? 'checked' : '') + ' onchange="markConfigDirty()"><span class="slider"></span></label>' +
      '</div>' +
      '<div class="config-row">' +
        '<label>东场播报</label>' +
        '<label class="toggle"><input type="checkbox" id="cfg-east" ' + (cfg.monitor_east !== false ? 'checked' : '') + ' onchange="markConfigDirty()"><span class="slider"></span></label>' +
      '</div>' +
      '<div class="config-row">' +
        '<label>友人播报</label>' +
        '<label class="toggle"><input type="checkbox" id="cfg-friend" ' + (cfg.monitor_friend === true ? 'checked' : '') + ' onchange="markConfigDirty()"><span class="slider"></span></label>' +
      '</div>' +
      '<div class="config-row">' +
        '<label>比赛播报</label>' +
        '<label class="toggle"><input type="checkbox" id="cfg-contest" ' + (cfg.monitor_contest === true ? 'checked' : '') + ' onchange="markConfigDirty()"><span class="slider"></span></label>' +
      '</div>' +
      '<div class="config-row">' +
        '<span class="hint">这里是所有群聊的初始默认值，各群可以用「/三麻播报」等指令或在监控列表页面单独调整。</span>' +
      '</div>' +
    '</div>' +
    '<div class="config-section">' +
      '<h3>🤝 好友</h3>' +
      '<div class="config-row">' +
        '<label>自动同意好友申请</label>' +
        '<label class="toggle"><input type="checkbox" id="cfg-auto-approve-friends" ' + (cfg.auto_approve_friends === true ? 'checked' : '') + ' onchange="markConfigDirty()"><span class="slider"></span></label>' +
      '</div>' +
    '</div>' +
    '<div class="config-section">' +
      '<h3>📄 日志</h3>' +
      '<div class="config-row">' +
        '<label>日志保存时间</label>' +
        '<input type="number" id="cfg-log-days" value="' + _savedLogRetain + '" min="1" max="3650" style="width:76px" onchange="onLogRetainChange(this)" onkeydown="if(event.key===\'Enter\'){event.preventDefault();this.blur();}">' +
        '<span class="hint">天</span>' +
      '</div>' +
      '<div class="config-row" id="log-purge-row" style="display:none">' +
        '<span class="hint" style="color:#b45309">即将删除超期日志，是否确定？</span>' +
        '<button class="btn-node" onclick="confirmLogPurge()">确定</button>' +
        '<button class="btn-node" onclick="cancelLogPurge()">取消</button>' +
      '</div>' +
    '</div>' +
    '<div class="config-section">' +
      '<h3>🌐 牌谱屋 API 节点</h3>' +
      '<div class="api-node-list" id="api-node-list">' + nodeHtml + '</div>' +
      '<button class="btn-node btn-node-add" onclick="addApiNode()">＋ 添加节点</button>' +
    '</div>' +
    '</div>' +  // end poll-settings-wrap
    '<div class="config-bar">' +
      '<span class="save-msg" id="config-save-status"></span>' +
    '</div>';
  el.innerHTML = html;
  applyMonitorValidState();
}
var _configSaving = false;
var _configSaveAgain = false;
function markConfigDirty() {
  _configDirty = true;
  saveConfigPage();
}
function togglePollSettings(checkbox) {
  var hint = document.getElementById("enable-hint");
  if (hint) hint.textContent = checkbox.checked ? "已启用（保存后开始运行）" : "已停用（保存后停止所有活动）";
  var wrap = document.getElementById("poll-settings-wrap");
  if (wrap) wrap.classList.toggle("disabled", !checkbox.checked);
  markConfigDirty();
}
// 配置页：改动立即保存（并发时排队，避免旧结果覆盖新结果）
async function saveConfigPage() {
  if (!document.getElementById("cfg-enabled")) return;
  if (_configSaving) { _configSaveAgain = true; return; }
  _configSaving = true;
  var st = document.getElementById("config-save-status");
  var typedLogDays = parseInt(document.getElementById("cfg-log-days").value) || _savedLogRetain || 30;
  // 改小但还没点「确定」时不落库，避免把天数改小却没清日志
  if (typedLogDays < _savedLogRetain && !_pendingLogPurge) typedLogDays = _savedLogRetain;
  var payload = {
    enabled: document.getElementById("cfg-enabled").checked,
    friend_poll_interval: parseInt(document.getElementById("cfg-friend-poll").value) || 30,
    poll_interval: parseInt(document.getElementById("cfg-poll").value) || 120,
    nonfriend_daily_limit: parseInt(document.getElementById("cfg-nonfriend-limit").value),
    monitor_valid_value: parseInt(document.getElementById("cfg-monitor-valid-value").value) || _lastMonitorValidValue || 3,
    monitor_valid_unit: document.getElementById("cfg-monitor-valid-unit").value,
    monitor_three_player: document.getElementById("cfg-three-player").checked,
    monitor_east: document.getElementById("cfg-east").checked,
    monitor_friend: document.getElementById("cfg-friend").checked,
    monitor_contest: document.getElementById("cfg-contest").checked,
    auto_approve_friends: document.getElementById("cfg-auto-approve-friends").checked,
    log_retain_days: typedLogDays,
    purge_logs: _pendingLogPurge,
    api_nodes: getApiNodes(),
  };
  try {
    await window.AstrBotPluginPage.ready();
    var resp = await window.AstrBotPluginPage.apiPost("api/config", payload);
    if (st) {
      if (resp && resp.ok === false) {
        st.textContent = "❌ " + (resp.message || "保存失败");
        st.className = "save-msg err";
        // 保存失败：日志天数改回上次成功的值，避免输入框里的数字和实际配置不一致
        var logInput = document.getElementById("cfg-log-days");
        if (logInput) logInput.value = _savedLogRetain;
        _pendingLogPurge = false;
      } else {
        st.textContent = "✅ " + ((resp && resp.message) || "已保存");
        st.className = "save-msg ok";
        _savedLogRetain = payload.log_retain_days;
        _pendingLogPurge = false;
      }
    }
    _configDirty = false;
  } catch(e) {
    if (st) {
      st.textContent = "❌ " + (e.message || String(e));
      st.className = "save-msg err";
    }
  } finally {
    _configSaving = false;
    if (_configSaveAgain) {
      _configSaveAgain = false;
      saveConfigPage();
    }
  }
}
async function testApiNode(idx) {
  var btn = document.getElementById("test-btn-" + idx);
  var input = document.querySelector('#api-node-list input[data-idx="' + idx + '"]');
  if (!btn || !input) return;
  var url = input.value.trim();
  if (!url) return;
  btn.classList.add("testing");
  btn.textContent = "…";
  try {
    await window.AstrBotPluginPage.ready();
    var resp = await window.AstrBotPluginPage.apiPost("api/test_node", { url: url });
    btn.classList.remove("testing");
    if (resp && resp.ok) {
      btn.innerHTML = '<span class="test-ok">✓</span>';
      btn.title = "可用";
    } else {
      btn.innerHTML = '<span class="test-fail">✗</span>';
      btn.title = resp && resp.message ? resp.message : "不可用";
    }
  } catch(e) {
    btn.classList.remove("testing");
    btn.innerHTML = '<span class="test-fail">✗</span>';
    btn.title = e.message || "请求失败";
  }
}
function getApiNodes() {
  var inputs = document.querySelectorAll("#api-node-list input[data-idx]");
  var nodes = [];
  inputs.forEach(function(inp) {
    var v = inp.value.trim();
    if (v) nodes.push(v);
  });
  return nodes;
}
function getAdmins() {
  var inputs = document.querySelectorAll("#admin-list input[data-admin-idx]");
  var admins = [];
  inputs.forEach(function(inp) {
    var v = inp.value.trim();
    if (v) admins.push(v);
  });
  return admins;
}
function removeApiNode(idx) {
  var list = document.getElementById("api-node-list");
  var items = list.querySelectorAll(".api-node-item");
  if (items[idx]) items[idx].remove();
  list.querySelectorAll("input[data-idx]").forEach(function(inp, i) {
    inp.dataset.idx = i;
    var btn = inp.nextElementSibling;
    if (btn) btn.setAttribute("onclick", "removeApiNode(" + i + ")");
  });
  markConfigDirty();
}
function removeAdmin(idx) {
  var list = document.getElementById("admin-list");
  var items = list.querySelectorAll(".api-node-item");
  if (items[idx]) items[idx].remove();
  list.querySelectorAll("input[data-admin-idx]").forEach(function(inp, i) {
    inp.dataset.adminIdx = i;
    var lbl = inp.previousElementSibling;
    if (lbl) lbl.textContent = "管理员 " + (i+1);
    var btn = inp.nextElementSibling;
    if (btn) btn.setAttribute("onclick", "removeAdmin(" + i + ")");
  });
  markCommandsDirty();
}
function addApiNode() {
  var list = document.getElementById("api-node-list");
  var idx = list.querySelectorAll(".api-node-item").length;
  var div = document.createElement("div");
  div.className = "api-node-item";
  div.innerHTML = '<span class="api-node-label">自定义节点</span>' +
    '<input type="text" value="" data-idx="' + idx + '" placeholder="https://..." onchange="markConfigDirty()" onkeydown="if(event.key===\'Enter\'){event.preventDefault();this.blur();}">' +
    '<button class="btn-test" id="test-btn-' + idx + '" onclick="testApiNode(' + idx + ')" title="测试此节点">🔍</button>' +
    '<button class="btn-node btn-node-del" onclick="removeApiNode(' + idx + ')" title="删除">×</button>';
  list.appendChild(div);
  div.querySelector("input").focus();
  markConfigDirty();
}
function addAdmin() {
  var list = document.getElementById("admin-list");
  var idx = list.querySelectorAll(".api-node-item").length;
  var div = document.createElement("div");
  div.className = "api-node-item";
  div.innerHTML = '<span class="api-node-label">管理员 ' + (idx+1) + '</span>' +
    '<input type="text" value="" data-admin-idx="' + idx + '" placeholder="QQ号" onchange="markCommandsDirty()" onkeydown="if(event.key===\'Enter\'){event.preventDefault();this.blur();}">' +
    '<button class="btn-node btn-node-del" onclick="removeAdmin(' + idx + ')" title="移除">×</button>';
  list.appendChild(div);
  div.querySelector("input").focus();
  markCommandsDirty();
}

// ── 指令设置页 ──
async function loadCommands() {
  var el = document.getElementById("commands-content");
  try {
    await window.AstrBotPluginPage.ready();
    var cfg = await window.AstrBotPluginPage.apiGet("api/config");
    renderCommands(cfg);
  } catch(e) {
    el.innerHTML = '<div class="empty">加载失败: ' + esc(e.message || String(e)) + '</div>';
  }
}
function renderCommands(cfg) {
  var el = document.getElementById("commands-content");
  var admins = cfg.admins_id || [];
  var adminHtml = '';
  var enterBlur = ' onkeydown="if(event.key===\'Enter\'){event.preventDefault();this.blur();}"';
  for (var j = 0; j < admins.length; j++) {
    adminHtml += '<div class="api-node-item">' +
      '<span class="api-node-label">管理员 ' + (j+1) + '</span>' +
      '<input type="text" value="' + esc(admins[j]) + '" data-admin-idx="' + j + '" onchange="markCommandsDirty()"' + enterBlur + '>' +
      '<button class="btn-node btn-node-del" onclick="removeAdmin(' + j + ')" title="移除">×</button>' +
      '</div>';
  }
  var addName = cfg.cmd_add_name || "雀魂播报";
  var removeName = cfg.cmd_remove_name || "删除播报";
  var listName = cfg.cmd_list_name || "雀魂播报列表";
  var paipuName = cfg.cmd_paipu_name || "雀魂牌谱";
  var helpName = cfg.cmd_help_name || "雀魂播报帮助";
  var threeName = cfg.cmd_three_name || "三麻播报";
  var eastName = cfg.cmd_east_name || "东场播报";
  var friendName = cfg.cmd_friend_name || "友人播报";
  var contestName = cfg.cmd_contest_name || "比赛播报";
  var rows = [
    {perm: "所有人可用", span: 5, label: "新增监控", id: "cfg-cmd-add", value: addName, hint: "例如：/" + addName + " 云上归雁"},
    {label: "删除监控", id: "cfg-cmd-remove", value: removeName, hint: "例如：/" + removeName + " 云上归雁"},
    {label: "查看本群监控列表", id: "cfg-cmd-list", value: listName, hint: ""},
    {label: "牌谱回放", id: "cfg-cmd-paipu", value: paipuName, hint: "例如：/" + paipuName + " 牌谱链接或ID"},
    {label: "帮助", id: "cfg-cmd-help", value: helpName, hint: ""},
    {perm: "仅群管理员", span: 4, label: "三麻播报", id: "cfg-cmd-three", value: threeName, hint: "各群单独开关，例如：/" + threeName + " 开"},
    {label: "东场播报", id: "cfg-cmd-east", value: eastName, hint: "各群单独开关，例如：/" + eastName + " 关"},
    {label: "友人播报", id: "cfg-cmd-friend", value: friendName, hint: "各群单独开关，只播报好友的友人场，例如：/" + friendName + " 开"},
    {label: "比赛播报", id: "cfg-cmd-contest", value: contestName, hint: "各群单独开关，只播报好友的比赛场，例如：/" + contestName + " 开"}
  ];
  var tableHtml = '<table class="cmd-table"><thead><tr>' +
    '<th style="width:104px">功能</th><th style="width:330px">指令名</th><th style="width:92px">权限</th>' +
    '</tr></thead><tbody>';
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    tableHtml += '<tr>';
    tableHtml += '<td class="cmd-name">' + esc(r.label) + '</td>';
    tableHtml += '<td>' +
      '<div class="cmd-input">' +
        '<span style="font-weight:700;color:#1a56db;font-size:15px">/</span>' +
        '<input type="text" id="' + r.id + '" value="' + esc(r.value) + '" onchange="markCommandsDirty()"' + enterBlur + '>' +
      '</div>' +
      (r.hint ? '<span class="cmd-hint">' + esc(r.hint) + '</span>' : '') +
      '</td>';
    if (r.perm) {
      tableHtml += '<td class="cmd-perm" rowspan="' + r.span + '">' + esc(r.perm) + '</td>';
    }
    tableHtml += '</tr>';
  }
  tableHtml += '</tbody></table>';

  var html = '' +
    '<div class="config-section">' +
      '<h3>🛠️ 指令名</h3>' +
      tableHtml +
    '</div>' +
    '<div class="config-section">' +
      '<h3>🛡️ 额外授权的管理员</h3>' +
      '<div class="hint" style="margin-bottom:10px">以下管理员不受群管理员限制</div>' +
      '<div id="admin-list">' + adminHtml + '</div>' +
      '<button class="btn-node btn-node-add" onclick="addAdmin()">＋ 添加管理员</button>' +
    '</div>' +
    '<div class="config-bar">' +
      '<span class="save-msg" id="commands-save-status"></span>' +
    '</div>';
  el.innerHTML = html;
}
var _cmdSaving = false;
var _cmdSaveAgain = false;
function markCommandsDirty() {
  saveCommandsPage();
}
async function saveCommandsPage() {
  if (!document.getElementById("cfg-cmd-add")) return;
  if (_cmdSaving) { _cmdSaveAgain = true; return; }
  _cmdSaving = true;
  var st = document.getElementById("commands-save-status");
  var payload = {
    cmd_add_name: document.getElementById("cfg-cmd-add").value,
    cmd_remove_name: document.getElementById("cfg-cmd-remove").value,
    cmd_list_name: document.getElementById("cfg-cmd-list").value,
    cmd_paipu_name: document.getElementById("cfg-cmd-paipu").value,
    cmd_help_name: document.getElementById("cfg-cmd-help").value,
    cmd_three_name: document.getElementById("cfg-cmd-three").value,
    cmd_east_name: document.getElementById("cfg-cmd-east").value,
    cmd_friend_name: document.getElementById("cfg-cmd-friend").value,
    cmd_contest_name: document.getElementById("cfg-cmd-contest").value,
    admins_id: getAdmins(),
  };
  try {
    await window.AstrBotPluginPage.ready();
    var resp = await window.AstrBotPluginPage.apiPost("api/config", payload);
    if (resp && resp.ok === false) {
      if (st) { st.textContent = "❌ " + (resp.message || "保存失败"); st.className = "save-msg err"; }
      return;
    }
    if (st) { st.textContent = "✅ 已保存"; st.className = "save-msg ok"; }
  } catch(e) {
    if (st) { st.textContent = "❌ " + (e.message || String(e)); st.className = "save-msg err"; }
  } finally {
    _cmdSaving = false;
    if (_cmdSaveAgain) {
      _cmdSaveAgain = false;
      saveCommandsPage();
    }
  }
}

// ── 账号池 ──
var _accountsLoaded = false;
var _accountsItems = [];
var _accountDragUid = null;
var _accountsTimer = null;
var ACCOUNTS_REFRESH_MS = 10000;
function startAccountsTimer() {
  stopAccountsTimer();
  _accountsTimer = setInterval(function () {
    if (currentTab === "accounts" && !_accountDragUid) refreshAccounts(true);
  }, ACCOUNTS_REFRESH_MS);
}
function stopAccountsTimer() {
  if (_accountsTimer) { clearInterval(_accountsTimer); _accountsTimer = null; }
}
document.addEventListener("visibilitychange", function () {
  if (!document.hidden && currentTab === "accounts") refreshAccounts(true);
});
window.addEventListener("focus", function () {
  if (currentTab === "accounts") refreshAccounts(true);
});
function loadAccounts() {
  if (!_accountsLoaded) {
    _accountsLoaded = true;
    refreshAccounts();
  } else {
    refreshAccounts(true);
  }
}
async function refreshAccounts(silent) {
  var wrap = document.getElementById("accounts-table-wrap");
  if (!silent) wrap.innerHTML = '<div class="loading">加载中...</div>';
  try {
    await window.AstrBotPluginPage.ready();
    var result = await window.AstrBotPluginPage.apiGet(EP_ACCOUNTS, {_t: Date.now()});
    var items = (result && result.items) || [];
    _accountsItems = items;
    if (!items.length) { wrap.innerHTML = '<div style="color:#888;padding:20px;text-align:center">暂无账号，请点击“添加账号”登录雀魂</div>'; return; }
    var h = '<table class="accounts-table"><thead><tr><th>#</th><th>UID</th><th>昵称</th><th>用户名</th><th title="上次刷新好友列表时的数量 / 上限">好友</th><th>牌谱权限</th><th title="每天 05:01 刷新">今日牌谱</th><th>登录状态</th><th>更新时间</th><th>启用</th><th>操作</th></tr></thead><tbody>';
    items.forEach(function(a) {
      var ls = a.login_status || "未登录";
      var statusStyle = ls === "已登录"
        ? "background:#e8f5e9;color:#2e7d32"
        : (ls.indexOf("重连") >= 0 ? "background:#fff8e1;color:#b26a00" : "background:#f2f4f7;color:#777");
      var statusBadge = '<span style="padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600;' + statusStyle + '">' + esc(ls) + '</span>';
      var permBadge = a.paipu_perm === "无"
        ? '<span style="background:#fce4ec;color:#c62828;padding:2px 8px;border-radius:4px;font-size:12px">无</span>'
        : '<span style="background:#e8f5e9;color:#2e7d32;padding:2px 8px;border-radius:4px;font-size:12px">有</span>';
      var enabled = a.enabled !== false;
      var toggle = '<label class="toggle" style="vertical-align:middle"><input type="checkbox"' + (enabled ? ' checked' : '') + ' onchange="toggleAccount(\'' + esc(a.uid) + '\', this.checked)"><span class="slider"></span></label>';
      h += '<tr draggable="true" data-uid="' + esc(a.uid) + '" title="按住拖动可调整顺序，越靠上优先级越高" ondragstart="accountDragStart(event,this)" ondragover="accountDragOver(event,this)" ondragleave="accountDragLeave(event,this)" ondrop="accountDrop(event,this)" ondragend="accountDragEnd(this)">';
      h += '<td>' + a.index + '</td>';
      h += '<td style="font-family:monospace">' + esc(String(a.display_uid || a.uid)) + '</td>';
      h += '<td>' + esc(a.nickname || "-") + '</td>';
      h += '<td>' + esc(a.username_masked) + '</td>';
      // 好友数量 / 上限：来自「好友列表」最近一次刷新，没有数据就显示 -
      var fCountAcc = a.friend_count;
      var fMaxAcc = parseInt(a.friend_max || 0, 10) || 0;
      var friendText = "-";
      var friendStyle = "font-size:13px;color:#999";
      if (fCountAcc !== null && fCountAcc !== undefined) {
        var fcAcc = parseInt(fCountAcc, 10) || 0;
        friendText = fMaxAcc > 0 ? (fcAcc + "/" + fMaxAcc) : (fcAcc + " 个");
        var fFullAcc = fMaxAcc > 0 && fcAcc >= fMaxAcc;
        friendStyle = "font-size:13px;color:" + (fFullAcc ? "#c62828" : "#333") + (fFullAcc ? ";font-weight:600" : "");
      }
      h += '<td style="' + friendStyle + '">' + esc(friendText) + '</td>';
      h += '<td>' + permBadge + '</td>';
      h += '<td style="font-size:13px;color:#333">' + (a.paipu_today === undefined ? 0 : a.paipu_today) + '</td>';
      h += '<td>' + statusBadge + '</td>';
      h += '<td style="font-size:12px;color:#666">' + esc(a.updated_at || "-") + '</td>';
      h += '<td>' + toggle + '</td>';
      var permBtn = a.paipu_perm === "无"
        ? '<button class="btn-account-perm" onclick="restorePerm(\'' + esc(a.uid) + '\')">恢复权限</button>'
        : '';
      h += '<td style="white-space:nowrap"><div style="display:flex;gap:6px;justify-content:center;flex-wrap:wrap">' +
        permBtn +
        '<button class="btn-account-del" onclick="deleteAccount(\'' + esc(a.uid) + '\', this)">删除</button>' +
        '</div></td>';
      h += '</tr>';
      if (a.last_error) {
        h += '<tr><td></td><td colspan="10" style="font-size:12px;color:#b71c1c;padding:4px 8px;background:#fff5f5">' + esc(a.last_error) + '</td></tr>';
      }
    });
    h += '</tbody></table>';
    wrap.innerHTML = h;
  } catch(e) {
    wrap.innerHTML = '<div style="color:#d32f2f">请求失败: ' + esc(e.message) + '</div>';
  }
}

// ── 账号池（拖拽排序 / 权限 / 开关 / 删除） ──
function accountDragStart(e, tr) {
  _accountDragUid = tr.getAttribute("data-uid");
  tr.classList.add("dragging");
  try {
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", _accountDragUid);
  } catch(err) {}
}
function accountDragOver(e, tr) {
  if (!_accountDragUid) return;
  if (tr.getAttribute("data-uid") === _accountDragUid) return;
  e.preventDefault();
  var rect = tr.getBoundingClientRect();
  var after = (e.clientY - rect.top) > rect.height / 2;
  tr.classList.toggle("drop-above", !after);
  tr.classList.toggle("drop-below", after);
}
function accountDragLeave(e, tr) {
  if (e.relatedTarget && tr.contains(e.relatedTarget)) return;
  tr.classList.remove("drop-above", "drop-below");
}
function accountDragEnd(tr) {
  if (tr) tr.classList.remove("dragging");
  document.querySelectorAll(".accounts-table tbody tr").forEach(function(el){
    el.classList.remove("drop-above", "drop-below");
  });
  _accountDragUid = null;
}
async function accountDrop(e, tr) {
  e.preventDefault();
  var targetUid = tr.getAttribute("data-uid");
  var dragUid = _accountDragUid;
  if (!dragUid || !targetUid || String(dragUid) === String(targetUid)) { accountDragEnd(tr); return; }
  var rect = tr.getBoundingClientRect();
  var after = (e.clientY - rect.top) > rect.height / 2;
  var order = _accountsItems.map(function(a){ return String(a.uid); });
  var from = order.indexOf(String(dragUid));
  var to = order.indexOf(String(targetUid));
  if (from < 0 || to < 0) { accountDragEnd(tr); return; }
  order.splice(from, 1);
  to = order.indexOf(String(targetUid));
  if (after) to += 1;
  order.splice(to, 0, String(dragUid));
  accountDragEnd(tr);
  try {
    await window.AstrBotPluginPage.ready();
    var json = await window.AstrBotPluginPage.apiPost(EP_ACCOUNTS_REORDER, {uids: order});
    if (!json || json.ok === false) {
      showAccountsNotice((json && json.message) || "排序保存失败", false);
    } else {
      showAccountsNotice("顺序已保存，优先从上往下使用", true);
    }
  } catch(err) {
    showAccountsNotice("排序保存失败: " + (err.message || err), false);
  }
  refreshAccounts();
}

async function restorePerm(uid) {
  try {
    await window.AstrBotPluginPage.ready();
    var json = await window.AstrBotPluginPage.apiPost("api/accounts/restore_perm", {uid: uid});
    if (!json || json.ok === false) {
      showAccountsNotice((json && json.message) || "操作失败", false);
      return;
    }
    showAccountsNotice(json.message || "已恢复牌谱权限", true);
    await refreshAccounts();
    setTimeout(refreshAccounts, 800);
  } catch(e) {
    showAccountsNotice("请求失败: " + (e.message || e), false);
  }
}
async function toggleAccount(uid, enabled) {
  try {
    await window.AstrBotPluginPage.ready();
    var json = await window.AstrBotPluginPage.apiPost(EP_ACCOUNTS_TOGGLE, {uid: uid, enabled: enabled});
    if (!json || json.ok === false) {
      showAccountsNotice((json && json.message) || "操作失败", false);
      refreshAccounts();
      return;
    }
    showAccountsNotice(json.message || (enabled ? "已启用" : "已禁用"), true);
    refreshAccounts();
  } catch(e) {
    showAccountsNotice("请求失败: " + (e.message || e), false);
    refreshAccounts();
  }
}

function showAccountsNotice(text, ok) {
  var wrap = document.getElementById("accounts-table-wrap");
  if (!wrap || !wrap.parentNode) return;
  var n = document.createElement("div");
  n.className = "accounts-notice " + (ok ? "ok" : "err");
  n.textContent = text;
  wrap.parentNode.insertBefore(n, wrap);
  setTimeout(function(){ if (n.parentNode) n.parentNode.removeChild(n); }, 3000);
}

async function deleteAccount(uid, btnEl) {
  // iframe 沙箱里 confirm/alert 会被拦截，用按钮二次点击确认
  if (btnEl && !btnEl.dataset.confirmed) {
    btnEl.dataset.confirmed = "1";
    btnEl.textContent = "确认删除？";
    btnEl.style.background = "#fca5a5";
    setTimeout(function() {
      if (btnEl.dataset.confirmed) {
        delete btnEl.dataset.confirmed;
        btnEl.textContent = "删除";
        btnEl.style.background = "";
      }
    }, 3000);
    return;
  }
  if (btnEl) { btnEl.disabled = true; btnEl.textContent = "删除中..."; }
  try {
    await window.AstrBotPluginPage.ready();
    var json = await window.AstrBotPluginPage.apiPost(EP_ACCOUNTS_DELETE, {identifier: uid});
    showAccountsNotice(json && json.ok ? "✅ 删除成功" : ("❌ " + ((json && json.message) || "删除失败")), !!(json && json.ok));
    refreshAccounts();
  } catch(e) {
    showAccountsNotice("请求失败: " + (e.message || e), false);
    refreshAccounts();
  }
}

// ── 好友列表 ──
var _friendsLoaded = false;
function loadFriends() {
  refreshFriends(!!_friendsLoaded);
  _friendsLoaded = true;
}
function levelText(levelId, score) {
  if (!levelId) return "-";
  if (typeof rankName === "function") {
    try { return rankName(levelId) + (score ? " " + score : ""); } catch (e) {}
  }
  return String(levelId) + (score ? " " + score : "");
}
function lastOnlineText(ts) {
  ts = parseInt(ts || 0);
  if (!ts) return "";
  var d = new Date(ts * 1000);
  if (isNaN(d.getTime())) return "";
  var now = new Date();
  var hm = ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
  if (d.toDateString() === now.toDateString()) return hm;
  var yesterday = new Date(now.getTime() - 86400000);
  if (d.toDateString() === yesterday.toDateString()) return "昨天 " + hm;
  if (d.getFullYear() === now.getFullYear()) return (d.getMonth() + 1) + "-" + d.getDate() + " " + hm;
  return d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate() + " " + hm;
}
async function removeFriend(accountId, nickname, btnEl) {
  accountId = parseInt(accountId || 0);
  if (!accountId) { showToast("好友 ID 无效", false); return; }
  // 插件页在 iframe 沙箱里，confirm() 会被拦掉，用「点两次」确认
  if (btnEl && !btnEl.dataset.confirmed) {
    btnEl.dataset.confirmed = "1";
    var oldText = btnEl.textContent;
    btnEl.textContent = "确认删除？";
    btnEl.style.background = "#fca5a5";
    setTimeout(function() {
      if (btnEl.dataset.confirmed) {
        delete btnEl.dataset.confirmed;
        btnEl.textContent = oldText;
        btnEl.style.background = "";
      }
    }, 3000);
    return;
  }
  if (btnEl) {
    delete btnEl.dataset.confirmed;
    btnEl.textContent = "删除中...";
    btnEl.disabled = true;
  }
  try {
    await window.AstrBotPluginPage.ready();
    var res = await window.AstrBotPluginPage.apiPost(EP_FRIEND_REMOVE, { account_id: accountId });
    var data = (res && (res.ok !== undefined || res.message)) ? res : ((res && res.data) || {});
    if (data.ok === false) {
      showToast(data.message || "删除失败", false);
      if (btnEl) { btnEl.textContent = "删除"; btnEl.disabled = false; }
      return;
    }
    showToast((data.message || "已删除该好友") + "（" + (nickname || accountId) + "）", true);
    refreshFriends(true);
  } catch(e) {
    showToast("删除失败: " + (e && e.message ? e.message : e), false);
    if (btnEl) { btnEl.textContent = "删除"; btnEl.disabled = false; }
  }
}
var _friendsWrap = document.getElementById("friends-content");
if (_friendsWrap) {
  _friendsWrap.addEventListener("click", function(ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest("button.btn-friend-del[data-account-id]") : null;
    if (!btn) return;
    removeFriend(btn.dataset.accountId, btn.dataset.nickname, btn);
  });
}
async function refreshFriends(silent) {
  var wrap = document.getElementById("friends-content");
  if (!wrap) return;
  if (!silent) wrap.innerHTML = '<div class="loading">加载中...</div>';
  try {
    await window.AstrBotPluginPage.ready();
    var res = await window.AstrBotPluginPage.apiGet(EP_FRIENDS, {_t: Date.now()});
    if (!res || res.ok === false) {
      wrap.innerHTML = '<div style="color:#d32f2f;padding:12px">' + esc((res && res.message) || "加载失败") + '</div>';
      return;
    }
    // 插件页 SDK 会把响应里的 data 解包出来，两种形式都兼容
    var data = (res && res.accounts) ? res : ((res && res.data) || {});
    var accounts = data.accounts || [];
    if (!accounts.length) {
      wrap.innerHTML = '<div style="color:#888;padding:20px;text-align:center">还没有在线账号，请先在账号池登录</div>';
      return;
    }
    var h = '';
    accounts.forEach(function(acc) {
      var statusBadge = acc.online
        ? '<span style="background:#e8f5e9;color:#2e7d32;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600">在线</span>'
        : '<span style="background:#f2f4f7;color:#777;padding:2px 8px;border-radius:4px;font-size:12px">离线</span>';
      var primary = acc.primary ? '<span style="background:#e3f2fd;color:#1565c0;padding:2px 8px;border-radius:4px;font-size:12px;margin-left:6px">主账号</span>' : '';
      var fCount = parseInt(acc.count || 0, 10) || 0;
      var fMax = parseInt(acc.friend_max || 0, 10) || 0;
      var fFull = fMax > 0 && fCount >= fMax;
      var fText = fMax > 0 ? ('好友 ' + fCount + '/' + fMax) : ('好友 ' + fCount + ' 个');
      h += '<div style="margin:0 0 18px 0">';
      h += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">' +
        '<strong style="font-size:15px">' + esc(acc.nickname || acc.uid) + '</strong>' +
        '<span style="font-family:monospace;color:#666;font-size:12px">' + esc(String(acc.display_uid || acc.uid)) + '</span>' +
        statusBadge + primary +
        '<span style="color:' + (fFull ? '#c62828' : '#888') + ';font-size:12px' + (fFull ? ';font-weight:600' : '') + '">' + esc(fText) + (fFull ? '（已满）' : '') +
        (acc.friends_at ? '，更新于 ' + new Date(acc.friends_at * 1000).toLocaleTimeString() : '') + '</span>' +
        '</div>';
      var friends = acc.friends || [];
      if (!friends.length) {
        h += '<div style="color:#888;font-size:13px;padding:8px 0">这个账号还没有好友</div></div>';
        return;
      }
      h += '<table class="accounts-table friends-table"><thead><tr><th>昵称</th><th>账号ID</th><th>四麻段位</th><th>三麻段位</th><th>状态</th><th style="text-align:center" title="有多少个群在播报这个玩家；鼠标移上去可查看具体群">播报群数</th><th style="text-align:center">操作</th></tr></thead><tbody>';
      _friendGroupMap = {};
      friends.forEach(function(f) {
        var st;
        if (f.game_uuid) {
          st = '<span style="color:#1565c0">对局中</span>';
        } else if (f.online) {
          st = '<span style="color:#2e7d32">在线</span>';
        } else {
          var last = lastOnlineText(f.last_online);
          st = (last ? '<span style="color:#888">' + last + '</span> ' : '') + '<span style="color:#999">离线</span>';
        }
        var mgCount = parseInt(f.monitor_groups || 0, 10) || 0;
        var mgIds = f.monitor_group_ids || [];
        var mgNames = f.monitor_group_names || [];
        var mgList = mgIds.map(function(g, i) {
          return { id: String(g), name: String(mgNames[i] || "").trim() };
        });
        var mgKey = String(f.account_id || 0);
        _friendGroupMap[mgKey] = mgList;
        // 群号顺带写进 data 属性，万一面板失效，鼠标悬停也能看到
        var mgFlat = mgList.map(function(it) { return it.name ? (it.name + " " + it.id) : it.id; }).join("\n");
        var mgTypeHint = mgCount > 0 ? '悬停查看具体群' : '';
        var mgCell = mgCount > 0
          ? '<td style="text-align:center;cursor:help" data-friend-gid="' + attrEsc(mgKey) + '"' +
            ' data-friend-groups="' + attrEsc(mgFlat) + '"' +
            ' title="' + mgTypeHint + '">' + mgCount + '</td>'
          : '<td style="text-align:center"><span style="color:#999">0</span></td>';
        h += '<tr><td><strong>' + esc(f.nickname || "-") + '</strong></td>' +
             '<td style="font-family:monospace">' + (f.display_id || f.account_id || "-") + '</td>' +
             '<td>' + esc(f.rank || "-") + '</td>' +
             '<td>' + esc(f.rank3 || "-") + '</td>' +
             '<td>' + st + '</td>' +
             mgCell +
             '<td style="text-align:center"><button class="btn-friend-del" title="删除该好友" data-account-id="' + (f.account_id || 0) + '" data-nickname="' + attrEsc(f.nickname || "") + '">删除</button></td></tr>';
      });
      h += '</tbody></table></div>';
    });
    wrap.innerHTML = h;
    bindFriendsGroupMenu(wrap);
  } catch(e) {
    wrap.innerHTML = '<div style="color:#d32f2f">请求失败: ' + esc(e.message) + '</div>';
  }
}
// ── 好友列表「播报群数」悬停面板 ────────────────────────
var _groupListPop = null;
var _friendGroupMap = {};
var _hoverGroupCell = null;
// 事件委托绑在列表容器上：表格每次重绘 innerHTML 都不会丢，也不依赖内联事件
function bindFriendsGroupMenu(wrap) {
  if (!wrap || wrap.dataset.grpMenuBound === "1") return;
  wrap.dataset.grpMenuBound = "1";
  wrap.addEventListener("mouseover", function(ev) {
    var cell = ev.target && ev.target.closest ? ev.target.closest("[data-friend-gid]") : null;
    if (!cell) return;
    if (cell === _hoverGroupCell) return;
    _hoverGroupCell = cell;
    friendGroupsMenu(null, cell);
  });
  wrap.addEventListener("mouseout", function(ev) {
    var cell = ev.target && ev.target.closest ? ev.target.closest("[data-friend-gid]") : null;
    if (!cell) return;
    // 只是移动到同一格子的子元素，不算离开
    if (ev.relatedTarget && cell.contains && cell.contains(ev.relatedTarget)) return;
    _hoverGroupCell = null;
    hideGroupListPanel();
  });
}
function hideGroupListPanel() {
  if (_groupListPop) { _groupListPop.remove(); _groupListPop = null; }
}
function showGroupListPanel(ev, list, anchorEl) {
  hideGroupListPanel();
  var items = list || [];
  var el = document.createElement("div");
  el.className = "group-list-pop";
  items.forEach(function(item) {
    var d = document.createElement("div");
    d.className = "glp-item";
    var name = String(item && item.name || "").trim();
    var gid = String(item && item.id || "").trim();
    d.textContent = name ? (name + " " + gid) : gid;
    el.appendChild(d);
  });
  document.body.appendChild(el);
  var x = 0, y = 0;
  if (anchorEl && anchorEl.getBoundingClientRect) {
    // 悬停：贴在格子左下角，往下偏一点
    var r = anchorEl.getBoundingClientRect();
    x = r.left;
    y = r.bottom + 4;
  } else if (ev) {
    x = ev.clientX || 0;
    y = ev.clientY || 0;
  }
  var w = el.offsetWidth, h = el.offsetHeight;
  if (x + w > window.innerWidth - 8) x = window.innerWidth - w - 8;
  if (y + h > window.innerHeight - 8) y = Math.max(8, (anchorEl && anchorEl.getBoundingClientRect ? anchorEl.getBoundingClientRect().top - h - 4 : y - h));
  el.style.left = Math.max(8, x) + "px";
  el.style.top = Math.max(8, y) + "px";
  _groupListPop = el;
}
function friendGroupsMenu(ev, el) {
  var key = String((el && el.getAttribute("data-friend-gid")) || "");
  var list = _friendGroupMap[key] || [];
  if (!list.length) {
    // 兜底：直接读格子上记录的群号文本
    var flat = String((el && el.getAttribute("data-friend-groups")) || "").trim();
    if (flat) {
      list = flat.split(/\n+/).map(function(line) {
        var text = String(line || "").trim();
        return { id: text, name: "" };
      });
    }
  }
  if (!list.length) return;
  showGroupListPanel(ev, list, el);
}
window.addEventListener("resize", hideGroupListPanel);

var _friendsBtn = document.getElementById("friends-refresh");
if (_friendsBtn) _friendsBtn.addEventListener("click", function () { refreshFriends(false); });

document.getElementById("show-login-form").addEventListener("click", function() {
  document.getElementById("login-form-wrap").style.display = "block";
  document.getElementById("acct-username").focus();
});
document.getElementById("hide-login-form").addEventListener("click", function() {
  document.getElementById("login-form-wrap").style.display = "none";
  document.getElementById("acct-login-msg").textContent = "";
  document.getElementById("acct-username").value = "";
  document.getElementById("acct-password").value = "";
});
document.getElementById("acct-login-btn").addEventListener("click", async function() {
  var u = document.getElementById("acct-username").value.trim();
  var p = document.getElementById("acct-password").value;
  var msg = document.getElementById("acct-login-msg");
  if (!u || !p) { msg.style.color = "#d32f2f"; msg.textContent = "请输入用户名和密码"; return; }
  msg.style.color = "#1565c0"; msg.textContent = "登录中，请稍候（最长60秒）...";
  try {
    await window.AstrBotPluginPage.ready();
    var json = await window.AstrBotPluginPage.apiPost(EP_ACCOUNTS_LOGIN, {username: u, password: p});
    if (json.ok) {
      msg.style.color = "#2e7d32"; msg.textContent = "✅ " + (json.message || "登录成功");
      document.getElementById("acct-password").value = "";
      setTimeout(function(){ document.getElementById("login-form-wrap").style.display = "none"; msg.textContent = ""; refreshAccounts(); }, 1500);
    } else {
      msg.style.color = "#d32f2f"; msg.textContent = "❌ " + (json.message || "登录失败");
    }
  } catch(e) {
    msg.style.color = "#d32f2f"; msg.textContent = "请求失败: " + e.message;
  }
});

// ── 监控列表 ──
var monitorView = "player";   // player=按玩家，group=按群聊
var monitorData = [];
var monitorGroupSel = "";

function monitorTabsHtml() {
  return '<div class="monitor-tabs">'
    + '<div class="monitor-tab' + (monitorView === "player" ? " active" : "") + '" onclick="setMonitorView(\'player\')">按玩家</div>'
    + '<div class="monitor-tab' + (monitorView === "group" ? " active" : "") + '" onclick="setMonitorView(\'group\')">按群聊</div>'
    + '</div>';
}

function setMonitorView(view) {
  view = (view === "group") ? "group" : "player";
  if (monitorView === view) return;
  monitorView = view;
  renderMonitor();
}

function buildPlayerTableHtml() {
  var data = monitorData || [];
  var h = '<div class="monitor-scroll"><table class="monitor-table"><thead><tr><th>昵称</th><th>账号ID</th><th>近期战绩</th><th>今日播报</th><th>最后对局</th><th>绑定群号</th><th title="每个群单独计算监控时间；点右侧「更新」把该群的监控时间重置为当前时间">监控时间</th><th>操作</th></tr></thead><tbody>';
    for (var i = 0; i < data.length; i++) {
      var p = data[i];
      var rawWho = p.account_id || p.player_id || "-";
      var who = p.display_id || rawWho;
      var ranksFour = p.recent_ranks || "";
      var ranksThree = p.recent_ranks_three || "";
      var ranks;
      if (ranksFour && ranksThree) {
        ranks = '四[' + esc(ranksFour) + ']<br>三[' + esc(ranksThree) + ']';
      } else if (ranksThree) {
        ranks = '三[' + esc(ranksThree) + ']';
      } else if (ranksFour) {
        ranks = '四[' + esc(ranksFour) + ']';
      } else {
        ranks = '-';
      }
      var lastTime = p.last_played_time || p.latest_time || "未知";
      var bc = parseInt(p.today_broadcast || 0);
      var lim = (p.nonfriend_limit === undefined || p.nonfriend_limit === null) ? -1 : parseInt(p.nonfriend_limit);
      var bcText = p.is_friend
        ? '<span style="color:#2e7d32">' + bc + ' 次（好友）</span>'
        : ('<span style="color:' + (lim >= 0 && bc >= lim ? '#c62828' : '#333') + '">' + bc + (lim >= 0 ? ' / ' + lim : '') + '</span>');
      var allGroups = (p.group_ids || []).join(",");
      var nickEsc = esc(p.nickname).replace(/"/g, '&quot;');
      // 每个绑定群单独一行；没有绑定群的玩家也占一行（监控时间/操作按玩家算）
      var groups = (p.groups && p.groups.length) ? p.groups : [null];
      var groupList = (p.groups || []);
      // 所有群都过期（或没绑定群但玩家已过期）时，才算这个玩家整体过期
      var playerExpired = groupList.length
        ? groupList.every(function(x){ return !!x.expired; })
        : !!p.expired;
      for (var gi = 0; gi < groups.length; gi++) {
        var g = groups[gi];
        var gid = g ? String(g.group_id) : "";
        var gidEsc = esc(gid).replace(/"/g, '&quot;');
        var rawAdded = g ? (g.added_time || "") : (p.added_time || "");
        var expireTime = g ? (g.expire_time || "") : (p.expire_time || "");
        var expired = g ? !!g.expired : !!p.expired;
        var gName = g ? String(g.group_name || "") : "";
        var gs = gid ? '<span class="badge badge-group group-name-toggle" data-gid="' + gidEsc + '" data-gname="' + attrEsc(gName) + '">' + esc(gid) + '</span>' : '<span style="color:#ccc">未绑定</span>';
        var addedTime = rawAdded ? String(rawAdded).slice(0, 16) : "-";
        // 昵称只有一个：玩家所有群都过期时，在昵称后加（过期）；单群过期标在该群监控时间后面
        var nickCell = '<strong>' + esc(p.nickname) + '</strong>' + (playerExpired ? '<span class="expired-tag">（过期）</span>' : '');
        var addedCell = esc(addedTime) + (expired ? '<span class="expired-tag">（过期）</span>' : '');
        var addedTitle = expireTime ? ('到期时间 ' + expireTime) : '无限制（不会过期）';
        if (expired) addedTitle = '监控已过期（' + (expireTime || '') + '），点「更新」重新计时';
        // 每个群单独一行；玩家信息（昵称、账号ID、近期战绩、今日播报、最后对局）用 rowspan 只显示一次
        var first = gi === 0;
        var rs = first ? ' rowspan="' + groups.length + '"' : '';
        var rowCls = (first ? 'player-start' : '') + (expired ? (first ? ' row-expired' : 'row-expired') : '');
        h += '<tr data-nickname="' + nickEsc + '"' + (rowCls ? ' class="' + rowCls + '"' : '') + '>';
        if (first) {
          h += '<td class="nick-cell"' + rs + '>' + nickCell + '</td>';
          h += '<td class="ts acc-cell"' + rs + '>' + esc(String(who)) + '</td>';
          h += '<td class="ts ranks-cell"' + rs + '>' + ranks + '</td>';
          h += '<td class="ts bc-cell"' + rs + '>' + bcText + '</td>';
          h += '<td class="ts last-cell"' + rs + '>' + lastTime + '</td>';
        }
        h += '<td class="group-cell" data-nickname="' + nickEsc + '" title="右键可添加群聊">' + gs + '</td>';
        h += '<td class="ts time-cell" title="' + attrEsc(addedTitle) + '">' + addedCell + '</td>';
        h += '<td class="ops-cell"><span class="op-btns"><button class="btn-update" data-nickname="' + nickEsc + '" data-group="' + gidEsc + '" title="把该群的监控时间更新为当前时间">更新</button><button class="btn-del" data-nickname="' + nickEsc + '" data-group="' + gidEsc + '">删除</button></span></td></tr>';
      }
    }
  h += '</tbody></table></div>';
  return h;
}

function buildGroupViewHtml() {
  var data = monitorData || [];
  var groups = [];
  var seen = {};
  var groupNames = {};
  data.forEach(function(p){
    (p.groups || []).forEach(function(g){
      var id = String(g.group_id || "");
      if (id && !seen[id]) { seen[id] = 1; groups.push(id); }
      var nm = String(g.group_name || "").trim();
      if (id && nm && !groupNames[id]) groupNames[id] = nm;
    });
  });
  groups.sort(function(a, b){ return a.localeCompare(b); });
  if (!groups.length) {
    return '<div class="empty">还没有玩家绑定群聊<br><span style="font-size:13px;color:#bbb">在「按玩家」里右键一行并选择「添加群聊」</span></div>';
  }
  if (!monitorGroupSel || groups.indexOf(monitorGroupSel) < 0) monitorGroupSel = groups[0];
  var members = data.filter(function(p){
    return (p.groups || []).some(function(g){ return String(g.group_id) === monitorGroupSel; });
  }).sort(function(a, b){ return String(a.nickname).localeCompare(String(b.nickname)); });
  var opts = groups.map(function(g){
    var label = groupNames[g] ? (g + ' ' + groupNames[g]) : g;
    return '<option value="' + esc(g).replace(/"/g, '&quot;') + '"' + (g === monitorGroupSel ? ' selected' : '') + '>' + esc(label) + '</option>';
  }).join('');
  var bar = '<div class="monitor-group-bar"><span style="font-size:13px;color:#666">选择群聊：</span><select onchange="onMonitorGroupChange(this)">' + opts + '</select><span style="font-size:12px;color:#999">该群监控 ' + members.length + ' 人</span></div>';
  var h = bar + groupBroadcastBarHtml() + '<div class="monitor-scroll"><table class="monitor-table"><thead><tr><th>昵称</th><th>账号ID</th><th>近期战绩</th><th>今日播报</th><th>最后对局</th><th>绑定群号</th><th title="该群的监控时间；点右侧「更新」把该群的监控时间重置为当前时间">监控时间</th><th>操作</th></tr></thead><tbody>';
  members.forEach(function(p){
    var g = (p.groups || []).filter(function(x){ return String(x.group_id) === monitorGroupSel; })[0] || {};
    var nickEsc = esc(p.nickname).replace(/"/g, '&quot;');
    var gidEsc = esc(monitorGroupSel).replace(/"/g, '&quot;');
    var allExpired = (p.groups || []).length ? (p.groups || []).every(function(x){ return !!x.expired; }) : !!p.expired;
    var expired = !!g.expired;
    var rawAdded = g.added_time || "";
    var addedTime = rawAdded ? String(rawAdded).slice(0, 16) : "-";
    var nickCell = '<strong>' + esc(p.nickname) + '</strong>' + (allExpired ? '<span class="expired-tag">（过期）</span>' : '');
    var addedCell = esc(addedTime) + (expired ? '<span class="expired-tag">（过期）</span>' : '');
    var addedTitle = g.expire_time ? ('到期时间 ' + g.expire_time) : '无限制（不会过期）';
    if (expired) addedTitle = '监控已过期（' + (g.expire_time || '') + '），点「更新」重新计时';
    h += '<tr data-nickname="' + nickEsc + '"' + (expired ? ' class="row-expired"' : '') + '>'
      + '<td class="nick-cell">' + nickCell + '</td>'
      + '<td class="ts acc-cell">' + esc(String(p.display_id || p.account_id || p.player_id || "-")) + '</td>'
      + '<td class="ts ranks-cell">' + playerRanksHtml(p) + '</td>'
      + '<td class="ts bc-cell">' + playerBcHtml(p) + '</td>'
      + '<td class="ts last-cell">' + (p.last_played_time || p.latest_time || "未知") + '</td>'
      + '<td class="group-cell"><span class="badge badge-group group-name-toggle" data-gid="' + gidEsc + '" data-gname="' + attrEsc(String(g.group_name || "")) + '">' + esc(monitorGroupSel) + '</span></td>'
      + '<td class="ts time-cell" title="' + attrEsc(addedTitle) + '">' + addedCell + '</td>'
      + '<td class="ops-cell"><span class="op-btns"><button class="btn-update" data-nickname="' + nickEsc + '" data-group="' + gidEsc + '" title="把该群的监控时间更新为当前时间">更新</button><button class="btn-del" data-nickname="' + nickEsc + '" data-group="' + gidEsc + '">删除</button></span></td></tr>';
  });
  h += '</tbody></table></div>';
  return h;
}

function playerRanksHtml(p) {
  var ranksFour = p.recent_ranks || "";
  var ranksThree = p.recent_ranks_three || "";
  if (ranksFour && ranksThree) return '四[' + esc(ranksFour) + ']<br>三[' + esc(ranksThree) + ']';
  if (ranksThree) return '三[' + esc(ranksThree) + ']';
  if (ranksFour) return '四[' + esc(ranksFour) + ']';
  return '-';
}

function playerBcHtml(p) {
  var bc = parseInt(p.today_broadcast || 0);
  var lim = (p.nonfriend_limit === undefined || p.nonfriend_limit === null) ? -1 : parseInt(p.nonfriend_limit);
  if (p.is_friend) return '<span style="color:#2e7d32">' + bc + ' 次（好友）</span>';
  return '<span style="color:' + (lim >= 0 && bc >= lim ? '#c62828' : '#333') + '">' + bc + (lim >= 0 ? ' / ' + lim : '') + '</span>';
}

function onMonitorGroupChange(sel) {
  monitorGroupSel = sel.value;
  renderMonitor();
}

// ── 各群播报开关（三麻 / 东场 / 友人 / 比赛）──
var EP_GROUP_SETTINGS = "api/group_settings";
var groupSettings = { groups: {}, defaults: {}, labels: {}, kinds: ["three", "east", "friend", "contest"] };

async function loadGroupSettings() {
  try {
    await window.AstrBotPluginPage.ready();
    var res = await window.AstrBotPluginPage.apiGet(EP_GROUP_SETTINGS);
    var data = (res && res.data) ? res.data : res;
    if (data && typeof data === "object") {
      groupSettings.groups = data.groups || {};
      groupSettings.defaults = data.defaults || {};
      groupSettings.labels = data.labels || {};
      groupSettings.kinds = data.kinds || groupSettings.kinds;
    }
  } catch(e) {
    // 拿不到就用插件设置里的默认值显示
  }
}

function groupBroadcastBarHtml() {
  var gid = monitorGroupSel;
  if (!gid) return "";
  var kinds = groupSettings.kinds || ["three", "east", "friend", "contest"];
  var labels = groupSettings.labels || {};
  var stored = (groupSettings.groups || {})[gid] || null;
  var defaults = groupSettings.defaults || {};
  var buttons = kinds.map(function(k){
    var on = (stored && stored[k] !== undefined && stored[k] !== null) ? !!stored[k] : !!defaults[k];
    var label = labels[k] || k;
    return '<button type="button" class="grp-bc-btn' + (on ? ' on' : '') + '" data-kind="' + esc(k) + '"'
      + ' title="点击切换本群的' + esc(label) + '" onclick="toggleGroupBroadcast(this)">'
      + esc(label) + '：' + (on ? '开' : '关') + '</button>';
  }).join('');
  return '<div class="monitor-group-switch"><span style="font-size:13px;color:#666">本群播报：</span>' + buttons
    + '<span style="font-size:12px;color:#999">点一下切换；友人场 / 比赛场只播报好友的对局</span></div>';
}

async function toggleGroupBroadcast(btn) {
  var gid = monitorGroupSel;
  if (!gid || !btn) return;
  var kind = btn.getAttribute("data-kind") || "";
  if (!kind) return;
  var next = !btn.classList.contains("on");
  var label = (groupSettings.labels || {})[kind] || kind;
  btn.disabled = true;
  try {
    await window.AstrBotPluginPage.ready();
    var resp = await window.AstrBotPluginPage.apiPost(EP_GROUP_SETTINGS, {group_id: gid, kind: kind, enabled: next});
    if (resp && resp.ok === false) {
      showToast("❌ " + (resp.message || "保存失败"), false);
      return;
    }
    if (!groupSettings.groups) groupSettings.groups = {};
    if (!groupSettings.groups[gid]) groupSettings.groups[gid] = {};
    groupSettings.groups[gid][kind] = next;
    btn.classList.toggle("on", next);
    btn.textContent = label + "：" + (next ? "开" : "关");
    showToast("✅ 本群" + label + "已" + (next ? "开启" : "关闭"), true);
  } catch(e) {
    showToast("❌ " + (e.message || String(e)), false);
  } finally {
    btn.disabled = false;
  }
}

function renderMonitor() {
  var el = document.getElementById("monitor-content");
  if (!el) return;
  var data = monitorData || [];
  var toolbar = '<div class="toolbar-row"><span>共 <span class="count">' + data.length + '</span> 个监控玩家</span><button class="btn-add" onclick="openAddModal()">＋ 添加玩家</button></div>';
  var tabs = monitorTabsHtml();
  if (!data.length) {
    el.innerHTML = tabs + toolbar + '<div class="empty">暂无监控玩家<br><span style="font-size:13px;color:#bbb">点击上方按钮或在群内发送 /雀魂播报 &lt;昵称&gt; 添加</span></div>';
    return;
  }
  var oldScroll = el.querySelector(".monitor-scroll");
  var top = oldScroll ? oldScroll.scrollTop : 0;
  el.innerHTML = tabs + toolbar + (monitorView === "group" ? buildGroupViewHtml() : buildPlayerTableHtml());
  var sc = el.querySelector(".monitor-scroll");
  if (sc) sc.scrollTop = top;
}

async function loadData(retry) {
  if (typeof retry === "undefined") retry = 0;
  var el = document.getElementById("monitor-content");
  try {
    await window.AstrBotPluginPage.ready();
    var data = await window.AstrBotPluginPage.apiGet(EP_LIST);
    if (!Array.isArray(data)) data = [];
    monitorData = data;
    await loadGroupSettings();
    renderMonitor();
  } catch(e) {
    if (retry < 3) {
      setTimeout(function(){ loadData(retry + 1); }, 1000 * (retry + 1));
      return;
    }
    if (el) el.innerHTML = "<div class=empty>请求失败: " + esc(e.message || String(e)) + "</div>";
  }
}

function showToast(text, ok) {
  var n = document.createElement("div");
  n.className = "toast";
  if (ok === false) n.style.background = "rgba(198,40,40,.95)";
  n.textContent = text;
  document.body.appendChild(n);
  setTimeout(function(){ if (n.parentNode) n.parentNode.removeChild(n); }, 2200);
}

async function del(nickname, groupId, btnEl) {
  // Use inline confirmation instead of confirm() which is blocked by iframe sandbox
  if (btnEl && !btnEl.dataset.confirmed) {
    btnEl.dataset.confirmed = "1";
    btnEl.textContent = groupId ? "确认删群？" : "确认删除？";
    btnEl.style.background = "#fca5a5";
    setTimeout(function() {
      if (btnEl.dataset.confirmed) {
        delete btnEl.dataset.confirmed;
        btnEl.textContent = "删除";
        btnEl.style.background = "";
      }
    }, 3000);
    return;
  }
  if (btnEl) {
    delete btnEl.dataset.confirmed;
    btnEl.textContent = "删除中...";
    btnEl.disabled = true;
  }
  try {
    await window.AstrBotPluginPage.ready();
    await window.AstrBotPluginPage.apiPost(EP_DELETE, { nickname: nickname, group_id: groupId || "" });
    await loadData();
  } catch(e) {
    // Show error inline since alert() is also blocked by sandbox
    if (btnEl) {
      btnEl.textContent = "失败";
      btnEl.disabled = false;
      setTimeout(function() { btnEl.textContent = "删除"; }, 2000);
    }
    console.error("Delete failed:", e);
  }
}
// Event delegation for delete buttons
document.addEventListener("click", function(e) {
  var btn = e.target.closest(".btn-del");
  if (btn && btn.dataset.nickname) {
    del(btn.dataset.nickname, btn.dataset.group || "", btn);
    return;
  }
  var upBtn = e.target.closest(".btn-update");
  if (upBtn && upBtn.dataset.nickname) {
    updateMonitor(upBtn.dataset.nickname, upBtn.dataset.group || "", upBtn);
  }
});

async function updateMonitor(nickname, groupId, btnEl) {
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = "更新中...";
  }
  try {
    await window.AstrBotPluginPage.ready();
    var resp = await window.AstrBotPluginPage.apiPost(EP_UPDATE, { nickname: nickname, group_id: groupId || "" });
    if (resp && resp.ok === false) throw new Error(resp.message || "更新失败");
    // apiPost 可能返回整个响应，也可能已经解包出 data，两种都兼容
    var payload = (resp && resp.data) || (resp && resp.added_time ? resp : null);
    // 只改这一行，不重新加载整张表，避免滚动位置丢失
    applyMonitorRowUpdated(btnEl, payload || {});
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.textContent = "更新";
    }
    showToast("已更新监控时间", true);
  } catch(e) {
    if (btnEl) {
      btnEl.textContent = "失败";
      btnEl.disabled = false;
      setTimeout(function() { btnEl.textContent = "更新"; }, 2000);
    }
    showToast((e && e.message) || "更新失败", false);
    console.error("Update monitor failed:", e);
  }
}

// 更新成功后就地刷新这一行（不整表重绘）；只动这个群这一行，其它群保持原样
function applyMonitorRowUpdated(btnEl, data) {
  if (!btnEl) return;
  var tr = btnEl.closest("tr");
  if (!tr) return;
  tr.classList.remove("row-expired");
  var timeTd = tr.querySelector("td.time-cell");
  if (!timeTd) return;
  var timeTag = timeTd.querySelector(".expired-tag");
  if (timeTag && timeTag.parentNode) timeTag.parentNode.removeChild(timeTag);
  // 昵称是 rowspan 的一格，通常在这一组的第一行；更新后玩家不再「所有群都过期」，去掉昵称后的（过期）
  var nickTd = tr.querySelector("td.nick-cell");
  if (!nickTd) {
    var nickname = tr.getAttribute("data-nickname") || "";
    if (nickname) {
      var rows = document.querySelectorAll(".monitor-table tr[data-nickname]");
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].getAttribute("data-nickname") !== nickname) continue;
        var c = rows[i].querySelector("td.nick-cell");
        if (c) { nickTd = c; break; }
      }
    }
  }
  if (nickTd) {
    var nickTag = nickTd.querySelector(".expired-tag");
    if (nickTag && nickTag.parentNode) nickTag.parentNode.removeChild(nickTag);
  }
  if (data && data.added_time) {
    timeTd.textContent = String(data.added_time).slice(0, 16);
    timeTd.setAttribute("title", data.expire_time ? ("到期时间 " + data.expire_time) : "无限制（不会过期）");
  }
  // 同步内存里的数据，切换「按群聊/按玩家」时不会又显示成过期
  if (data) {
    var nickOfData = data.nickname || tr.getAttribute("data-nickname") || "";
    var gid = String(data.group_id || "");
    for (var mi = 0; mi < (monitorData || []).length; mi++) {
      var mp = monitorData[mi];
      if (String(mp.nickname) !== String(nickOfData)) continue;
      var mg = (mp.groups || []).filter(function(x){ return String(x.group_id) === gid; })[0];
      if (mg) {
        if (data.added_time) mg.added_time = data.added_time;
        if (data.expire_time) mg.expire_time = data.expire_time;
        mg.expired = false;
      } else if (!gid) {
        if (data.added_time) mp.added_time = data.added_time;
        if (data.expire_time) mp.expire_time = data.expire_time;
      }
      mp.expired = (mp.groups && mp.groups.length) ? mp.groups.every(function(x){ return !!x.expired; }) : false;
      break;
    }
  }
}

async function refreshMonitor(btn) {
  if (btn) btn.classList.add("spinning");
  await loadData();
  if (btn) setTimeout(function(){ btn.classList.remove("spinning"); }, 500);
}

// ── 绑定群号：右键菜单「添加群聊」 ──
var monitorCtxNickname = "";

document.addEventListener("contextmenu", function(e) {
  if (monitorView !== "player") return;
  var tr = e.target && e.target.closest ? e.target.closest("tr[data-nickname]") : null;
  if (!tr || !tr.closest(".monitor-table")) return;
  e.preventDefault();
  monitorCtxNickname = tr.getAttribute("data-nickname") || "";
  if (!monitorCtxNickname) return;
  var menu = document.getElementById("monitor-ctx-menu");
  if (!menu) return;
  menu.style.display = "block";
  var x = e.clientX, y = e.clientY;
  var mw = 150, mh = 44;
  if (x + mw > window.innerWidth) x = window.innerWidth - mw - 8;
  if (y + mh > window.innerHeight) y = window.innerHeight - mh - 8;
  menu.style.left = Math.max(8, x) + "px";
  menu.style.top = Math.max(8, y) + "px";
});

document.addEventListener("click", function(e) {
  var menu = document.getElementById("monitor-ctx-menu");
  if (menu && menu.style.display === "block" && !e.target.closest("#monitor-ctx-menu")) {
    menu.style.display = "none";
  }
});

document.addEventListener("keydown", function(e) {
  if (e.key === "Escape") {
    var menu = document.getElementById("monitor-ctx-menu");
    if (menu) menu.style.display = "none";
  }
});

// ── 绑定群号：鼠标移上去弹出群名（和好友列表「播报群数」一样的浮层）──
var _groupNameTip = null;
function hideGroupNameTip() {
  if (_groupNameTip) { _groupNameTip.remove(); _groupNameTip = null; }
}
function showGroupNameTip(anchorEl, name) {
  hideGroupNameTip();
  var text = String(name || "").trim();
  if (!text) return;
  var el = document.createElement("div");
  el.className = "group-list-pop";
  var line = document.createElement("div");
  line.className = "glp-item glp-name";
  line.textContent = text;
  el.appendChild(line);
  document.body.appendChild(el);
  var r = anchorEl.getBoundingClientRect();
  var x = r.left, y = r.bottom + 4;
  var w = el.offsetWidth, h = el.offsetHeight;
  if (x + w > window.innerWidth - 8) x = window.innerWidth - w - 8;
  if (y + h > window.innerHeight - 8) y = Math.max(8, r.top - h - 4);
  el.style.left = Math.max(8, x) + "px";
  el.style.top = Math.max(8, y) + "px";
  _groupNameTip = el;
}
function _groupBadgeOf(e) {
  var badge = e.target && e.target.closest ? e.target.closest(".group-name-toggle") : null;
  if (!badge) return null;
  if (!badge.closest(".monitor-table")) return null;
  return badge;
}
document.addEventListener("mouseover", function(e) {
  var badge = _groupBadgeOf(e);
  if (!badge) return;
  showGroupNameTip(badge, badge.getAttribute("data-gname"));
});
document.addEventListener("mouseout", function(e) {
  var badge = _groupBadgeOf(e);
  if (!badge) return;
  if (e.relatedTarget && badge.contains && badge.contains(e.relatedTarget)) return;
  hideGroupNameTip();
});
window.addEventListener("resize", hideGroupNameTip);

function ctxAddGroup() {
  var menu = document.getElementById("monitor-ctx-menu");
  if (menu) menu.style.display = "none";
  if (monitorCtxNickname) addGroupRow(monitorCtxNickname);
}

// 在这个玩家的群列表末尾插入一行空白群号输入框；点别处保存，留空则删掉这一行
function addGroupRow(nickname) {
  var tbody = document.querySelector(".monitor-table tbody");
  if (!tbody) return;
  var all = Array.prototype.slice.call(tbody.querySelectorAll("tr[data-nickname]"));
  var rows = all.filter(function(r){ return r.getAttribute("data-nickname") === nickname; });
  if (!rows.length) return;
  var pending = tbody.querySelector(".new-group-row");
  if (pending) {
    var pendNick = pending.getAttribute("data-nickname") || "";
    var pin = pending.querySelector("input");
    if (pendNick === nickname && pin) { pin.focus(); return; }
    removePendingGroupRow(pending);
    rows = Array.prototype.slice.call(tbody.querySelectorAll("tr[data-nickname]")).filter(function(r){
      return r.getAttribute("data-nickname") === nickname;
    });
    if (!rows.length) return;
  }
  var last = rows[rows.length - 1];
  var first = rows[0];
  var tr = document.createElement("tr");
  tr.className = "new-group-row";
  tr.setAttribute("data-nickname", nickname);
  tr.innerHTML = '<td class="group-cell"><input class="new-group-input" type="text" placeholder="输入群号，回车或点其他位置保存"></td>'
    + '<td class="ts time-cell">保存时计时</td>'
    + '<td class="ops-cell"><button class="btn-cancel-group" type="button">取消</button></td>';
  last.parentNode.insertBefore(tr, last.nextSibling);
  // 玩家级单元格是 rowspan，新加一行要把 rowspan 加 1
  first.querySelectorAll("td[rowspan]").forEach(function(td){
    td.rowSpan = (parseInt(td.rowSpan, 10) || 1) + 1;
  });
  var input = tr.querySelector("input");
  input.focus();
  var done = false;
  input.addEventListener("blur", function() {
    if (done) return;
    done = true;
    commitNewGroup(tr, nickname);
  });
  input.addEventListener("keydown", function(e) {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    else if (e.key === "Escape") { e.preventDefault(); done = true; removePendingGroupRow(tr); }
  });
  var cancel = tr.querySelector(".btn-cancel-group");
  if (cancel) {
    cancel.addEventListener("mousedown", function(ev) {
      ev.preventDefault();
      done = true;
      removePendingGroupRow(tr);
    });
  }
}

function removePendingGroupRow(tr) {
  if (!tr || !tr.parentNode) return;
  var nickname = tr.getAttribute("data-nickname") || "";
  tr.parentNode.removeChild(tr);
  var rows = Array.prototype.slice.call(document.querySelectorAll(".monitor-table tbody tr[data-nickname]")).filter(function(r){
    return r.getAttribute("data-nickname") === nickname;
  });
  if (!rows.length) return;
  rows[0].querySelectorAll("td[rowspan]").forEach(function(td){
    td.rowSpan = Math.max(1, (parseInt(td.rowSpan, 10) || 1) - 1);
  });
}

async function commitNewGroup(tr, nickname) {
  var input = tr.querySelector(".new-group-input");
  var gid = input ? String(input.value || "").trim().replace(/，/g, ",") : "";
  if (gid.indexOf(",") >= 0) gid = gid.split(",")[0].trim();
  if (!gid) { removePendingGroupRow(tr); return; }
  var p = null;
  (monitorData || []).forEach(function(x){ if (String(x.nickname) === String(nickname)) p = x; });
  var existing = (p && p.group_ids) ? p.group_ids.slice() : [];
  if (existing.indexOf(gid) >= 0) {
    removePendingGroupRow(tr);
    showToast("群号 " + gid + " 已在监控列表中", false);
    return;
  }
  if (input) input.disabled = true;
  try {
    await window.AstrBotPluginPage.ready();
    var json = await window.AstrBotPluginPage.apiPost(EP_GROUPS, { nickname: nickname, group_ids: existing.concat([gid]).join(",") });
    if (!json || json.ok === false) throw new Error((json && json.message) || "添加群聊失败");
    showToast("已添加群聊 " + gid + "，该群监控时间从当前开始", true);
    await loadData();
  } catch(e) {
    removePendingGroupRow(tr);
    showToast("添加群聊失败: " + ((e && e.message) || e), false);
  }
}

// ── 对局列表 ──
var _liveUuid = "";
var _liveAccountId = 0;
var _liveMenuName = "";
var _liveData = [];
var _gameResults = {};
var _awaitingResults = {};
var _resultTimers = {};
var _resultPollMiss = {};
var _liveReqSeq = 0;
var LIVE_TIMEOUT_MS = 60000;

function liveTimeoutHtml() {
  return '<div class="empty">加载失败，请<a class="retry-link" onclick="loadLiveGames()">重试</a></div>';
}

async function loadLiveGames() {
  var el = document.getElementById("live-content");
  var note = document.getElementById("live-note");
  if (!el) return;
  var seq = ++_liveReqSeq;
  var timedOut = false;
  el.innerHTML = '<div class="loading">加载中...</div>';
  if (note) note.style.display = "none";
  var timer = setTimeout(function() {
    if (seq === _liveReqSeq) {
      timedOut = true;
      el.innerHTML = liveTimeoutHtml();
    }
  }, LIVE_TIMEOUT_MS);
  try {
    await window.AstrBotPluginPage.ready();
    var room = document.getElementById("live-room").value;
    var direction = document.getElementById("live-direction").value;
    var query = {};
    if (room) query.room = room;
    if (direction) query.direction = direction;
    var includeOldEl = document.getElementById("live-include-old");
    query.include_old = (includeOldEl && !includeOldEl.checked) ? "0" : "1";
    var resp = await window.AstrBotPluginPage.apiGet(EP_LIVE_GAMES, query);
    if (seq !== _liveReqSeq || timedOut) return;
    // 插件页 SDK 会直接把响应里的 data 字段解包出来；为兼容两种返回形式都处理
    var data = [];
    var message = "";
    if (Array.isArray(resp)) {
      data = resp;
    } else if (resp && typeof resp === "object") {
      if (Array.isArray(resp.data)) data = resp.data;
      message = resp.message || "";
      if (resp.ok === false && !data.length) {
        el.innerHTML = '<div class="empty">' + esc(message || "加载失败，请重试") + '</div>';
        if (note) note.style.display = "none";
        return;
      }
    }
    if (note) {
      if (message) {
        note.textContent = message;
        note.style.display = "block";
      } else {
        note.style.display = "none";
      }
    }
    renderLiveGames(data);
    _liveData = data;
  } catch(e) {
    if (seq !== _liveReqSeq || timedOut) return;
    el.innerHTML = '<div class="empty">加载失败，请<a class="retry-link" onclick="loadLiveGames()">重试</a><br><span style="font-size:12px;color:#bbb">' + esc(e.message || String(e)) + '</span></div>';
  } finally {
    clearTimeout(timer);
  }
}

function onLiveSearch() {
  renderLiveGames(_liveData);
}

function renderLiveGames(data) {
  var el = document.getElementById("live-content");
  var includeOld = document.getElementById("live-include-old");
  if (includeOld && !includeOld.checked) {
    data = data.filter(function (g) { return g.live !== false; });
  }
  var searchEl = document.getElementById("live-search");
  var exactEl = document.getElementById("live-search-exact");
  var query = searchEl ? searchEl.value.trim() : "";
  var exact = exactEl ? exactEl.checked : false;
  var queryLower = query.toLowerCase();
  function nameMatches(name) {
    if (!query) return false;
    name = String(name || "");
    return exact ? name === query : name.toLowerCase().indexOf(queryLower) >= 0;
  }
  if (query) {
    data = data.filter(function (g) {
      return (g.players || []).some(function (p) {
        return nameMatches(p.nickname || String(p.account_id || ""));
      });
    });
  }
  if (!data.length) {
    if (query) {
      el.innerHTML = '<div class="empty">没有找到包含「' + esc(query) + '」的对局</div>';
    } else {
      el.innerHTML = '<div class="empty">当前没有符合条件的对局<br><span style="font-size:13px;color:#bbb">换个筛选条件，或点右上角刷新</span></div>';
    }
    return;
  }
  var liveCount = data.filter(function (g) { return g.live !== false; }).length;
  var oldCount = data.length - liveCount;
  var countText = '共 ' + data.length + ' 局' + (oldCount ? '（进行中 ' + liveCount + ' / 较早 ' + oldCount + '）' : '');
  if (query) countText += ' · 搜索「' + esc(query) + '」' + (exact ? '（精确）' : '');
  var h = '<div class="toolbar-row"><span>' + countText + '</span><span style="font-size:12px;color:#999">右键某一局可查看观战 uuid，右键玩家名字可复制昵称</span></div>';
  h += '<table class="live-table"><thead><tr><th>开局时间</th><th>房间</th><th>玩家</th><th>结果</th></tr></thead><tbody>';
  for (var i = 0; i < data.length; i++) {
    var g = data[i];
    var t = g.start_time ? formatLiveTime(g.start_time) : "未知";
    var oldTag = g.live === false ? '<span class="live-old-tag">较早</span>' : "";
    var players = g.players || [];
    var ps = players.map(function(p){
      var rank = p.rank ? '<span class="rank rank-' + (p.rank_id || 0) + '">' + esc(p.rank) + '</span>' : "";
      var name = p.nickname || String(p.account_id || "");
      var hitCls = nameMatches(name) ? " pname-hit" : "";
      return '<div class="live-player' + hitCls + '" data-name="' + esc(name) + '" oncontextmenu="openLivePlayerMenu(event, this, \'' + esc(g.uuid || "") + '\')">' + rank + '<span class="pname" title="' + esc(name) + '">' + esc(name) + '</span></div>';
    }).join("");
    var cols = players.length === 3 ? 3 : 4;
    h += '<tr data-uuid="' + esc(g.uuid || "") + '" oncontextmenu="openLiveMenu(event, this)">';
    h += '<td class="ts">' + esc(t) + oldTag + '</td><td>' + esc(g.mode_label || "其他对局") + '</td>';
    h += '<td><div class="live-players" style="grid-template-columns:repeat(' + cols + ',minmax(0,1fr))">' + ps + '</div></td>';
    var uid = g.uuid || "";
    var shown = !!(uid && _gameResults[uid]);
    var waiting = !!(uid && _awaitingResults[uid]);
    var btnLabel = shown ? "已显示" : (waiting ? "等待结束" : "显示结果");
    var btnDisabled = (shown || waiting) ? " disabled" : "";
    h += '<td><button class="btn-result" data-uuid="' + esc(uid) + '" data-mode="' + (g.mode_id || 0) + '" data-start="' + (g.start_time || 0) + '" data-account="' + ((players[0] && players[0].account_id) || 0) + '"' + btnDisabled + '>' + btnLabel + '</button></td></tr>';
    if (uid && _gameResults[uid]) {
      h += renderResultRowHtml(uid);
    }
  }
  h += '</tbody></table>';
  el.innerHTML = h;
}

function renderResultRowHtml(uuid) {
  var res = _gameResults[uuid] || {};
  var list = res.players || [];
  var cols = list.length === 3 ? 3 : 4;
  var chips = list.map(function (p) {
    return '<span class="res-chip"><span class="res-rank">' + (p.rank || "-") + '位</span>' +
      (p.point === undefined ? "" : esc(String(p.point))) + " " + esc(p.nickname || "") + '</span>';
  }).join("");
  return '<tr class="live-result-row" data-result-for="' + esc(uuid) + '"><td colspan="4"><div class="result-chips" style="grid-template-columns:repeat(' + cols + ',minmax(0,1fr))">' + chips + '</div></td></tr>';
}

async function fetchGameResult(uuid, btn) {
  if (!uuid) return;
  var modeId = btn ? (btn.dataset.mode || 0) : 0;
  var startTime = btn ? (btn.dataset.start || 0) : 0;
  var accountId = btn ? (btn.dataset.account || 0) : 0;
  if (btn) { btn.disabled = true; btn.textContent = "查询中…"; }
  try {
    await window.AstrBotPluginPage.ready();
    var resp = await window.AstrBotPluginPage.apiGet("api/game_result", {
      uuid: uuid, mode_id: modeId, start_time: startTime, account_id: accountId, _t: Date.now()
    });
    var data = (resp && resp.data) ? resp.data : resp;
    if (data && data.ended) {
      _gameResults[uuid] = data;
      delete _awaitingResults[uuid];
      stopResultTimer(uuid);
      renderLiveGames(_liveData);
      return;
    }
    _awaitingResults[uuid] = true;
    showToast(data && data.message ? data.message : "对局尚未结束，结束后会自动显示", true);
    startResultTimer(uuid);
    renderLiveGames(_liveData);
  } catch(e) {
    showToast("查询失败: " + (e.message || e), false);
    if (btn && document.body.contains(btn)) {
      btn.disabled = false;
      btn.textContent = "显示结果";
    }
  }
}

function startResultTimer(uuid) {
  if (_resultTimers[uuid]) return;
  _resultTimers[uuid] = setInterval(function () { pollGameResult(uuid); }, 10000);
}

function stopResultTimer(uuid) {
  if (_resultTimers[uuid]) {
    clearInterval(_resultTimers[uuid]);
    delete _resultTimers[uuid];
  }
  delete _awaitingResults[uuid];
  delete _resultPollMiss[uuid];
}

async function pollGameResult(uuid) {
  if (!document.querySelector('tr[data-uuid="' + uuid + '"]')) {
    // 对局已经从列表里消失（多半是打完了）：结果没拿到就再免费等 3 分钟
    _resultPollMiss[uuid] = (_resultPollMiss[uuid] || 0) + 1;
    if (_resultPollMiss[uuid] > 18) {
      stopResultTimer(uuid);
      return;
    }
  } else {
    _resultPollMiss[uuid] = 0;
  }
  try {
    // cache_only=1：只读监控侧已经拉到的结果，轮询本身不消耗查询次数
    var resp = await window.AstrBotPluginPage.apiGet("api/game_result", {uuid: uuid, cache_only: 1, _t: Date.now()});
    var data = (resp && resp.data) ? resp.data : resp;
    if (data && data.ended) {
      _gameResults[uuid] = data;
      stopResultTimer(uuid);
      renderLiveGames(_liveData);
      return;
    }
    if (data && data.pending === false) {
      // 监控已经不再跟这局（插件重启/关闭/玩家被删），停止轮询并放开按钮
      stopResultTimer(uuid);
      showToast("监控已不再跟踪这局，请重新点「显示结果」", true);
      renderLiveGames(_liveData);
    }
  } catch(e) {}
}

function formatLiveTime(ts) {
  var d = new Date(ts * 1000);
  function p(n){ return (n < 10 ? "0" : "") + n; }
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

async function refreshLive(btn) {
  if (btn) btn.classList.add("spinning");
  await loadLiveGames();
  if (btn) setTimeout(function(){ btn.classList.remove("spinning"); }, 500);
}

function openLiveMenu(e, tr, name) {
  e.preventDefault();
  _liveUuid = tr ? (tr.getAttribute("data-uuid") || "") : "";
  _liveMenuName = name || "";
  _liveAccountId = 0;
  for (var i = 0; i < _liveData.length; i++) {
    if ((_liveData[i].uuid || "") === _liveUuid) {
      var ps = _liveData[i].players || [];
      if (ps.length) _liveAccountId = ps[0].account_id || 0;
      break;
    }
  }
  var copyItem = document.getElementById("live-menu-copy");
  if (copyItem) copyItem.style.display = _liveMenuName ? "block" : "none";
  var menu = document.getElementById("live-menu");
  menu.style.left = Math.min(e.clientX, window.innerWidth - 200) + "px";
  menu.style.top = Math.min(e.clientY, window.innerHeight - 80) + "px";
  menu.classList.add("open");
}
function openLivePlayerMenu(e, el, uuid) {
  e.preventDefault();
  e.stopPropagation();
  var tr = el.closest("tr");
  var name = el.getAttribute("data-name") || el.textContent || "";
  openLiveMenu(e, tr, name);
}
function copyLivePlayerName() {
  var name = _liveMenuName || "";
  closeLiveMenu();
  if (!name) return;
  function ok() { showToast("已复制玩家名字：" + name, true); }
  function fallback() {
    try {
      var ta = document.createElement("textarea");
      ta.value = name;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    } catch(err) {}
    ok();
  }
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(name).then(ok, fallback);
    } else {
      fallback();
    }
  } catch(e) {
    fallback();
  }
}
function closeLiveMenu() {
  var menu = document.getElementById("live-menu");
  if (menu) menu.classList.remove("open");
}
async function showUuid() {
  var spectateEl = document.getElementById("uuid-spectate");
  var paipuEl = document.getElementById("uuid-paipu");
  var uuid = _liveUuid || "";
  document.getElementById("uuid-modal").classList.add("open");
  spectateEl.textContent = "加载中…";
  paipuEl.textContent = "加载中…";
  if (!uuid) {
    spectateEl.textContent = "-";
    paipuEl.textContent = "-";
    return;
  }
  try {
    await window.AstrBotPluginPage.ready();
    var resp = await window.AstrBotPluginPage.apiGet("api/uuid_links", {
      uuid: uuid, account_id: _liveAccountId || 0, _t: Date.now()
    });
    var data = (resp && resp.data) ? resp.data : resp;
    var spectate = (data && data.spectate) || "";
    var paipu = (data && data.paipu) || "";
    spectateEl.innerHTML = spectate
      ? '<a href="' + esc(spectate) + '" target="_blank" rel="noopener">' + esc(spectate) + '</a>'
      : "-";
    paipuEl.innerHTML = paipu
      ? '<a href="' + esc(paipu) + '" target="_blank" rel="noopener">' + esc(paipu) + '</a>'
      : "-";
  } catch(e) {
    spectateEl.textContent = "获取失败: " + (e.message || e);
    paipuEl.textContent = "-";
  }
}
function closeUuidModal() {
  document.getElementById("uuid-modal").classList.remove("open");
}
document.addEventListener("click", function(e) {
  if (!e.target.closest("#live-menu")) closeLiveMenu();
});
(function bindLiveWidgets() {
  var menuUuid = document.getElementById("live-menu-uuid");
  if (menuUuid) menuUuid.addEventListener("click", function(){ closeLiveMenu(); showUuid(); });
  var menuCopy = document.getElementById("live-menu-copy");
  if (menuCopy) menuCopy.addEventListener("click", copyLivePlayerName);
  var uuidModal = document.getElementById("uuid-modal");
  if (uuidModal) uuidModal.addEventListener("click", function(e){ if (e.target === this) closeUuidModal(); });
})();

document.addEventListener("click", function(e) {
  var btn = e.target.closest(".btn-result");
  if (btn && btn.dataset.uuid) fetchGameResult(btn.dataset.uuid, btn);
});

function esc(s) { var d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

// ── 日志 ──
async function loadLogs() {
  var el = document.getElementById("logs-content");
  try {
    await window.AstrBotPluginPage.ready();
    var data = await window.AstrBotPluginPage.apiGet(EP_LOGS);
    if (!Array.isArray(data)) data = [];
    if (!data.length) {
      el.innerHTML = '<div class="empty">暂无插件日志<br><span style="font-size:13px;color:#bbb">轮询运行后将自动生成日志</span></div>';
      return;
    }
    var h = '<div class="log-split"><div class="log-sidebar"><ul class="log-list">';
    for (var i = 0; i < data.length; i++) {
      var f = data[i];
      var activeClass = (currentLogFile === f.filename) ? " active" : "";
      h += '<li class="log-item' + activeClass + '" data-file="' + f.filename + '" onclick="selectLog(\'' + f.filename + '\', this)">';
      h += '<span class="log-date">' + esc(f.date) + '</span>';
      h += '<span class="log-meta">' + formatSize(f.size) + '</span>';
      h += '</li>';
    }
    h += '</ul></div><div class="log-main"><div id="log-viewer" class="log-viewer">← 选择左侧日期查看日志</div></div></div>';
    el.innerHTML = h;
    if (currentTab === "logs") {
      if (!currentLogFile) selectLog(data[0].filename, null);
      else loadLogContent(currentLogFile);
      return;
    }
  } catch(e) {
    el.innerHTML = "<div class=empty>请求失败: " + esc(e.message || String(e)) + "</div>";
  }
}

async function selectLog(filename, itemEl) {
  currentLogFile = filename;
  document.querySelectorAll(".log-item").forEach(function(el){ el.classList.remove("active"); });
  if (itemEl) itemEl.classList.add("active");
  else {
    var target = document.querySelector('.log-item[data-file="' + filename + '"]');
    if (target) target.classList.add("active");
  }
  await loadLogContent(filename);
  startLogTimer();
}

var currentLogFilter = "ALL";
var cachedLogEntries = [];
// 日志是否自动滚到底部：只是页面上的开关，不保存、重载后回到默认「开启」
var logAutoScroll = true;

var cachedLogTotal = 0;
var cachedLogMatched = 0;
var cachedLogSignature = "";
var _logTimer = null;
var _logIndexTimer = null;
var LOG_REFRESH_MS = 3000;
// 日志搜索：在当天整个日志文件里匹配，不只是最近 1000 条
var logSearchQuery = "";
var _logSearchTimer = null;

function onLogSearchInput() {
  var input = document.getElementById("log-search");
  var clearBtn = document.getElementById("log-search-clear");
  var val = input ? input.value.trim() : "";
  if (clearBtn) clearBtn.style.display = val ? "" : "none";
  if (_logSearchTimer) clearTimeout(_logSearchTimer);
  _logSearchTimer = setTimeout(function () {
    _logSearchTimer = null;
    if (val === logSearchQuery) return;
    logSearchQuery = val;
    if (!currentLogFile || currentTab !== "logs") return;
    loadLogContent(currentLogFile, true);
  }, 300);
}
function clearLogSearch() {
  var input = document.getElementById("log-search");
  if (input) { input.value = ""; input.focus(); }
  var clearBtn = document.getElementById("log-search-clear");
  if (clearBtn) clearBtn.style.display = "none";
  if (_logSearchTimer) { clearTimeout(_logSearchTimer); _logSearchTimer = null; }
  if (!logSearchQuery) return;
  logSearchQuery = "";
  if (currentLogFile && currentTab === "logs") loadLogContent(currentLogFile, true);
}
function updateLogSearchHint() {
  var hint = document.getElementById("log-search-hint");
  if (!hint) return;
  if (!logSearchQuery) { hint.textContent = ""; hint.title = ""; return; }
  var matched = Number(cachedLogMatched);
  if (!isFinite(matched) || matched < 0) matched = 0;
  hint.textContent = "搜索「" + logSearchQuery + "」：当天共 " + cachedLogTotal + " 条，匹配 " + matched + " 条";
  hint.title = hint.textContent;
}

function stopLogTimer() {
  if (_logTimer) { clearInterval(_logTimer); _logTimer = null; }
  if (_logIndexTimer) { clearInterval(_logIndexTimer); _logIndexTimer = null; }
}

function startLogTimer() {
  stopLogTimer();
  _logTimer = setInterval(function () {
    if (currentTab !== "logs" || !currentLogFile) return;
    loadLogContent(currentLogFile, true);
  }, LOG_REFRESH_MS);
  _logIndexTimer = setInterval(function () {
    if (currentTab !== "logs") return;
    refreshLogIndex();
  }, 15000);
}

async function refreshLogIndex() {
  try {
    var data = await window.AstrBotPluginPage.apiGet(EP_LOGS);
    if (!Array.isArray(data)) return;
    var rendered = [];
    document.querySelectorAll(".log-item").forEach(function(el){ rendered.push(el.getAttribute("data-file")); });
    var names = data.map(function(f){ return f.filename; });
    if (rendered.join(",") !== names.join(",")) { loadLogs(); return; }
    data.forEach(function(f){
      var el = document.querySelector('.log-item[data-file="' + f.filename + '"]');
      if (!el) return;
      var meta = el.querySelector(".log-meta");
      if (meta) meta.textContent = formatSize(f.size);
    });
  } catch(e) {}
}

function attrEsc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/"/g, "&quot;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function logRowKey(e) {
  return (e.time || "") + "|" + (e.level || "") + "|" + (e.target || "") + "|" + (e.message || "");
}
// 记录当前滚动位置对应的那一行（以及它的屏幕偏移），刷新后把同一行放回同一个位置：
// 日志是「最近 1000 条」滑动窗口，新日志进来时顶部会丢掉几行，只记住 scrollTop 会一行行往下漂。
function captureLogAnchor(viewer) {
  if (!viewer) return null;
  var distBottom = viewer.scrollHeight - viewer.scrollTop - viewer.clientHeight;
  var rows = viewer.querySelectorAll(".log-row");
  if (!rows.length) return { key: "", offset: 0, distBottom: distBottom };
  var boxTop = viewer.getBoundingClientRect().top;
  for (var i = 0; i < rows.length; i++) {
    var rect = rows[i].getBoundingClientRect();
    if (rect.bottom > boxTop + 1) {
      return {
        key: rows[i].getAttribute("data-key") || "",
        offset: rect.top - boxTop,
        distBottom: distBottom,
      };
    }
  }
  return { key: "", offset: 0, distBottom: distBottom };
}
function restoreLogAnchor(viewer, anchor, autoScroll) {
  if (!viewer) return;
  if (autoScroll) {
    viewer.scrollTop = viewer.scrollHeight;
    return;
  }
  if (!anchor) return;
  if (anchor.key) {
    var rows = viewer.querySelectorAll(".log-row");
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute("data-key") === anchor.key) {
        var delta = rows[i].getBoundingClientRect().top
          - viewer.getBoundingClientRect().top - anchor.offset;
        if (delta) viewer.scrollTop += delta;
        return;
      }
    }
  }
  // 锚点那一行已经被挤出窗口：退而求其次，保持离底部的距离
  viewer.scrollTop = Math.max(0, viewer.scrollHeight - viewer.clientHeight - (anchor.distBottom || 0));
}
function buildFilterBar(entries) {
  var h = '<div class="log-filter-bar">';
  ["ALL","INFO","SCAN","WARN","ERROR"].forEach(function(lv){
    var label = lv === "ALL" ? "全部" : (lv === "SCAN" ? "轮询" : lv);
    var cnt = lv === "ALL" ? entries.length : entries.filter(function(e){ return e.level === lv; }).length;
    var cls = currentLogFilter === lv ? " active" : "";
    h += '<button class="log-filter-btn' + cls + '" data-level="' + lv + '" onclick="applyLogFilter(\''+lv+'\')">' + label + ' (' + cnt + ')</button>';
  });
  h += '<label class="log-scroll-toggle" title="关闭后日志照常更新，只是不再自动滚到底部">' +
    '<input type="checkbox" id="log-autoscroll"' + (logAutoScroll ? ' checked' : '') +
    ' onchange="toggleLogScroll(this.checked)"> 开启滚动</label>';
  h += '</div>';
  return h;
}
function renderLogEntries(entries, filter, total) {
  var filtered = filter === "ALL" ? entries : entries.filter(function(e){ return e.level === filter; });
  var searching = !!logSearchQuery;
  var h = '';
  if (filtered.length === 0) {
    h += '<div style="padding:32px;text-align:center;color:#666">' +
      (searching ? '没有搜索到匹配的日志' : '该级别暂无日志') + '</div>';
  } else {
    h += '<table class="log-table">';
    for (var i = 0; i < filtered.length; i++) {
      var e = filtered[i];
      var lvlClass = "lvl-" + e.level;
      h += '<tr class="log-row" data-key="' + attrEsc(logRowKey(e)) + '">';
      h += '<td class="log-cell log-cell-time">' + esc(e.time) + '</td>';
      h += '<td class="log-cell log-cell-level ' + lvlClass + '">' + esc(e.level) + '</td>';
      h += '<td class="log-cell log-cell-target">' + esc(e.target) + '</td>';
      h += '<td class="log-cell log-cell-msg">' + esc(e.message) + '</td>';
      h += '</tr>';
    }
    h += '</table>';
  }
  var totalCount = Number(total);
  if (!isFinite(totalCount) || totalCount < entries.length) totalCount = entries.length;
  if (searching) {
    var matched = Number(cachedLogMatched);
    if (!isFinite(matched) || matched < entries.length) matched = entries.length;
    var extra = filtered.length < entries.length ? '，当前级别显示 ' + filtered.length + ' 条' : '';
    var truncated = entries.length < matched ? '（只显示最近2000条匹配）' : '';
    h += '<div class="log-summary">搜索「' + esc(logSearchQuery) + '」：当天共 ' + totalCount + ' 条，匹配 ' + matched + ' 条' + extra + truncated + '</div>';
  } else {
    h += '<div class="log-summary">' + (totalCount > 1000
      ? '共 ' + totalCount + ' 条，只显示最近1000条'
      : '共 ' + totalCount + ' 条') + '</div>';
  }
  return h;
}

function toggleLogScroll(enabled) {
  logAutoScroll = !!enabled;
  var bar = document.getElementById("log-filter-bar-wrap");
  if (bar) bar.innerHTML = buildFilterBar(cachedLogEntries);
  if (!logAutoScroll) return;
  var viewer = document.getElementById("log-viewer");
  if (viewer) viewer.scrollTop = viewer.scrollHeight;
}
function applyLogFilter(level) {
  currentLogFilter = level;
  var viewer = document.getElementById("log-viewer");
  var bar = document.getElementById("log-filter-bar-wrap");
  if (bar) bar.innerHTML = buildFilterBar(cachedLogEntries);
  if (viewer) {
    var anchor = captureLogAnchor(viewer);
    viewer.innerHTML = renderLogEntries(cachedLogEntries, level, cachedLogTotal);
    restoreLogAnchor(viewer, anchor, logAutoScroll);
  }
}

async function loadLogContent(filename, silent) {
  var mainEl = document.querySelector(".log-main");
  if (!mainEl) return;
  if (!silent) {
    currentLogFilter = "ALL";
    cachedLogEntries = [];
    cachedLogTotal = 0;
    cachedLogMatched = 0;
    mainEl.innerHTML = '<div id="log-filter-bar-wrap"></div><div id="log-viewer" class="log-viewer">加载中...</div>';
  }
  try {
    await window.AstrBotPluginPage.ready();
    var reqQuery = logSearchQuery;
    var params = {file: filename};
    if (reqQuery) params.q = reqQuery;
    var resp = await window.AstrBotPluginPage.apiGet(EP_LOG_CONTENT, params);
    // 请求期间用户又改了搜索词/切了文件：丢弃这次的结果，避免旧结果覆盖新结果
    if (reqQuery !== logSearchQuery || filename !== currentLogFile) return;
    var entries = [];
    var total = 0;
    var matched = 0;
    var respData = (resp && resp.data) ? resp.data : resp;
    if (resp && Array.isArray(resp.entries)) {
      entries = resp.entries;
      total = Number(resp.total) || entries.length;
      matched = (resp.matched === undefined) ? total : Number(resp.matched);
    } else if (respData && Array.isArray(respData.entries)) {
      entries = respData.entries;
      total = Number(respData.total) || entries.length;
      matched = (respData.matched === undefined) ? total : Number(respData.matched);
    } else if (Array.isArray(resp)) {
      entries = resp;
      total = entries.length;
      matched = total;
    } else if (resp && Array.isArray(resp.data)) {
      entries = resp.data;
      total = Number(resp.total) || entries.length;
      matched = total;
    }
    if (!isFinite(matched) || matched < entries.length) matched = entries.length;
    var first = entries.length ? entries[0] : null;
    var last = entries.length ? entries[entries.length - 1] : null;
    var signature = logSearchQuery + "|" + entries.length + "|" + total + "|" + matched + "|" +
      (first ? String(first.time || "") + "|" + String(first.message || "") : "") + "|" +
      (last ? String(last.time || "") + "|" + String(last.message || "") : "");
    if (silent && signature === cachedLogSignature) return;
    cachedLogSignature = signature;

    var viewer = document.getElementById("log-viewer");
    if (!viewer) return;
    cachedLogEntries = entries;
    cachedLogTotal = total;
    cachedLogMatched = matched;
    updateLogSearchHint();
    var barWrap = document.getElementById("log-filter-bar-wrap");
    if (barWrap) barWrap.innerHTML = buildFilterBar(cachedLogEntries);
    // 内容照常刷新（随时往下拉都能看到新日志），只是不自动滚到底：
    // 用锚点把「正在看的那一行」放回原来的位置，窗口顶部丢行时也不会一行行往下漂
    var anchor = captureLogAnchor(viewer);
    viewer.innerHTML = renderLogEntries(cachedLogEntries, currentLogFilter, cachedLogTotal);
    restoreLogAnchor(viewer, anchor, logAutoScroll);
  } catch(e) {
    if (!silent) {
      var v = document.getElementById("log-viewer");
      if (v) v.textContent = "加载失败: " + (e.message || String(e));
    }
  }
}

async function refreshLogs(btn) {
  btn.classList.add("spinning");
  await loadLogs();
  setTimeout(function(){ btn.classList.remove("spinning"); }, 500);
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1048576).toFixed(1) + " MB";
}

// ── 添加监控弹窗 ──
function openAddModal() {
  document.getElementById("add-nickname").value = "";
  document.getElementById("add-group-id").value = "";
  document.getElementById("add-msg").textContent = "";
  document.getElementById("add-msg").className = "modal-msg";
  document.getElementById("add-submit").disabled = false;
  document.getElementById("add-submit").textContent = "搜索并添加";
  document.getElementById("add-modal").classList.add("open");
  setTimeout(function(){ document.getElementById("add-nickname").focus(); }, 100);
}
function closeAddModal() {
  document.getElementById("add-modal").classList.remove("open");
}
async function submitAdd() {
  var nick = document.getElementById("add-nickname").value.trim();
  var gid = document.getElementById("add-group-id").value.trim();
  var msgEl = document.getElementById("add-msg");
  var btn = document.getElementById("add-submit");
  if (!nick) { msgEl.textContent = "请输入玩家昵称"; msgEl.className = "modal-msg error"; return; }
  btn.disabled = true;
  btn.textContent = "搜索中...";
  msgEl.textContent = "";
  msgEl.className = "modal-msg";
  try {
    await window.AstrBotPluginPage.ready();
    var resp = await window.AstrBotPluginPage.apiPost("api/add", { nickname: nick, group_id: gid });
    msgEl.textContent = "✅ " + (resp && resp.message ? resp.message : "已添加 " + nick);
    msgEl.className = "modal-msg success";
    btn.textContent = "已添加";
    setTimeout(function(){ closeAddModal(); loadData(); }, 800);
  } catch(e) {
    msgEl.textContent = "❌ " + (e.message || String(e));
    msgEl.className = "modal-msg error";
    btn.disabled = false;
    btn.textContent = "搜索并添加";
  }
}
// Close modal on overlay click
document.getElementById("add-modal").addEventListener("click", function(e) {
  if (e.target === this) closeAddModal();
});
// Enter key submits
document.getElementById("add-group-id").addEventListener("keydown", function(e) {
  if (e.key === "Enter") submitAdd();
});
document.getElementById("add-nickname").addEventListener("keydown", function(e) {
  if (e.key === "Enter") document.getElementById("add-group-id").focus();
});

// ── 初始化 ──
// Init - wait for AstrBotPluginPage bridge then load
(function initWhenReady() {
  if (!window.AstrBotPluginPage || typeof window.AstrBotPluginPage.ready !== "function") {
    setTimeout(initWhenReady, 200);
    return;
  }
  window.AstrBotPluginPage.ready().then(function() {
    loadData();
    loadConfig();
  }).catch(function() {
    setTimeout(initWhenReady, 500);
  });
})();