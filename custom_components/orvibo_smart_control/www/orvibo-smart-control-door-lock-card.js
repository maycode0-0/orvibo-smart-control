/**
 * ORVIBO 门锁卡片（纯原生 JS，无第三方依赖）。
 *
 * 功能：
 *  - 门锁状态总览（锁状态/门磁/电池/最近截图）
 *  - 下发临时密码（type/minutes/number/phone/name，可选短信）
 *  - 临时密码列表管理（查看/删除/过期状态）
 *
 * 用法（Lovelace 卡片）：
 *   type: custom:orvibo-smart-control-door-lock-card
 *   device_id: "<your-door-lock-device-id>"   # 可选，留空自动选第一把门锁
 *
 * 仅显示临时密码管理：
 *   type: custom:orvibo-smart-control-temp-password-card
 *   device_id: "<your-door-lock-device-id>"
 */

const ORVIBO_PREFIX = "orvibo_smart_control_";
const CARD_VERSION = "0.2.0";

class OrviboSmartControlDoorLockCard extends HTMLElement {
  constructor() {
    super();
    this._config = {};
    this._hass = null;
    this._entities = {};   // device_id -> {kind -> entity_id}
    this._deviceName = "";
    this._tempResult = "";
    this._tempError = "";
    this._notice = "";
    this._noticeKind = "";
    this._granting = false;
    this._revokingId = null;
    this._listLoading = false;
    this._listError = "";
    this._records = null;
    this._draft = {
      type: "2",
      minutes: "1440",
      number: "1",
      phone: "",
      name: "",
    };
    this._entitiesLoaded = false;
    this._listOpen = false;
    this._grantOpen = false;
    this._lastListAt = 0;
    this._listLoaded = false;
    this._events = [];
    this._eventsLoaded = false;
    this._eventsLoading = false;
    this._eventsLastCam = "";
    this._lastReloadAt = 0;
    this._lightboxUrl = null;
    this.attachShadow({ mode: "open" });
  }

