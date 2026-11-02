/*
 * Danfoss Link pause card.
 *
 * Pauses the heating, as the app's Pause house mode does: every room holds the
 * pause temperature until the chosen end time, or until you resume. Reads the
 * house mode select (its `pause_end` and `pause_temperature` attributes),
 * pauses with the danfoss_link.pause action and resumes by selecting the
 * schedule again.
 *
 * Served and loaded by the integration itself; no separate install.
 */

const DEFAULT_DURATIONS = [1, 2, 4, 8]; // hours

const PAUSE_TEXT = {
  en: {
    title: "Pause heating",
    paused: "Heating paused",
    until: "Until",
    until_resumed: "Until you resume",
    left: "left",
    follows_schedule: "Heating follows the schedule",
    house_mode: "House mode",
    pause_info: "Pausing sets every room, Manual ones too, to",
    pause_info_no_temp: "Pausing sets every room, Manual ones too, to the pause temperature.",
    rooms_at: "Every room holds",
    pause_for: "Pause for",
    hour: "h",
    minute: "min",
    forever: "Until resumed",
    pick_time: "Until…",
    pause: "Pause",
    resume: "Resume schedule",
    extend: "+1 h",
    change_end: "Change end",
    cancel: "Cancel",
    working: "Please wait…",
    error: "Could not change the pause",
    no_entity: "No Danfoss Link house mode found. Choose it in the card settings.",
    no_pause: "This CC has no pause mode.",
  },
  da: {
    title: "Sæt varmen på pause",
    paused: "Varmen er på pause",
    until: "Indtil",
    until_resumed: "Indtil du genoptager",
    left: "tilbage",
    follows_schedule: "Varmen følger ugeprogrammet",
    house_mode: "Husets tilstand",
    pause_info: "En pause sætter alle rum, også dem på Manuel, til",
    pause_info_no_temp: "En pause sætter alle rum, også dem på Manuel, til pausetemperaturen.",
    rooms_at: "Alle rum holder",
    pause_for: "Pause i",
    hour: "t",
    minute: "min",
    forever: "Til jeg genoptager",
    pick_time: "Indtil…",
    pause: "Pause",
    resume: "Genoptag ugeprogram",
    extend: "+1 t",
    change_end: "Ændr slut",
    cancel: "Fortryd",
    working: "Vent venligst…",
    error: "Kunne ikke ændre pausen",
    no_entity: "Fandt ingen Danfoss Link-tilstand for huset. Vælg den i kortets indstillinger.",
    no_pause: "Denne CC har ingen pausetilstand.",
  },
};

