/*
 * Danfoss Link living zone card.
 *
 * Shows the rooms in the living zone (they share one Schedule/Manual setting
 * and one weekly schedule) with their temperatures, switches the zone between
 * schedule and manual, and moves rooms into or out of the zone.
 *
 * Reads the "Living zone" sensor (attributes entity_id, other_entity_id,
 * hvac_mode, schedule_today) and the rooms' climate entities, and uses the
 * danfoss_link.join_living_zone / leave_living_zone actions.
 *
 * Served and loaded by the integration itself; no separate install.
 */

const ZONE_TEXT = {
  en: {
    title: "Living zone",
    schedule: "Schedule",
    manual: "Manual",
    today: "Today at home",
    away_all_day: "Today away all day",
    rooms: "Rooms in the zone",
    others: "Other rooms",
    add: "Add",
    remove: "Remove",
    confirm_remove: "Remove?",
    no_rooms: "No rooms in the living zone.",
    no_others: "All rooms are in the living zone.",
    heating: "Heating",
    working: "Please wait…",
    error: "Could not save",
    no_entity: "No Danfoss Link living zone sensor found. Choose it in the card settings.",
    last_room: "The living zone needs at least one room.",
  },
  da: {
    title: "Living Zone",
    schedule: "Ugeprogram",
    manual: "Manuel",
    today: "Hjemme i dag",
    away_all_day: "Ude hele dagen i dag",
    rooms: "Rum i zonen",
    others: "Andre rum",
    add: "Tilføj",
    remove: "Fjern",
    confirm_remove: "Fjern?",
    no_rooms: "Ingen rum i Living Zone.",
    no_others: "Alle rum er i Living Zone.",
    heating: "Varmer",
    working: "Vent venligst…",
    error: "Kunne ikke gemme",
    no_entity: "Fandt ingen Danfoss Link Living Zone-sensor. Vælg den i kortets indstillinger.",
    last_room: "Living Zone skal have mindst ét rum.",
  },
};

