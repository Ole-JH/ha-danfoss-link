/*
 * Danfoss Link pause & vacation card.
 *
 * Pause the heating now or plan a pause for later, and plan or end a vacation,
 * as in the app. Reads the house mode select (its pause_* and vacation_*
 * attributes) and uses the danfoss_link.pause / cancel_pause /
 * plan_vacation / cancel_vacation actions. Either part can be hidden.
 *
 * Served and loaded by the integration itself; no separate install.
 */

const DEFAULT_DURATIONS = [1, 2, 4, 8]; // hours

const AWAY_TEXT = {
  en: {
    title: "Pause & vacation",
    title_pause: "Pause heating",
    title_vacation: "Vacation",
    follows_schedule: "Heating follows the schedule",
    house_mode: "House mode",
    paused: "Heating paused",
    on_vacation: "On vacation",
    until: "Until",
    until_resumed: "Until you resume",
    left: "left",
    rooms_hold: "Rooms hold",
    pause_now: "Pause now",
    hour: "h",
    minute: "min",
    forever: "Until resumed",
    plan_pause: "Plan pause…",
    plan_vacation: "Plan vacation…",
    pause_planned: "Pause planned",
    vacation_planned: "Vacation planned",
    resume: "Resume schedule",
    end_vacation: "End vacation",
    extend: "+1 h",
    change_end: "Change end",
    edit: "Change",
    cancel_plan: "Cancel",
    from: "From",
    to: "To",
    to_optional: "To (empty: until resumed)",
    save: "Save",
    close: "Back",
    working: "Please wait…",
    error: "Could not save",
    no_entity: "No Danfoss Link house mode found. Choose it in the card settings.",
    nothing: "This CC has neither pause nor vacation mode.",
    vacation_temp: "Vacation temperature",
    pause_temp: "Pause temperature",
  },
  da: {
    title: "Pause og ferie",
    title_pause: "Sæt varmen på pause",
    title_vacation: "Ferie",
    follows_schedule: "Varmen følger ugeprogrammet",
    house_mode: "Husets tilstand",
    paused: "Varmen er på pause",
    on_vacation: "På ferie",
    until: "Indtil",
    until_resumed: "Indtil du genoptager",
    left: "tilbage",
    rooms_hold: "Rummene holder",
    pause_now: "Pause nu",
    hour: "t",
    minute: "min",
    forever: "Til jeg genoptager",
    plan_pause: "Planlæg pause…",
    plan_vacation: "Planlæg ferie…",
    pause_planned: "Pause planlagt",
    vacation_planned: "Ferie planlagt",
    resume: "Genoptag ugeprogram",
    end_vacation: "Afslut ferie",
    extend: "+1 t",
    change_end: "Ændr slut",
    edit: "Ændr",
    cancel_plan: "Annullér",
    from: "Fra",
    to: "Til",
    to_optional: "Til (tom: til du genoptager)",
    save: "Gem",
    close: "Tilbage",
    working: "Vent venligst…",
    error: "Kunne ikke gemme",
    no_entity: "Fandt ingen Danfoss Link-tilstand for huset. Vælg den i kortets indstillinger.",
    nothing: "Denne CC har hverken pause eller ferie.",
    vacation_temp: "Ferietemperatur",
    pause_temp: "Pausetemperatur",
  },
};

const pad = (n) => String(n).padStart(2, "0");