// "2026-11-02T21:30" for an <input type="datetime-local">, in the browser's time.
function localInput(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function makePauseCard() {
  return class DanfossLinkPauseCard extends HTMLElement {
    static getConfigForm() {
      return {
        schema: [
          {
            name: "entity",
            selector: { entity: { filter: { domain: "select", integration: "danfoss_link" } } },
          },
          { name: "title", selector: { text: {} } },
        ],
      };
    }

    static getStubConfig() {
      // No entity: the (first) Danfoss Link house mode.
      return {};
    }

    setConfig(config) {
      this._config = config || {};
      this._picking = false;
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
      // Keep the "time left" current.
      this._timer = setInterval(() => this._paused() && !this._picking && this._render(), 30000);
    }

    disconnectedCallback() {
      clearInterval(this._timer);
    }

    getCardSize() {
      return 3;
    }

    getGridOptions() {
      return { columns: 6, min_columns: 4, rows: "auto" };
    }

    _t(key) {
      const lang = (this._hass && this._hass.language) || "en";
      return (PAUSE_TEXT[lang.split("-")[0]] || PAUSE_TEXT.en)[key];
    }

    _entityId() {
      if (this._config && this._config.entity) return this._config.entity;
      if (!this._hass) return undefined;
      const registry = this._hass.entities || {};
      const fromRegistry = Object.values(registry).find(
        (e) => e.platform === "danfoss_link" && e.translation_key === "house_mode"
      );
      if (fromRegistry) return fromRegistry.entity_id;
      return Object.keys(this._hass.states).find(
        (id) => id.startsWith("select.") && "pause_end" in this._hass.states[id].attributes
      );
    }

    _state() {
      const id = this._entityId();
      return id && this._hass ? this._hass.states[id] : undefined;
    }

    _paused() {
      const state = this._state();
      return !!state && state.state === "pause";
    }

    _locale() {
      return (this._hass && this._hass.locale && this._hass.locale.language) || this._hass.language || "en";
    }

    _formatEnd(end) {
      const now = new Date();
      const time = end.toLocaleTimeString(this._locale(), { hour: "2-digit", minute: "2-digit" });
      if (end.toDateString() === now.toDateString()) return time;
      const day = end.toLocaleDateString(this._locale(), { weekday: "short", day: "numeric", month: "short" });
      return `${day} ${time}`;
    }

    _formatLeft(end) {
      const minutes = Math.max(0, Math.round((end - new Date()) / 60000));
      const h = Math.floor(minutes / 60);
      const m = minutes % 60;
      if (!h) return `${m} ${this._t("minute")}`;
      return m ? `${h} ${this._t("hour")} ${m} ${this._t("minute")}` : `${h} ${this._t("hour")}`;
    }

    _modeText(state) {
      if (state.state === "schedule") return this._t("follows_schedule");
      const label = this._hass.formatEntityState ? this._hass.formatEntityState(state) : state.state;
      return `${this._t("house_mode")}: ${label}`;
    }

    _temp(value) {
      const unit =
        (this._hass.config && this._hass.config.unit_system && this._hass.config.unit_system.temperature) || "°C";
      return `${value} ${unit}`;
    }

    _render() {
      if (!this._config || !this._hass) return;
      if (!this._root) this._root = this.attachShadow({ mode: "open" });
      const state = this._state();
      const attrs = (state && state.attributes) || {};
      const title = this._config.title || this._t("title");
      const paused = this._paused();
      const end = attrs.pause_end ? new Date(attrs.pause_end) : null;
      const temp = attrs.pause_temperature;
      const busy = this._busy ? "disabled" : "";

      let body;
      if (!state) {
        body = `<div class="note">${this._t("no_entity")}</div>`;
      } else if (!(attrs.options || []).includes("pause")) {
        body = `<div class="note">${this._t("no_pause")}</div>`;
      } else {
        const status = paused
          ? `<div class="status paused">
               <ha-icon icon="mdi:pause-circle"></ha-icon>
               <div>
                 <div class="headline">${this._t("paused")}</div>
                 <div class="sub">${
                   end
                     ? `${this._t("until")} ${this._formatEnd(end)} · ${this._formatLeft(end)} ${this._t("left")}`
                     : this._t("until_resumed")
                 }</div>
                 ${temp !== undefined ? `<div class="sub">${this._t("rooms_at")} ${this._temp(temp)}</div>` : ""}
               </div>
             </div>`
          : `<div class="status">
               <ha-icon icon="mdi:radiator"></ha-icon>
               <div>
                 <div class="headline">${this._modeText(state)}</div>
                 <div class="sub">${
                   temp !== undefined
                     ? `${this._t("pause_info")} ${this._temp(temp)}.`
                     : this._t("pause_info_no_temp")
                 }</div>
               </div>
             </div>`;

        let actions;
        if (this._picking) {
          const initial = end || new Date(Date.now() + 2 * 3600000);
          actions = `
            <div class="pick">
              <input type="datetime-local" class="when" value="${localInput(initial)}" min="${localInput(new Date())}">
              <button class="cancel" ${busy}>${this._t("cancel")}</button>
              <button class="primary until" ${busy}>${this._t("pause")}</button>
            </div>`;
        } else if (paused) {
          actions = `
            <div class="row">
              ${end ? `<button class="extend" ${busy}>${this._t("extend")}</button>` : ""}
              <button class="picker" ${busy}>${this._t("change_end")}</button>
              <button class="primary resume" ${busy}>${this._t("resume")}</button>
            </div>`;
        } else {
          const durations = (this._config.durations || DEFAULT_DURATIONS)
            .map((h) => `<button class="chip" data-hours="${h}" ${busy}>${h} ${this._t("hour")}</button>`)
            .join("");
          actions = `
            <div class="label">${this._t("pause_for")}</div>
            <div class="row chips">
              ${durations}
              <button class="chip picker" ${busy}>${this._t("pick_time")}</button>
              <button class="chip forever" ${busy}>${this._t("forever")}</button>
            </div>`;
        }
        body = `
          ${status}
          ${actions}
          ${this._busy ? `<div class="note">${this._t("working")}</div>` : ""}
          ${this._error ? `<div class="error">${this._t("error")}: ${this._error}</div>` : ""}`;
      }

      this._root.innerHTML = `
        <style>${PAUSE_STYLE}</style>
        <ha-card>
          <div class="header">${title}</div>
          <div class="content">${body}</div>
        </ha-card>`;

      const on = (selector, handler) => {
        for (const el of this._root.querySelectorAll(selector)) el.addEventListener("click", handler);
      };
      on(".chip[data-hours]", (ev) => this._pause({ duration: { hours: +ev.currentTarget.dataset.hours } }));
      on(".forever", () => this._pause({}));
      on(".picker", () => {
        this._picking = true;
        this._error = null;
        this._render();
      });
      on(".cancel", () => {
        this._picking = false;
        this._render();
      });
      on(".until", () => {
        const value = this._root.querySelector(".when").value;
        if (value) this._pause({ end: `${value}:00` });
      });
      on(".extend", () => {
        const next = new Date(Math.max(end ? end.getTime() : 0, Date.now()) + 3600000);
        this._pause({ end: `${localInput(next)}:00` });
      });
      on(".resume", () =>
        this._call("select", "select_option", { option: "schedule" })
      );
    }

    _pause(data) {
      return this._call("danfoss_link", "pause", data);
    }

    async _call(domain, service, data) {
      this._busy = true;
      this._error = null;
      this._render();
      try {
        await this._hass.callService(domain, service, data, { entity_id: this._entityId() });
        this._picking = false;
      } catch (err) {
        this._error = (err && err.message) || String(err);
      }
      this._busy = false;
      this._render();
    }
  };
}

const PAUSE_STYLE = `
  :host { display: block; }
  .header { padding: 16px 16px 0; font-size: 1.2em; font-weight: 500; color: var(--primary-text-color); }
  .content { padding: 12px 16px 16px; color: var(--primary-text-color); }
  .status { display: flex; gap: 12px; align-items: center; margin-bottom: 14px; }
  .status ha-icon { --mdc-icon-size: 32px; color: var(--state-climate-heat-color, #ff8100);
                    background: rgba(255,129,0,0.12); border-radius: 50%; padding: 8px; flex: none; }
  .status.paused ha-icon { color: var(--dl-pause, var(--info-color, #4285f4)); background: rgba(66,133,244,0.15); }
  .headline { font-size: 1.05em; font-weight: 500; }
  .sub { font-size: 0.9em; color: var(--secondary-text-color); margin-top: 2px; }
  .label { font-size: 0.85em; color: var(--secondary-text-color); margin-bottom: 6px; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .row:not(.chips) { justify-content: flex-end; }
  .pick { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; justify-content: flex-end; }
  .pick input { flex: 1 1 180px; font: inherit; padding: 6px 10px; border-radius: 8px; min-width: 0;
                border: 1px solid var(--divider-color, #ccc); background: var(--card-background-color, transparent);
                color: var(--primary-text-color); color-scheme: light dark; }
  .note { margin-top: 10px; font-size: 0.85em; color: var(--secondary-text-color); }
  .error { margin-top: 10px; color: var(--error-color, #db4437); font-size: 0.9em; }
  button { font: inherit; font-weight: 500; border-radius: 18px; padding: 6px 16px; cursor: pointer;
           border: 1px solid var(--divider-color, #ccc); background: none; color: var(--primary-color); }
  button.chip { padding: 4px 12px; font-size: 0.9em; }
  button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  button[disabled] { opacity: 0.6; cursor: default; }
`;

// Same as the schedule card: define the element only once the app's own main
// element exists, or a polyfilled custom element registry can lose it.
function registerPauseCard() {
  if (!window.customElements.get("home-assistant")) {
    setTimeout(registerPauseCard, 100);
    return;
  }
  if (window.customElements.get("danfoss-link-pause-card")) return;
  window.customElements.define("danfoss-link-pause-card", makePauseCard());
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "danfoss-link-pause-card",
    name: "Danfoss Link pause",
    description: "Pause the heating for a while, or until you resume.",
    preview: true,
  });
}

registerPauseCard();