const escapeHtml = (text) =>
  String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function makeLivingZoneCard() {
  return class DanfossLinkLivingZoneCard extends HTMLElement {
    static getConfigForm() {
      return {
        schema: [
          {
            name: "entity",
            selector: { entity: { filter: { domain: "sensor", integration: "danfoss_link" } } },
          },
          { name: "title", selector: { text: {} } },
          {
            type: "grid",
            name: "",
            schema: [
              { name: "show_others", selector: { boolean: {} } },
              { name: "editable", selector: { boolean: {} } },
            ],
          },
        ],
        computeLabel: (schema) =>
          ({
            entity: "Living zone sensor",
            title: "Title",
            show_others: "Show other rooms",
            editable: "Allow adding and removing rooms",
          })[schema.name],
      };
    }

    static getStubConfig() {
      // No entity: the (first) Danfoss Link living zone sensor.
      return { show_others: true, editable: true };
    }

    setConfig(config) {
      this._config = config || {};
      this._confirm = null;
      this._render();
    }

    set hass(hass) {
      const old = this._hass;
      this._hass = hass;
      if (!this._root || !old || old.language !== hass.language || this._watched().some((id) => old.states[id] !== hass.states[id])) {
        this._render();
      }
    }

    getCardSize() {
      return 5;
    }

    getGridOptions() {
      return { columns: 6, min_columns: 4, rows: "auto" };
    }

    _t(key) {
      const lang = (this._hass && this._hass.language) || "en";
      return (ZONE_TEXT[lang.split("-")[0]] || ZONE_TEXT.en)[key];
    }

    _flag(name) {
      return this._config[name] !== false;
    }

    _entityId() {
      if (this._config && this._config.entity) return this._config.entity;
      if (!this._hass) return undefined;
      const fromRegistry = Object.values(this._hass.entities || {}).find(
        (e) => e.platform === "danfoss_link" && e.translation_key === "living_zone" && e.entity_id.startsWith("sensor.")
      );
      if (fromRegistry) return fromRegistry.entity_id;
      return Object.keys(this._hass.states).find(
        (id) => id.startsWith("sensor.") && "other_entity_id" in this._hass.states[id].attributes
      );
    }

    // The sensor and every room's climate entity: re-render when one changes.
    _watched() {
      const id = this._entityId();
      const state = id && this._hass && this._hass.states[id];
      if (!state) return [id];
      const a = state.attributes;
      return [id, ...(a.entity_id || []), ...(a.other_entity_id || [])];
    }

    _temp(value) {
      if (value === undefined || value === null) return "–";
      const unit =
        (this._hass.config && this._hass.config.unit_system && this._hass.config.unit_system.temperature) || "°C";
      return `${Number(value).toLocaleString(this._hass.language, { maximumFractionDigits: 1 })} ${unit}`;
    }

    _name(id) {
      const state = this._hass.states[id];
      const device = this._hass.entities && this._hass.entities[id] && this._hass.entities[id].device_id;
      const deviceName = device && this._hass.devices && this._hass.devices[device] && this._hass.devices[device].name;
      return deviceName || (state && state.attributes.friendly_name) || id;
    }

    _render() {
      if (!this._config || !this._hass) return;
      if (!this._root) this._root = this.attachShadow({ mode: "open" });
      const id = this._entityId();
      const state = id && this._hass.states[id];
      const busy = this._busy ? "disabled" : "";
      let body;
      if (!state) {
        body = `<div class="note">${this._t("no_entity")}</div>`;
      } else {
        const a = state.attributes;
        const members = (a.entity_id || []).filter(Boolean);
        const others = (a.other_entity_id || []).filter(Boolean);
        body = `
          ${members.length ? this._modeHtml(a, busy) : ""}
          <div class="label">${this._t("rooms")}</div>
          ${members.length ? members.map((m) => this._roomHtml(m, true, members.length, busy)).join("") : `<div class="note">${this._t("no_rooms")}</div>`}
          ${this._flag("show_others") ? `
            <div class="label others">${this._t("others")}</div>
            ${others.length ? others.map((m) => this._roomHtml(m, false, members.length, busy)).join("") : `<div class="note">${this._t("no_others")}</div>`}` : ""}`;
      }
      body += `${this._busy ? `<div class="note">${this._t("working")}</div>` : ""}
        ${this._error ? `<div class="error">${this._t("error")}: ${escapeHtml(this._error)}</div>` : ""}`;
      this._root.innerHTML = `
        <style>${ZONE_STYLE}</style>
        <ha-card>
          <div class="header">
            <span>${escapeHtml(this._config.title || this._t("title"))}</span>
          </div>
          <div class="content">${body}</div>
        </ha-card>`;
      this._bind();
    }

    _modeHtml(a, busy) {
      const mode = a.hvac_mode;
      const today = a.schedule_today;
      let line = "";
      if (mode === "auto" && Array.isArray(today)) {
        line = today.length ? `${this._t("today")}: ${today.map(escapeHtml).join(", ")}` : this._t("away_all_day");
      }
      return `<div class="mode">
        <div class="toggle">
          <button class="set-mode ${mode === "auto" ? "on" : ""}" data-mode="auto" ${busy}>
            <ha-icon icon="mdi:calendar-clock"></ha-icon>${this._t("schedule")}</button>
          <button class="set-mode ${mode === "heat" ? "on" : ""}" data-mode="heat" ${busy}>
            <ha-icon icon="mdi:hand-back-right"></ha-icon>${this._t("manual")}</button>
        </div>
        ${line ? `<div class="sub">${line}</div>` : ""}
      </div>`;
    }

    _roomHtml(entityId, member, memberCount, busy) {
      const state = this._hass.states[entityId];
      const a = (state && state.attributes) || {};
      const heating = a.hvac_action === "heating";
      const temps = `${this._temp(a.current_temperature)} <span class="target">→ ${this._temp(a.temperature)}</span>`;
      let action = "";
      if (this._flag("editable")) {
        if (member) {
          const last = memberCount <= 1;
          const confirming = this._confirm === entityId;
          action = `<button class="small leave ${confirming ? "danger" : ""}" data-entity="${entityId}" ${busy} ${last ? `disabled title="${this._t("last_room")}"` : ""}>
            ${confirming ? this._t("confirm_remove") : this._t("remove")}</button>`;
        } else {
          action = `<button class="small join" data-entity="${entityId}" ${busy}>${this._t("add")}</button>`;
        }
      }
      return `<div class="room ${member ? "member" : ""}">
        <ha-icon class="${heating ? "heating" : ""}" icon="${heating ? "mdi:radiator" : "mdi:radiator-disabled"}"
          title="${heating ? this._t("heating") : ""}"></ha-icon>
        <div class="name" data-entity="${entityId}">${escapeHtml(this._name(entityId))}</div>
        <div class="temps">${temps}</div>
        ${action}
      </div>`;
    }

    _bind() {
      const on = (selector, handler) => {
        for (const el of this._root.querySelectorAll(selector)) el.addEventListener("click", handler);
      };
      on(".set-mode", (ev) => {
        const members = (this._hass.states[this._entityId()].attributes.entity_id || []).filter(Boolean);
        if (!members.length) return;
        // Any member switches the whole zone.
        this._call("climate", "set_hvac_mode", { hvac_mode: ev.currentTarget.dataset.mode }, members[0]);
      });
      on(".join", (ev) => this._call("danfoss_link", "join_living_zone", {}, ev.currentTarget.dataset.entity));
      on(".leave", (ev) => {
        const entity = ev.currentTarget.dataset.entity;
        if (this._confirm !== entity) {
          // Ask once: the room gets its own schedule again.
          this._confirm = entity;
          this._render();
          return;
        }
        this._call("danfoss_link", "leave_living_zone", {}, entity);
      });
      on(".name", (ev) => {
        const event = new Event("hass-more-info", { bubbles: true, composed: true });
        event.detail = { entityId: ev.currentTarget.dataset.entity };
        this.dispatchEvent(event);
      });
    }

    async _call(domain, service, data, entityId) {
      this._busy = true;
      this._error = null;
      this._confirm = null;
      this._render();
      try {
        await this._hass.callService(domain, service, data, { entity_id: entityId });
      } catch (err) {
        this._error = (err && err.message) || String(err);
      }
      this._busy = false;
      this._render();
    }
  };
}