// "2026-11-02T21:30" for an <input type="datetime-local">, in the browser's time.
function localInput(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function nextHour(offsetHours) {
  const d = new Date(Date.now() + offsetHours * 3600000);
  d.setMinutes(0, 0, 0);
  return new Date(d.getTime() + 3600000);
}

function makeAwayCard() {
  return class DanfossLinkAwayCard extends HTMLElement {
    static getConfigForm() {
      return {
        schema: [
          {
            name: "entity",
            selector: { entity: { filter: { domain: "select", integration: "danfoss_link" } } },
          },
          { name: "title", selector: { text: {} } },
          {
            type: "grid",
            name: "",
            schema: [
              { name: "show_pause", selector: { boolean: {} } },
              { name: "show_vacation", selector: { boolean: {} } },
            ],
          },
        ],
        computeLabel: (schema) =>
          ({
            entity: "House mode",
            title: "Title",
            show_pause: "Show pause",
            show_vacation: "Show vacation",
          })[schema.name],
      };
    }

    static getStubConfig() {
      // No entity: the (first) Danfoss Link house mode.
      return { show_pause: true, show_vacation: true };
    }

    setConfig(config) {
      this._config = config || {};
      this._form = null;
      this._render();
    }

    set hass(hass) {
      const old = this._hass;
      this._hass = hass;
      const id = this._entityId();
      if (!this._root || !old || old.states[id] !== hass.states[id] || old.language !== hass.language) {
        this._render();
      }
    }

    connectedCallback() {
      // Keep "time left" current; not while a form is open.
      this._timer = setInterval(() => !this._form && this._render(), 30000);
    }

    disconnectedCallback() {
      clearInterval(this._timer);
    }

    getCardSize() {
      return 4;
    }

    getGridOptions() {
      return { columns: 6, min_columns: 4, rows: "auto" };
    }

    _t(key) {
      const lang = (this._hass && this._hass.language) || "en";
      return (AWAY_TEXT[lang.split("-")[0]] || AWAY_TEXT.en)[key];
    }

    _show(part) {
      return this._config[`show_${part}`] !== false;
    }

    _entityId() {
      if (this._config && this._config.entity) return this._config.entity;
      if (!this._hass) return undefined;
      const fromRegistry = Object.values(this._hass.entities || {}).find(
        (e) => e.platform === "danfoss_link" && e.translation_key === "house_mode"
      );
      if (fromRegistry) return fromRegistry.entity_id;
      return Object.keys(this._hass.states).find(
        (id) => id.startsWith("select.") && "pause_state" in this._hass.states[id].attributes
      );
    }

    _locale() {
      return (this._hass.locale && this._hass.locale.language) || this._hass.language || "en";
    }

    _when(date) {
      const time = date.toLocaleTimeString(this._locale(), { hour: "2-digit", minute: "2-digit" });
      if (date.toDateString() === new Date().toDateString()) return time;
      const day = date.toLocaleDateString(this._locale(), { weekday: "short", day: "numeric", month: "short" });
      return `${day} ${time}`;
    }

    _left(end) {
      const minutes = Math.max(0, Math.round((end - new Date()) / 60000));
      const days = Math.floor(minutes / 1440);
      const h = Math.floor((minutes % 1440) / 60);
      const m = minutes % 60;
      if (days) return `${days} d ${h} ${this._t("hour")}`;
      if (!h) return `${m} ${this._t("minute")}`;
      return m ? `${h} ${this._t("hour")} ${m} ${this._t("minute")}` : `${h} ${this._t("hour")}`;
    }

    _temp(value) {
      const unit =
        (this._hass.config && this._hass.config.unit_system && this._hass.config.unit_system.temperature) || "°C";
      return `${value} ${unit}`;
    }

    // {state, start, end, temp} for "pause" or "vacation", or null if the CC lacks it.
    _mode(key, attrs) {
      if (!(`${key}_state` in attrs)) return null;
      const date = (v) => (v ? new Date(v) : null);
      return {
        state: attrs[`${key}_state`],
        start: date(attrs[`${key}_start`]),
        end: date(attrs[`${key}_end`]),
        temp: attrs[`${key}_temperature`],
      };
    }

    _render() {
      if (!this._config || !this._hass) return;
      if (!this._root) this._root = this.attachShadow({ mode: "open" });
      const id = this._entityId();
      const state = id && this._hass.states[id];
      const attrs = (state && state.attributes) || {};
      const pause = this._show("pause") ? this._mode("pause", attrs) : null;
      const vacation = this._show("vacation") ? this._mode("vacation", attrs) : null;
      const title =
        this._config.title ||
        (this._show("pause") && this._show("vacation")
          ? this._t("title")
          : this._t(this._show("pause") ? "title_pause" : "title_vacation"));
      const busy = this._busy ? "disabled" : "";

      let body;
      if (!state) {
        body = `<div class="note">${this._t("no_entity")}</div>`;
      } else if (!pause && !vacation) {
        body = `<div class="note">${this._t("nothing")}</div>`;
      } else if (this._form) {
        body = this._formHtml(this._form, pause, vacation, busy);
      } else {
        body = `
          ${this._statusHtml(state, pause, vacation)}
          ${this._plannedHtml(pause, vacation, busy)}
          ${pause ? this._pauseHtml(pause, vacation, busy) : ""}
          ${vacation ? this._vacationHtml(vacation, pause, busy) : ""}
          ${this._planHtml(pause, vacation, busy)}`;
      }
      body += `${this._busy ? `<div class="note">${this._t("working")}</div>` : ""}
        ${this._error ? `<div class="error">${this._t("error")}: ${this._error}</div>` : ""}`;

      this._root.innerHTML = `
        <style>${AWAY_STYLE}</style>
        <ha-card>
          <div class="header">${title}</div>
          <div class="content">${body}</div>
        </ha-card>`;
      this._bind(pause, vacation);
    }

    _statusHtml(state, pause, vacation) {
      const line = (icon, cls, headline, subs) =>
        `<div class="status ${cls}">
           <ha-icon icon="${icon}"></ha-icon>
           <div><div class="headline">${headline}</div>${subs
             .filter(Boolean)
             .map((s) => `<div class="sub">${s}</div>`)
             .join("")}</div>
         </div>`;
      const hold = (m) => (m.temp !== undefined ? `${this._t("rooms_hold")} ${this._temp(m.temp)}` : "");
      if (state.state === "pause" && pause) {
        return line("mdi:pause-circle", "paused", this._t("paused"), [
          pause.end
            ? `${this._t("until")} ${this._when(pause.end)} · ${this._left(pause.end)} ${this._t("left")}`
            : this._t("until_resumed"),
          hold(pause),
        ]);
      }
      if (state.state === "vacation" && vacation) {
        return line("mdi:airplane", "vacation", this._t("on_vacation"), [
          vacation.end
            ? `${this._t("until")} ${this._when(vacation.end)} · ${this._left(vacation.end)} ${this._t("left")}`
            : "",
          hold(vacation),
        ]);
      }
      const headline =
        state.state === "schedule"
          ? this._t("follows_schedule")
          : `${this._t("house_mode")}: ${this._hass.formatEntityState ? this._hass.formatEntityState(state) : state.state}`;
      const temps = [
        pause && pause.temp !== undefined ? `${this._t("pause_temp")} ${this._temp(pause.temp)}` : "",
        vacation && vacation.temp !== undefined ? `${this._t("vacation_temp")} ${this._temp(vacation.temp)}` : "",
      ].filter(Boolean);
      return line("mdi:radiator", "", headline, [temps.join(" · ")]);
    }

    _plannedHtml(pause, vacation, busy) {
      const rows = [];
      const range = (m) => `${m.start ? this._when(m.start) : ""} – ${m.end ? this._when(m.end) : this._t("until_resumed").toLowerCase()}`;
      if (pause && pause.state === "planned") {
        rows.push(`<div class="planned"><ha-icon icon="mdi:timer-pause-outline"></ha-icon>
          <div class="what"><div>${this._t("pause_planned")}</div><div class="sub">${range(pause)}</div></div>
          <button class="small edit-pause" ${busy}>${this._t("edit")}</button>
          <button class="small cancel-pause" ${busy}>${this._t("cancel_plan")}</button></div>`);
      }
      if (vacation && vacation.state === "planned") {
        rows.push(`<div class="planned"><ha-icon icon="mdi:airplane-clock"></ha-icon>
          <div class="what"><div>${this._t("vacation_planned")}</div><div class="sub">${range(vacation)}</div></div>
          <button class="small edit-vacation" ${busy}>${this._t("edit")}</button>
          <button class="small cancel-vacation" ${busy}>${this._t("cancel_plan")}</button></div>`);
      }
      return rows.join("");
    }

    _pauseHtml(pause, vacation, busy) {
      if (pause.state === "active") {
        return `<div class="row end">
          ${pause.end ? `<button class="extend" ${busy}>${this._t("extend")}</button>` : ""}
          <button class="change-end" ${busy}>${this._t("change_end")}</button>
          <button class="primary resume" ${busy}>${this._t("resume")}</button>
        </div>`;
      }
      const chips = (this._config.durations || DEFAULT_DURATIONS)
        .map((h) => `<button class="chip" data-hours="${h}" ${busy}>${h} ${this._t("hour")}</button>`)
        .join("");
      return `<div class="section">
        <div class="label">${this._t("pause_now")}</div>
        <div class="row">${chips}
          <button class="chip forever" ${busy}>${this._t("forever")}</button>
        </div></div>`;
    }

    _vacationHtml(vacation, pause, busy) {
      if (vacation.state === "active") {
        return `<div class="row end">
          <button class="change-vacation" ${busy}>${this._t("change_end")}</button>
          <button class="primary cancel-vacation" ${busy}>${this._t("end_vacation")}</button>
        </div>`;
      }
      return "";
    }

    // "Plan pause…" / "Plan vacation…" for what is neither running nor planned.
    _planHtml(pause, vacation, busy) {
      const buttons = [];
      if (pause && !pause.state) buttons.push(`<button class="plan-pause" ${busy}>${this._t("plan_pause")}</button>`);
      if (vacation && !vacation.state) buttons.push(`<button class="plan-vacation" ${busy}>${this._t("plan_vacation")}</button>`);
      return buttons.length ? `<div class="row end">${buttons.join("")}</div>` : "";
    }

    _formHtml(form, pause, vacation, busy) {
      // form: {kind: "pause"|"vacation", endOnly: bool}
      const m = form.kind === "pause" ? pause : vacation;
      const start = (m && m.state && m.start) || (form.kind === "vacation" ? nextHour(0) : nextHour(0));
      const end =
        (m && m.state && m.end) ||
        (form.kind === "vacation" ? nextHour(24 * 7 - 1) : new Date(start.getTime() + 2 * 3600000));
      const step = form.kind === "vacation" ? 'step="3600"' : 'step="900"';
      const field = (cls, label, value, required) =>
        `<label>${label}<input type="datetime-local" class="${cls}" ${step} value="${value ? localInput(value) : ""}" ${required ? "required" : ""}></label>`;
      return `<div class="form">
        <div class="headline">${this._t(form.kind === "pause" ? "plan_pause" : "plan_vacation").replace("…", "")}</div>
        ${form.endOnly ? "" : field("from", this._t("from"), start, true)}
        ${field("to", this._t(form.kind === "pause" ? "to_optional" : "to"), end, form.kind === "vacation")}
        <div class="row end">
          <button class="close" ${busy}>${this._t("close")}</button>
          <button class="primary save" ${busy}>${this._t("save")}</button>
        </div></div>`;
    }

    _bind(pause, vacation) {
      const on = (selector, handler) => {
        for (const el of this._root.querySelectorAll(selector)) el.addEventListener("click", handler);
      };
      const openForm = (form) => () => {
        this._form = form;
        this._error = null;
        this._render();
      };
      on(".chip[data-hours]", (ev) => this._pause({ duration: { hours: +ev.currentTarget.dataset.hours } }));
      on(".forever", () => this._pause({}));
      on(".plan-pause, .edit-pause", openForm({ kind: "pause" }));
      on(".change-end", openForm({ kind: "pause", endOnly: true }));
      on(".plan-vacation, .edit-vacation", openForm({ kind: "vacation" }));
      on(".change-vacation", openForm({ kind: "vacation", endOnly: true }));
      on(".resume", () => this._call("danfoss_link", "cancel_pause", {}, true));
      on(".cancel-pause", () => this._call("danfoss_link", "cancel_pause", {}, true));
      on(".cancel-vacation", () => this._call("danfoss_link", "cancel_vacation", {}));
      on(".extend", () => {
        const next = new Date(Math.max(pause.end ? pause.end.getTime() : 0, Date.now()) + 3600000);
        this._pause({ end: `${localInput(next)}:00` });
      });
      on(".close", () => {
        this._form = null;
        this._error = null;
        this._render();
      });
      on(".save", () => this._save(pause, vacation));
    }

    _save(pause, vacation) {
      const value = (cls) => {
        const el = this._root.querySelector(`.${cls}`);
        return el && el.value ? `${el.value}:00` : null;
      };
      const form = this._form;
      const from = form.endOnly ? null : value("from");
      const to = value("to");
      if (form.kind === "pause") {
        const data = {};
        if (from) data.start = from;
        if (to) data.end = to;
        return this._pause(data);
      }
      // A vacation always needs both; when only the end changes, keep the start.
      const start = from || (vacation && vacation.start ? `${localInput(vacation.start)}:00` : null);
      if (!start || !to) return;
      return this._call("danfoss_link", "plan_vacation", { start, end: to });
    }

    _pause(data) {
      return this._call("danfoss_link", "pause", data, true);
    }

    async _call(domain, service, data, targeted) {
      this._busy = true;
      this._error = null;
      this._render();
      try {
        await this._hass.callService(
          domain,
          service,
          data,
          targeted ? { entity_id: this._entityId() } : undefined
        );
        this._form = null;
      } catch (err) {
        this._error = (err && err.message) || String(err);
      }
      this._busy = false;
      this._render();
    }
  };
}

const AWAY_STYLE = `
  :host { display: block; }
  .header { padding: 16px 16px 0; font-size: 1.2em; font-weight: 500; color: var(--primary-text-color); }
  .content { padding: 12px 16px 16px; color: var(--primary-text-color); }
  .status { display: flex; gap: 12px; align-items: center; margin-bottom: 12px; }
  .status ha-icon { --mdc-icon-size: 32px; color: var(--state-climate-heat-color, #ff8100);
                    background: rgba(255,129,0,0.12); border-radius: 50%; padding: 8px; flex: none; }
  .status.paused ha-icon, .status.vacation ha-icon { color: var(--dl-pause, var(--info-color, #4285f4)); background: rgba(66,133,244,0.15); }
  .headline { font-size: 1.05em; font-weight: 500; }
  .sub { font-size: 0.9em; color: var(--secondary-text-color); margin-top: 2px; }
  .planned { display: flex; gap: 10px; align-items: center; padding: 8px 10px; margin-bottom: 8px;
             border-radius: 10px; background: rgba(66,133,244,0.08); }
  .planned ha-icon { color: var(--info-color, #4285f4); flex: none; }
  .planned .what { flex: 1; min-width: 0; }
  .section { margin-top: 12px; }
  .label { font-size: 0.85em; color: var(--secondary-text-color); margin-bottom: 6px; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .row.end { justify-content: flex-end; margin-top: 12px; }
  .form { display: flex; flex-direction: column; gap: 10px; }
  .form label { display: flex; flex-direction: column; gap: 4px; font-size: 0.85em; color: var(--secondary-text-color); }
  .form input { font: inherit; font-size: 1rem; padding: 6px 10px; border-radius: 8px; min-width: 0;
                border: 1px solid var(--divider-color, #ccc); background: var(--card-background-color, transparent);
                color: var(--primary-text-color); color-scheme: light dark; }
  .note { margin-top: 10px; font-size: 0.85em; color: var(--secondary-text-color); }
  .error { margin-top: 10px; color: var(--error-color, #db4437); font-size: 0.9em; }
  button { font: inherit; font-weight: 500; border-radius: 18px; padding: 6px 16px; cursor: pointer;
           border: 1px solid var(--divider-color, #ccc); background: none; color: var(--primary-color); }
  button.chip { padding: 4px 12px; font-size: 0.9em; }
  button.small { padding: 3px 10px; font-size: 0.85em; }
  button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  button[disabled] { opacity: 0.6; cursor: default; }
`;

// Same as the schedule card: define the element only once the app's own main
// element exists, or a polyfilled custom element registry can lose it.
function registerAwayCard() {
  if (!window.customElements.get("home-assistant")) {
    setTimeout(registerAwayCard, 100);
    return;
  }
  if (window.customElements.get("danfoss-link-away-card")) return;
  window.customElements.define("danfoss-link-away-card", makeAwayCard());
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "danfoss-link-away-card",
    name: "Danfoss Link pause & vacation",
    description: "Pause the heating now or later, and plan vacations.",
    preview: true,
  });
}

registerAwayCard();