  static LIST_THROTTLE_MS = 60000;

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = config || {};
    this._deviceId = "";
    this._entities = {};
    this._entitiesLoaded = false;
    this._records = null;
    this._lastListAt = 0;
    this._listLoaded = false;
    this._loadEntities();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._entitiesLoaded) {
      this._loadEntities();
    } else {
      if (
        (!this._entities.door || !this._entities.lock_state) &&
        Date.now() - this._lastReloadAt > 60000
      ) {
        this._lastReloadAt = Date.now();
        this._entitiesLoaded = false;
        this._loadEntities();
      }
      this._render();
    }
  }

  getCardSize() {
    return this._config.temp_password_only ? 5 : 6;
  }

  async _loadEntities() {
    if (!this._hass) return;
    try {
      const entities = await this._hass.callWS({ type: "config/entity_registry/list" });
      const devices = await this._hass.callWS({ type: "config/device_registry/list" });
      const lockDevices = new Map();
      for (const dev of devices) {
        const identifier = (dev.identifiers || []).find(
          (value) =>
            Array.isArray(value) &&
            value[0] === "orvibo_smart_control" &&
            value[1]
        );
        if (!identifier) continue;
        lockDevices.set(dev.id, {
          name: dev.name_by_user || dev.name || "门锁",
          deviceId: String(identifier[1]),
        });
      }
      const byDevice = {};
      for (const ent of entities) {
        if (!ent.unique_id || !ent.unique_id.startsWith(ORVIBO_PREFIX)) continue;
        if (!ent.device_id) continue;
        const dev = lockDevices.get(ent.device_id);
        if (!dev) continue;
        const uid = ent.unique_id;
        let kind = "unknown";
        // door_lock_doorbell 含 door_lock_door 子串，必须优先匹配
        if (uid.includes("door_lock_doorbell")) kind = "doorbell";
        else if (uid.includes("door_lock_state")) kind = "lock_state";
        else if (uid.includes("door_lock_door")) kind = "door";
        else if (uid.includes("dry_battery")) kind = "dry_battery";
        else if (uid.includes("lithium_battery")) kind = "lithium_battery";
        else if (uid.includes("door_lock_unlock")) kind = "unlock";
        else if (uid.includes("temp_password")) kind = "temp_password";
        else if (uid.includes("camera")) kind = "camera";
        byDevice[dev.deviceId] = byDevice[dev.deviceId] || { name: dev.name, entities: {} };
        byDevice[dev.deviceId].entities[kind] = ent.entity_id;
      }
      for (const [deviceId, item] of Object.entries(byDevice)) {
        if (
          !item.entities.lock_state &&
          !item.entities.door &&
          !item.entities.unlock
        ) {
          delete byDevice[deviceId];
        }
      }
      const requested = this._config.device_id;
      const candidates = Object.keys(byDevice);
      let deviceId = requested && byDevice[requested] ? requested : "";
      if (!deviceId) {
        // 多设备匹配时选择实体最全的门锁（锁状态+门磁+截图都在的优先）
        let bestScore = -1;
        for (const id of candidates) {
          const ents = byDevice[id].entities;
          const score =
            (ents.lock_state ? 2 : 0) +
            (ents.door ? 2 : 0) +
            (ents.camera ? 1 : 0) +
            Object.keys(ents).length;
          if (score > bestScore) {
            bestScore = score;
            deviceId = id;
          }
        }
      }
      if (deviceId && byDevice[deviceId]) {
        this._deviceId = deviceId;
        this._deviceName = byDevice[deviceId].name;
        this._entities = byDevice[deviceId].entities;
        this._entitiesLoaded = true;
      }
      this._entitiesLoaded = true;
    } catch (e) {
      console.error("ORVIBO card: 加载实体失败", e);
    }
    this._render();
  }

  _state(entityId) {
    if (!entityId || !this._hass) return null;
    const st = this._hass.states[entityId];
    return st ? st.state : null;
  }

  _attr(entityId, key) {
    if (!entityId || !this._hass) return null;
    const st = this._hass.states[entityId];
    return st && st.attributes ? st.attributes[key] : null;
  }

  _fmtTs(ts) {
    if (!ts) return "-";
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return "-";
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }

  _recordsMarkup() {
    if (this._listLoading && this._records === null) {
      return "<div class='empty'>正在加载授权...</div>";
    }
    if (this._listError) {
      return `<div class="message error">${this._escapeHtml(this._listError)}</div>`;
    }
    if (!Array.isArray(this._records) || !this._records.length) {
      return "<div class='empty'>暂无临时密码授权</div>";
    }
    return this._records
      .map((record) => {
        const authorizedId = Number(record.authorized_id);
        const name = record.name || "临时用户";
        const validity = record.end_time
          ? `有效至 ${this._fmtTs(record.end_time)}`
          : record.start_time
            ? `生效于 ${this._fmtTs(record.start_time)}`
            : "长期有效";
        const usage = Number(record.number)
          ? `已用 ${Number(record.unlock_num) || 0} / ${Number(record.number)} 次`
          : "不限次数";
        return `
          <div class="tp-item ${record.expired ? "expired" : ""}">
            <div class="tp-content">
              <div class="tp-heading">
                <span>${this._escapeHtml(name)}</span>
                <span class="status ${record.expired ? "expired" : "active"}">${record.expired ? "已过期" : "有效"}</span>
              </div>
              <div class="meta">授权 #${this._escapeHtml(authorizedId)} · ${this._escapeHtml(validity)}</div>
              <div class="meta">${this._escapeHtml(usage)}</div>
            </div>
            <button class="icon-button danger" data-aid="${this._escapeHtml(authorizedId)}" title="撤销授权" aria-label="撤销授权" ${this._revokingId === authorizedId ? "disabled" : ""}>
              <ha-icon icon="mdi:delete-outline"></ha-icon>
            </button>
          </div>`;
      })
      .join("");
  }

  _render() {
    const root = this.shadowRoot;
    if (!this._hass) {
      root.innerHTML = "<ha-card style='padding:16px'>ORVIBO 门锁卡片</ha-card>";
      return;
    }
    if (!this._deviceId) {
      root.innerHTML = "<ha-card style='padding:16px'>未找到门锁设备，请配置 device_id 或确认集成已加载</ha-card>";
      return;
    }
    const e = this._entities;
    const lockState = this._state(e.lock_state) || "-";
    const door = this._state(e.door);
    const dryBattery = this._state(e.dry_battery);
    const lithiumBattery = this._state(e.lithium_battery);
    const tempOnly = this._config.temp_password_only === true;
    const lockStateLabel = { locked: "已上锁", unlocked: "未上锁", inside_locked: "门内已反锁", abnormal: "异常" }[lockState] || lockState;
    const doorLabel = door === "on" ? "开" : door === "off" ? "关" : (door || "-");
    const batteryLabel =
      dryBattery != null && dryBattery !== "unknown"
        ? `${dryBattery}%`
        : "-";
    const lithiumLabel =
      lithiumBattery != null && lithiumBattery !== "unknown"
        ? `${lithiumBattery}%`
        : "-";
    const cameraEntity = e.camera;
    const cameraState = cameraEntity ? this._hass.states[cameraEntity] : null;
    const camKey = cameraState
      ? `${cameraState.state}|${cameraState.last_updated}`
      : "";
    if (!tempOnly && !this._eventsLoaded) {
      this._loadEvents();
    } else if (!tempOnly && camKey && camKey !== this._eventsLastCam) {
      this._loadEvents();
    }
    this._eventsLastCam = camKey;

    root.innerHTML = `
      <ha-card>
        <div class="header">
          <div>
            <div class="title"><ha-icon icon="${tempOnly ? "mdi:key-chain" : "mdi:lock"}"></ha-icon><span>${this._escapeHtml(tempOnly ? "临时密码管理" : this._deviceName)}</span></div>
            <div class="subtitle">${this._escapeHtml(this._deviceName)} · ${tempOnly ? "访客授权" : "状态总览"}</div>
          </div>
          ${tempOnly ? "" : `<div class="lock-badge ${this._escapeHtml(lockState)}">${this._escapeHtml(lockStateLabel)}</div>`}
        </div>
        ${tempOnly ? "" : `<div class="stats">
          <div class="stat"><span class="stat-label">门磁</span><span class="stat-value">${this._escapeHtml(doorLabel)}</span></div>
          <div class="stat"><span class="stat-label">干电池</span><span class="stat-value">${this._escapeHtml(batteryLabel)}</span></div>
          <div class="stat"><span class="stat-label">锂电池</span><span class="stat-value">${this._escapeHtml(lithiumLabel)}</span></div>
        </div>`}
        ${
          !tempOnly && this._events.length
            ? `<div class="events">${this._events
                .slice(0, 8)
                .map(
                  (ev, idx) => `
            <div class="ev-item" data-idx="${idx}">
              <img src="${ev.url}" alt="${this._eventLabel(ev.kind)}" loading="lazy" />
              <div class="ev-label">${this._eventLabel(ev.kind)}</div>
              <div class="ev-time">${this._fmtTs(ev.time)}</div>
            </div>`
                )
                .join("")}</div>`
            : ""
        }
        ${
          !tempOnly && this._lightboxUrl
            ? `<div class="lightbox" id="ov-lightbox"><img src="${this._lightboxUrl}" alt="事件大图" /></div>`
            : ""
        }
        <div class="section ${tempOnly ? "first" : ""}">
          <div class="section-title toggle" id="ov-grant-toggle">
            <span class="section-label"><ha-icon icon="mdi:key-plus"></ha-icon>生成临时密码</span>
            <ha-icon icon="${this._grantOpen ? "mdi:chevron-up" : "mdi:chevron-down"}"></ha-icon>
          </div>
          <div id="ov-grant-form" class="form" style="${this._grantOpen ? "" : "display:none"}">
            <label>类型
              <select id="ov-tp-type">
                <option value="2" ${this._draft.type === "2" ? "selected" : ""}>临时密码</option>
                <option value="1" ${this._draft.type === "1" ? "selected" : ""}>限时密码</option>
              </select>
            </label>
            <label>时长（分钟）
              <input id="ov-tp-minutes" type="number" value="${this._escapeHtml(this._draft.minutes)}" min="1" max="525600" inputmode="numeric" />
            </label>
            <label>次数
              <input id="ov-tp-number" type="number" value="${this._escapeHtml(this._draft.number)}" min="0" max="100" inputmode="numeric" />
            </label>
            <label>手机号
              <input id="ov-tp-phone" type="tel" value="${this._escapeHtml(this._draft.phone)}" placeholder="可选，用于短信通知" autocomplete="tel" />
            </label>
            <label>名称
              <input id="ov-tp-name" type="text" value="${this._escapeHtml(this._draft.name)}" placeholder="例如：保洁、访客" maxlength="64" autocomplete="off" />
            </label>
            <div class="actions">
              <ha-button raised id="ov-tp-grant" ${this._granting ? "disabled" : ""}>${this._granting ? "正在生成..." : "生成临时密码"}</ha-button>
            </div>
            ${this._tempError ? `<div class="error">${this._escapeHtml(this._tempError)}</div>` : ""}
            ${this._tempResult ? `<div class="result" role="status" aria-live="polite"><div><span>本次临时密码</span><strong>${this._escapeHtml(this._tempResult)}</strong><small>密码只在本次创建后显示，请妥善保存</small></div><button class="icon-button" id="ov-tp-copy" title="复制密码" aria-label="复制密码"><ha-icon icon="mdi:content-copy"></ha-icon></button></div>` : ""}
          </div>
        </div>
        <div class="section">
          <div class="section-title toggle" id="ov-tp-toggle">
            <span class="section-label"><ha-icon icon="mdi:key-chain-variant"></ha-icon>授权列表</span>
            <span class="section-actions">
              <button class="icon-button" id="ov-tp-refresh" title="刷新授权" aria-label="刷新授权" ${this._listLoading ? "disabled" : ""}><ha-icon icon="mdi:refresh"></ha-icon></button>
              <ha-icon icon="${this._listOpen ? "mdi:chevron-up" : "mdi:chevron-down"}"></ha-icon>
            </span>
          </div>
          <div id="ov-tp-list" class="tp-list" style="${this._listOpen ? "" : "display:none"}">
            ${this._listOpen ? this._recordsMarkup() : ""}
          </div>
        </div>
        ${this._notice ? `<div class="message ${this._noticeKind === "error" ? "error" : "success"}" role="status" aria-live="polite">${this._escapeHtml(this._notice)}</div>` : ""}
        <div class="ver">${tempOnly ? "orvibo-smart-control-temp-password-card" : "orvibo-smart-control-door-lock-card"} v${CARD_VERSION}</div>
      </ha-card>
      <style>
        ha-card { padding: 16px; font-size: 14px; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .title { display: flex; align-items: center; gap: 8px; font-size: 18px; font-weight: 600; }
        .title ha-icon { color: var(--primary-color); }
        .subtitle { color: var(--secondary-text-color); font-size: 12px; }
        .lock-badge { padding: 4px 10px; border-radius: 12px; font-size: 12px; background: var(--primary-color); color: var(--text-primary-color); }
        .lock-badge.unlocked { background: #43a047; }
        .lock-badge.abnormal { background: #e53935; }
        .stats { display: flex; gap: 12px; margin-bottom: 10px; flex-wrap: wrap; }
        .stat { background: var(--card-background-color, #f5f5f5); border-radius: 8px; padding: 8px 12px; flex: 1; min-width: 90px; }
        .stat-label { display: block; color: var(--secondary-text-color); font-size: 11px; }
        .stat-value { font-weight: 600; }
        .events { display: flex; gap: 8px; overflow-x: auto; margin-bottom: 10px; padding-bottom: 4px; }
        .ev-item { flex: 0 0 auto; width: 76px; cursor: pointer; text-align: center; }
        .ev-item img { width: 76px; height: 76px; object-fit: cover; border-radius: 6px; display: block; }
        .ev-label { font-size: 11px; color: var(--secondary-text-color); margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .ev-time { font-size: 10px; color: var(--secondary-text-color); opacity: 0.8; }
        .lightbox { position: fixed; inset: 0; background: rgba(0,0,0,0.82); display: flex; align-items: center; justify-content: center; z-index: 9999; cursor: zoom-out; }
        .lightbox img { max-width: 92vw; max-height: 92vh; border-radius: 8px; }
        .ver { margin-top: 8px; font-size: 10px; color: var(--secondary-text-color); opacity: 0.6; text-align: right; }
        .section { border-top: 1px solid var(--divider-color); padding-top: 12px; margin-top: 12px; }
        .section.first { border-top: 0; margin-top: 0; }
        .section-title { min-height: 36px; font-weight: 600; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; }
        .section-label, .section-actions { display: flex; align-items: center; gap: 8px; }
        .section-label ha-icon { color: var(--primary-color); }
        .toggle { cursor: pointer; user-select: none; }
        .form { display: grid; gap: 8px; }
        .form label { display: grid; grid-template-columns: 110px 1fr; align-items: center; gap: 8px; }
        .form input, .form select { min-height: 38px; width: 100%; box-sizing: border-box; border: 1px solid var(--divider-color); border-radius: 4px; padding: 7px 10px; color: var(--primary-text-color); background: var(--card-background-color); font: inherit; }
        .actions { margin-top: 8px; }
        .error { color: var(--error-color, #db4437); margin-top: 8px; }
        .result { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 8px; padding: 12px; border-left: 3px solid var(--primary-color); background: color-mix(in srgb, var(--primary-color) 8%, var(--card-background-color)); }
        .result span, .result small { display: block; color: var(--secondary-text-color); font-size: 12px; }
        .result strong { display: block; margin: 3px 0; font-size: 24px; letter-spacing: 0; font-variant-numeric: tabular-nums; }
        .tp-list { display: grid; gap: 8px; }
        .tp-item { display: flex; justify-content: space-between; align-items: center; gap: 12px; min-height: 58px; border: 1px solid var(--divider-color); border-radius: 6px; padding: 10px 12px; }
        .tp-content { min-width: 0; }
        .tp-heading { display: flex; align-items: center; gap: 8px; font-weight: 600; }
        .status { padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 500; }
        .status.active { color: var(--success-color, #2e7d32); background: color-mix(in srgb, var(--success-color, #2e7d32) 12%, var(--card-background-color)); }
        .status.expired { color: var(--secondary-text-color); background: var(--divider-color); }
        .meta { color: var(--secondary-text-color); font-size: 12px; line-height: 1.45; overflow-wrap: anywhere; }
        .tp-item.expired { opacity: 0.7; }
        .empty { padding: 18px 0; color: var(--secondary-text-color); text-align: center; }
        .icon-button { display: inline-flex; align-items: center; justify-content: center; width: 36px; height: 36px; flex: 0 0 36px; padding: 0; border: 0; border-radius: 50%; color: var(--primary-text-color); background: transparent; cursor: pointer; }
        .icon-button:hover { background: var(--secondary-background-color); }
        .icon-button.danger { color: var(--error-color, #db4437); }
        .icon-button:disabled { cursor: wait; opacity: 0.45; }
        .message { margin-top: 12px; padding: 9px 10px; border-radius: 4px; }
        .message.success { color: var(--success-color, #2e7d32); background: color-mix(in srgb, var(--success-color, #2e7d32) 10%, var(--card-background-color)); }
        .message.error { color: var(--error-color, #db4437); background: color-mix(in srgb, var(--error-color, #db4437) 8%, var(--card-background-color)); }
        @media (max-width: 480px) {
          .form label { grid-template-columns: 1fr; gap: 4px; }
          .stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
          .stat { min-width: 0; padding: 8px; }
        }
      </style>
    `;

    root.querySelector("#ov-tp-grant").addEventListener("click", () => this._grant());
    const draftInputs = {
      "#ov-tp-type": "type",
      "#ov-tp-minutes": "minutes",
      "#ov-tp-number": "number",
      "#ov-tp-phone": "phone",
      "#ov-tp-name": "name",
    };
    for (const [selector, key] of Object.entries(draftInputs)) {
      root.querySelector(selector).addEventListener("input", (event) => {
        this._draft[key] = event.target.value;
      });
    }
    root.querySelector("#ov-grant-toggle").addEventListener("click", () => {
      this._grantOpen = !this._grantOpen;
      this._render();
    });
    root.querySelector("#ov-tp-toggle").addEventListener("click", (e) => {
      if (e.target.closest("#ov-tp-refresh")) return;
      this._listOpen = !this._listOpen;
      this._render();
    });
    root.querySelector("#ov-tp-refresh").addEventListener("click", () => {
      this._lastListAt = 0;
      this._listLoaded = false;
      this._loadList();
    });
    root.querySelectorAll("button[data-aid]").forEach((button) => {
      button.addEventListener("click", () => {
        this._revoke(Number(button.getAttribute("data-aid")));
      });
    });
    const copyButton = root.querySelector("#ov-tp-copy");
    if (copyButton) {
      copyButton.addEventListener("click", () => this._copyPassword());
    }
    root.querySelectorAll(".ev-item").forEach((el) => {
      el.addEventListener("click", () => {
        const idx = Number(el.getAttribute("data-idx"));
        const ev = this._events[idx];
        if (ev) {
          this._lightboxUrl = ev.url;
          this._render();
        }
      });
    });
    const lightboxEl = root.querySelector("#ov-lightbox");
    if (lightboxEl) {
      lightboxEl.addEventListener("click", () => {
        this._lightboxUrl = null;
        this._render();
      });
    }
    if (this._listOpen) {
      this._loadList();
    }
  }

  async _grant() {
    if (this._granting) return;
    this._tempError = "";
    this._tempResult = "";
    this._notice = "";
    const minutes = Number(this._draft.minutes);
    const number = Number(this._draft.number);
    if (!Number.isInteger(minutes) || minutes < 1 || minutes > 525600) {
      this._tempError = "时长必须是 1 到 525600 之间的整数";
      this._render();
      return;
    }
    if (!Number.isInteger(number) || number < 0 || number > 100) {
      this._tempError = "次数必须是 0 到 100 之间的整数";
      this._render();
      return;
    }
    const data = {
      device_id: this._deviceId,
      type: Number(this._draft.type),
      minutes,
      number,
      phone: this._draft.phone.trim(),
      name: this._draft.name.trim(),
    };
    this._granting = true;
    this._render();
    try {
      const result = await this._hass.callWS({
        type: "call_service",
        domain: "orvibo_smart_control",
        service: "grant_temp_password",
        service_data: data,
        return_response: true,
      });
      const res = result && (result.response || result);
      if (res && res.error) {
        this._tempError = res.error;
      } else if (res && res.password) {
        this._tempResult = String(res.password);
        this._listOpen = true;
      } else {
        this._tempError = "门锁未返回临时密码，请稍后重试";
      }
    } catch (e) {
      this._tempError = e.message || String(e);
    }
    this._granting = false;
    this._lastListAt = 0;
    this._listLoaded = false;
    this._records = null;
    this._render();
    if (this._listOpen) {
      this._loadList();
    }
  }

  async _loadList() {
    if (!this._hass || !this._deviceId || this._listLoading) return;
    const now = Date.now();
    if (
      this._listLoaded &&
      now - this._lastListAt < OrviboSmartControlDoorLockCard.LIST_THROTTLE_MS
    ) {
      return; // 节流：60 秒内不重复拉取
    }
    this._listLoading = true;
    this._listError = "";
    this._render();
    try {
      const result = await this._hass.callWS({
        type: "call_service",
        domain: "orvibo_smart_control",
        service: "list_temp_passwords",
        service_data: { device_id: this._deviceId },
        return_response: true,
      });
      const res = result && (result.response || result);
      if (res && res.error) throw new Error(res.error);
      const records = (res && res[this._deviceId]) || [];
      if (!Array.isArray(records)) throw new Error("授权列表响应格式无效");
      this._records = records;
      this._listLoaded = true;
      this._lastListAt = Date.now();
    } catch (e) {
      this._listError = e.message || String(e);
      this._records = null;
    } finally {
      this._listLoading = false;
      this._render();
    }
  }

  async _revoke(authorizedId) {
    if (!Number.isInteger(authorizedId) || authorizedId <= 0 || this._revokingId) return;
    const record = Array.isArray(this._records)
      ? this._records.find((item) => Number(item.authorized_id) === authorizedId)
      : null;
    const label = record && record.name ? `“${record.name}”` : `#${authorizedId}`;
    if (!window.confirm(`确定撤销临时密码授权 ${label} 吗？撤销后将立即失效。`)) return;
    this._revokingId = authorizedId;
    this._notice = "";
    this._render();
    try {
      const result = await this._hass.callWS({
        type: "call_service",
        domain: "orvibo_smart_control",
        service: "revoke_temp_password",
        service_data: {
          device_id: this._deviceId,
          authorized_id: authorizedId,
        },
        return_response: true,
      });
      const res = result && (result.response || result);
      if (res && res.error) throw new Error(res.error);
      this._records = Array.isArray(this._records)
        ? this._records.filter((item) => Number(item.authorized_id) !== authorizedId)
        : this._records;
      this._notice = "临时密码授权已撤销";
      this._noticeKind = "success";
    } catch (e) {
      this._notice = e.message || String(e);
      this._noticeKind = "error";
    }
    this._revokingId = null;
    this._lastListAt = Date.now();
    this._listLoaded = true;
    this._render();
  }

  async _copyPassword() {
    if (!this._tempResult) return;
    try {
      await navigator.clipboard.writeText(this._tempResult);
      this._notice = "临时密码已复制";
      this._noticeKind = "success";
    } catch (e) {
      this._notice = "无法复制，请手动记录临时密码";
      this._noticeKind = "error";
    }
    this._render();
  }

  _escapeHtml(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  _eventLabel(kind) {
    const labels = {
      ring: "有人来访",
      visit: "有人来访",
      loiter: "逗留",
      picklock: "异常开门",
      message: "消息",
    };
    return labels[kind] || kind || "事件";
  }

  async _loadEvents() {
    if (!this._hass || !this._deviceId || this._eventsLoading) return;
    this._eventsLoading = true;
    try {
      const result = await this._hass.callWS({
        type: "call_service",
        domain: "orvibo_smart_control",
        service: "list_events",
        service_data: { device_id: this._deviceId, limit: 12 },
        return_response: true,
      });
      const res = result && result.response;
      const events = (res && res.events) || [];
      const images = [];
      for (const ev of events) {
        if (ev.type !== "image") continue;
        let url = "";
        try {
          const resolved = await this._hass.callWS({
            type: "media_source/resolve_media",
            media_id: ev.media_id,
          });
          url = resolved && resolved.url;
        } catch (e) {
          console.warn("ORVIBO card: 解析事件图片失败", ev.media_id, e);
        }
        if (url) {
          images.push({ kind: ev.kind, time: ev.time, url });
        }
      }
      this._events = images;
      this._eventsLoaded = true;
    } catch (e) {
      console.error("ORVIBO card: 加载事件历史失败", e);
      this._eventsLoaded = true;
    }
    this._eventsLoading = false;
    this._render();
  }
}

