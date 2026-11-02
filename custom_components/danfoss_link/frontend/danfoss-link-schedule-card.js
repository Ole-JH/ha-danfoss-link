/*
 * Danfoss Link schedule card.
 *
 * Shows a room's weekly schedule (the climate entity's `schedule` attribute)
 * as a grid of 15-minute cells, at home or away. Drag over the grid to paint
 * a block (across several days at once), then press Save: the changed days
 * are sent with the danfoss_link.set_schedule action.
 *
 * Served and loaded by the integration itself; no separate install.
 */

const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
const SLOTS = 96; // 15-minute cells per day

const TEXT = {
  en: {
    at_home: "At home",
    away: "Away (setback)",
    save: "Save",
    cancel: "Cancel",
    saving: "Saving…",
    hint: "Drag over the grid to paint at home or away. Drag down to change several days.",
    living: "Part of the living zone: saving changes the schedule of every room in the zone.",
    no_schedule: "This thermostat has no schedule (yet). It is read from the CC once an hour.",
    pick_entity: "Choose a Danfoss Link thermostat in the card settings.",
    error: "Could not save",
    changed: "unsaved days",
  },
  da: {
    at_home: "Hjemme",
    away: "Ude (sænkning)",
    save: "Gem",
    cancel: "Fortryd",
    saving: "Gemmer…",
    hint: "Træk hen over skemaet for at male hjemme eller ude. Træk nedad for at ændre flere dage.",
    living: "Rummet er i Living Zone: når du gemmer, ændres skemaet for alle rum i zonen.",
    no_schedule: "Termostaten har ikke noget skema (endnu). Det hentes fra CC'en én gang i timen.",
    pick_entity: "Vælg en Danfoss Link-termostat i kortets indstillinger.",
    error: "Kunne ikke gemme",
    changed: "dage ikke gemt",
  },
};

function parseDay(periods) {
  const slots = new Array(SLOTS).fill(false);
  for (const text of periods || []) {
    const m = /^(\d{2}):(\d{2})-(\d{2}):(\d{2})/.exec(text);
    if (!m) continue;
    const start = Math.floor((+m[1] * 60 + +m[2]) / 15);
    const end = Math.ceil((+m[3] * 60 + +m[4]) / 15);
    for (let i = start; i < end && i < SLOTS; i++) slots[i] = true;
  }
  return slots;
}