const ZONE_STYLE = `
  :host { display: block; }
  .header { padding: 16px 16px 0; font-size: 1.2em; font-weight: 500; color: var(--primary-text-color); }
  .content { padding: 12px 16px 16px; color: var(--primary-text-color); }
  .mode { margin-bottom: 14px; }
  .toggle { display: inline-flex; border: 1px solid var(--divider-color, #ccc); border-radius: 20px; overflow: hidden; }
  .toggle button { border: none; border-radius: 0; display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px;
                   color: var(--primary-text-color); }
  .toggle button ha-icon { --mdc-icon-size: 18px; }
  .toggle button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .sub { font-size: 0.9em; color: var(--secondary-text-color); margin-top: 8px; }
  .label { font-size: 0.85em; color: var(--secondary-text-color); margin: 4px 0 4px; }
  .label.others { margin-top: 14px; }
  .room { display: flex; align-items: center; gap: 10px; padding: 6px 0; border-top: 1px solid var(--divider-color, rgba(0,0,0,0.08)); }
  .room:first-of-type { border-top: none; }
  .room ha-icon { color: var(--secondary-text-color); flex: none; }
  .room ha-icon.heating { color: var(--state-climate-heat-color, #ff8100); }
  .room .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
  .room:not(.member) .name { color: var(--secondary-text-color); }
  .room .temps { white-space: nowrap; font-variant-numeric: tabular-nums; }
  .room .target { color: var(--secondary-text-color); font-size: 0.9em; }
  .note { margin-top: 6px; font-size: 0.85em; color: var(--secondary-text-color); }
  .error { margin-top: 10px; color: var(--error-color, #db4437); font-size: 0.9em; }
  button { font: inherit; font-weight: 500; border-radius: 18px; padding: 6px 16px; cursor: pointer;
           border: 1px solid var(--divider-color, #ccc); background: none; color: var(--primary-color); }
  button.small { padding: 3px 10px; font-size: 0.85em; flex: none; }
  button.danger { color: var(--error-color, #db4437); border-color: var(--error-color, #db4437); }
  button[disabled] { opacity: 0.5; cursor: default; }
`;

// Same as the other cards: define the element only once the app's own main
// element exists, or a polyfilled custom element registry can lose it.
function registerLivingZoneCard() {
  if (!window.customElements.get("home-assistant")) {
    setTimeout(registerLivingZoneCard, 100);
    return;
  }
  if (window.customElements.get("danfoss-link-living-zone-card")) return;
  window.customElements.define("danfoss-link-living-zone-card", makeLivingZoneCard());
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "danfoss-link-living-zone-card",
    name: "Danfoss Link living zone",
    description: "See and change which rooms are in the living zone, and its mode.",
    preview: true,
  });
}

registerLivingZoneCard();