class OrviboSmartControlTempPasswordCard extends OrviboSmartControlDoorLockCard {
  constructor() {
    super();
    this._grantOpen = true;
    this._listOpen = true;
  }

  setConfig(config) {
    super.setConfig({ ...(config || {}), temp_password_only: true });
  }
}

if (!customElements.get("orvibo-smart-control-door-lock-card")) {
  customElements.define("orvibo-smart-control-door-lock-card", OrviboSmartControlDoorLockCard);
} else {
  console.warn("orvibo-smart-control-door-lock-card 已定义，跳过重复注册");
}
if (!customElements.get("orvibo-smart-control-temp-password-card")) {
  customElements.define(
    "orvibo-smart-control-temp-password-card",
    OrviboSmartControlTempPasswordCard
  );
}
window.customCards = window.customCards || [];
for (const card of [
  {
    type: "orvibo-smart-control-door-lock-card",
    name: "ORVIBO 门锁",
    description: "门锁状态 + 临时密码下发与管理",
    preview: false,
  },
  {
    type: "orvibo-smart-control-temp-password-card",
    name: "ORVIBO 临时密码管理",
    description: "创建、查看和撤销门锁临时密码授权",
    preview: false,
  },
]) {
  if (!window.customCards.some((item) => item.type === card.type)) {
    window.customCards.push(card);
  }
}