function clock(slot) {
  const minutes = slot * 15;
  return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

function formatDay(slots) {
  const periods = [];
  let start = null;
  for (let i = 0; i <= SLOTS; i++) {
    const on = i < SLOTS && slots[i];
    if (on && start === null) start = i;
    if (!on && start !== null) {
      periods.push(`${clock(start)}-${clock(i)}`);
      start = null;
    }
  }
  return periods;
}

// Built only once Home Assistant's app has set up its custom element
// registry (see register() below), so it extends the right HTMLElement.
function makeCard() {
  return class DanfossLinkScheduleCard extends HTMLElement {
    static getConfigForm() {
      return {
        schema: [
          {
            name: "entity",
            required: true,
            selector: { entity: { filter: { domain: "climate", integration: "danfoss_link" } } },
          },
          { name: "title", selector: { text: {} } },
        ],
      };
    }

    static getStubConfig(hass) {
      const entity = Object.keys(hass.states).find(
        (id) => id.startsWith("climate.") && hass.states[id].attributes.schedule
      );
      return { entity: entity || "" };
    }

    setConfig(config) {
      this._config = config;
      this._draft = null;
      this._render();
    }

    set hass(hass) {
      const old = this._hass && this._config && this._hass.states[this._config.entity];
      this._hass = hass;
      const state = this._config && hass.states[this._config.entity];
      if (!this._root || state !== old) this._render();
    }

    getCardSize() {
      return 5;
    }

    getGridOptions() {
      return { columns: 12, min_columns: 6, rows: "auto" };
    }

    _t(key) {
      const lang = (this._hass && this._hass.language) || "en";
      return (TEXT[lang.split("-")[0]] || TEXT.en)[key];
    }

    _weekdayName(day) {
      // 2024-01-01 was a Monday.
      const date = new Date(Date.UTC(2024, 0, 1 + DAYS.indexOf(day)));
      const lang = (this._hass && this._hass.locale && this._hass.locale.language) || "en";
      return date.toLocaleDateString(lang, { weekday: "short", timeZone: "UTC" });
    }

    _saved() {
      const state = this._hass && this._hass.states[this._config.entity];
      const schedule = state && state.attributes.schedule;
      if (!schedule) return null;
      return Object.fromEntries(DAYS.map((d) => [d, parseDay(schedule[d])]));
    }

    _week() {
      return this._draft || this._saved();
    }

    _changedDays() {
      if (!this._draft) return [];
      const saved = this._saved() || {};
      return DAYS.filter(
        (d) => !saved[d] || saved[d].some((on, i) => on !== this._draft[d][i])
      );
    }

    _render() {
      if (!this._config || !this._hass) return;
      if (this._painting) return; // don't redraw under the pointer
      if (!this._root) {
        this._root = this.attachShadow({ mode: "open" });
      }
      const state = this._hass.states[this._config.entity];
      const week = state && this._week();
      const attrs = (state && state.attributes) || {};
      const title = this._config.title || attrs.friendly_name || this._config.entity || "";
      const unit = (this._hass.config && this._hass.config.unit_system && this._hass.config.unit_system.temperature) || "°C";
      const temp = (v) => (v === undefined || v === null ? "" : ` ${v} ${unit}`);

      let body;
      if (!this._config.entity || !state) {
        body = `<div class="note">${this._t("pick_entity")}</div>`;
      } else if (!week) {
        body = `<div class="note">${this._t("no_schedule")}</div>`;
      } else {
        const now = new Date();
        const today = DAYS[(now.getDay() + 6) % 7];
        const nowSlot = Math.floor((now.getHours() * 60 + now.getMinutes()) / 15);
        const changed = this._changedDays();
        const rows = DAYS.map((day) => {
          const cells = week[day]
            .map((on, i) => `<div class="cell ${on ? "on" : ""}${i % 4 === 0 ? " hour" : ""}" data-day="${day}" data-slot="${i}"></div>`)
            .join("");
          const marker =
            day === today ? `<div class="now" style="left:${(nowSlot / SLOTS) * 100}%"></div>` : "";
          const periods = formatDay(week[day]).join(", ") || "–";
          return `
            <div class="day${changed.includes(day) ? " changed" : ""}">${this._weekdayName(day)}</div>
            <div class="track" title="${periods}">${cells}${marker}</div>`;
        }).join("");
        const ticks = [0, 3, 6, 9, 12, 15, 18, 21, 24]
          .map((h) => `<span style="left:${(h / 24) * 100}%">${String(h).padStart(2, "0")}</span>`)
          .join("");
        body = `
          <div class="legend">
            <span><i class="swatch on"></i>${this._t("at_home")}${temp(attrs.at_home_setpoint)}</span>
            <span><i class="swatch"></i>${this._t("away")}${temp(attrs.away_setpoint)}</span>
          </div>
          <div class="grid">
            <div></div><div class="ticks">${ticks}</div>
            ${rows}
          </div>
          ${attrs.zone === "Living" ? `<div class="note">${this._t("living")}</div>` : ""}
          ${
            changed.length
              ? `<div class="actions">
                   <span class="count">${changed.length} ${this._t("changed")}</span>
                   <button class="cancel">${this._t("cancel")}</button>
                   <button class="save primary" ${this._saving ? "disabled" : ""}>${this._saving ? this._t("saving") : this._t("save")}</button>
                 </div>`
              : `<div class="hint">${this._t("hint")}</div>`
          }
          ${this._error ? `<div class="error">${this._t("error")}: ${this._error}</div>` : ""}`;
      }

      this._root.innerHTML = `
        <style>${STYLE}</style>
        <ha-card>
          <div class="header">${title}</div>
          <div class="content">${body}</div>
        </ha-card>`;

      const grid = this._root.querySelector(".grid");
      if (grid) grid.addEventListener("pointerdown", (ev) => this._start(ev));
      const save = this._root.querySelector(".save");
      if (save) save.addEventListener("click", () => this._save());
      const cancel = this._root.querySelector(".cancel");
      if (cancel)
        cancel.addEventListener("click", () => {
          this._draft = null;
          this._error = null;
          this._render();
        });
    }

    _cellAt(ev) {
      const el = this._root.elementFromPoint
        ? this._root.elementFromPoint(ev.clientX, ev.clientY)
        : null;
      return el && el.classList && el.classList.contains("cell") ? el : null;
    }

    _start(ev) {
      const cell = this._cellAt(ev) || (ev.target.classList.contains("cell") ? ev.target : null);
      if (!cell) return;
      ev.preventDefault();
      const week = this._week();
      this._base = Object.fromEntries(DAYS.map((d) => [d, week[d].slice()]));
      this._anchor = { day: DAYS.indexOf(cell.dataset.day), slot: +cell.dataset.slot };
      this._value = !week[cell.dataset.day][this._anchor.slot];
      this._painting = true;
      this._paint(this._anchor);
      const move = (e) => {
        const target = this._cellAt(e);
        if (target) this._paint({ day: DAYS.indexOf(target.dataset.day), slot: +target.dataset.slot });
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        window.removeEventListener("pointercancel", up);
        this._painting = false;
        if (!this._changedDays().length) this._draft = null;
        this._render();
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      window.addEventListener("pointercancel", up);
    }

    _paint(to) {
      const [d0, d1] = [this._anchor.day, to.day].sort((a, b) => a - b);
      const [s0, s1] = [this._anchor.slot, to.slot].sort((a, b) => a - b);
      const draft = Object.fromEntries(DAYS.map((d) => [d, this._base[d].slice()]));
      for (let d = d0; d <= d1; d++) {
        for (let s = s0; s <= s1; s++) draft[DAYS[d]][s] = this._value;
      }
      this._draft = draft;
      // Update the cells in place while dragging.
      for (const cell of this._root.querySelectorAll(".cell")) {
        cell.classList.toggle("on", draft[cell.dataset.day][+cell.dataset.slot]);
      }
    }

    async _save() {
      const changed = this._changedDays();
      if (!changed.length) return;
      // One action call per distinct day, e.g. all weekdays at once.
      const groups = new Map();
      for (const day of changed) {
        const periods = formatDay(this._draft[day]);
        const key = periods.join(",");
        if (!groups.has(key)) groups.set(key, { days: [], at_home: periods });
        groups.get(key).days.push(day);
      }
      this._saving = true;
      this._error = null;
      this._render();
      try {
        for (const data of groups.values()) {
          await this._hass.callService("danfoss_link", "set_schedule", data, {
            entity_id: this._config.entity,
          });
        }
        this._draft = null;
      } catch (err) {
        this._error = (err && err.message) || String(err);
      }
      this._saving = false;
      this._render();
    }
  };
}

const STYLE = `
  :host { display: block; }
  ha-card { overflow: hidden; }
  .header { padding: 16px 16px 0; font-size: 1.2em; font-weight: 500; color: var(--primary-text-color); }
  .content { padding: 12px 16px 16px; color: var(--primary-text-color); }
  .legend { display: flex; gap: 16px; flex-wrap: wrap; font-size: 0.9em; margin-bottom: 8px; color: var(--secondary-text-color); }
  .swatch { display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 6px; vertical-align: -1px;
            background: var(--dl-away, rgba(66,133,244,0.18)); }
  .swatch.on, .cell.on { background: var(--dl-home, var(--state-climate-heat-color, #ff8100)); }
  .grid { display: grid; grid-template-columns: auto 1fr; column-gap: 8px; row-gap: 4px; align-items: center;
          touch-action: none; user-select: none; -webkit-user-select: none; }
  .ticks { position: relative; height: 14px; font-size: 0.7em; color: var(--secondary-text-color); }
  .ticks span { position: absolute; transform: translateX(-50%); }
  .ticks span:first-child { transform: none; }
  .ticks span:last-child { transform: translateX(-100%); }
  .day { font-size: 0.85em; color: var(--secondary-text-color); text-transform: capitalize; min-width: 2.5em; }
  .day.changed { color: var(--primary-color); font-weight: 600; }
  .track { position: relative; display: grid; grid-template-columns: repeat(96, 1fr); height: 26px;
           border-radius: 6px; overflow: hidden; background: var(--dl-away, rgba(66,133,244,0.18)); cursor: crosshair; }
  .cell { background: transparent; }
  .cell.hour { box-shadow: inset 1px 0 0 rgba(127,127,127,0.18); }
  .now { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--primary-text-color); opacity: 0.7; pointer-events: none; }
  .note, .hint { margin-top: 10px; font-size: 0.85em; color: var(--secondary-text-color); }
  .error { margin-top: 10px; color: var(--error-color, #db4437); font-size: 0.9em; }
  .actions { display: flex; gap: 8px; justify-content: flex-end; align-items: center; margin-top: 12px; }
  .count { margin-right: auto; font-size: 0.85em; color: var(--secondary-text-color); }
  button { font: inherit; font-weight: 500; border-radius: 18px; padding: 6px 16px; cursor: pointer;
           border: 1px solid var(--divider-color, #ccc); background: none; color: var(--primary-color); }
  button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  button[disabled] { opacity: 0.6; cursor: default; }
`;

// The integration loads this file as an extra frontend module, which can run
// before the app has finished loading. On browsers without native scoped
// registries the app then swaps in a polyfilled customElements registry, and
// an element defined earlier is lost ("Custom element not found"). So wait
// for the app's own main element to be defined before defining the card.
function register() {
  if (!window.customElements.get("home-assistant")) {
    setTimeout(register, 100);
    return;
  }
  if (window.customElements.get("danfoss-link-schedule-card")) return;
  window.customElements.define("danfoss-link-schedule-card", makeCard());
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "danfoss-link-schedule-card",
    name: "Danfoss Link schedule",
    description: "See and edit a Danfoss Link room's weekly schedule.",
    preview: true,
  });
}

register();
