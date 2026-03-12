/**
 * choices-init.js — Liquid Glass custom select dropdowns
 *
 * Replaces native <select> elements with Choices.js instances that can receive
 * backdrop-filter glass styling. Also exposes:
 *
 *   window.igbRefreshChoices(id)            — destroy + reinit after innerHTML update
 *   window.igbChoicesSetVisible(id, bool)   — show/hide the Choices.js wrapper
 *   window.igbChoicesSetDisabled(id, bool)  — enable/disable a Choices.js instance
 *   window._igbChoices                      — map of id → Choices instance
 */
(function () {
    'use strict';

    window._igbChoices = {};

    if (typeof Choices === 'undefined') {
        console.warn('[Choices-Init] Choices.js not loaded — skipping');
        return;
    }

    /* ── Base config shared by all instances ── */
    const BASE = {
        searchEnabled: false,
        itemSelectText: '',
        shouldSort: false,
        allowHTML: false,
        noResultsText: 'No options',
        noChoicesText: 'No options available',
        classNames: { containerOuter: ['choices'] },
    };

    /* ── Internal: initialise one element ── */
    function _init(el, extra) {
        if (!el || !el.id) return null;
        const id = el.id;
        // Destroy any existing instance first
        const old = window._igbChoices[id];
        if (old) { try { old.destroy(); } catch (e) {} delete window._igbChoices[id]; }
        try {
            const inst = new Choices(el, Object.assign({}, BASE, extra || {}));
            // Sync disabled state
            if (el.disabled) inst.disable();
            window._igbChoices[id] = inst;
            return inst;
        } catch (e) {
            console.warn('[Choices-Init] Failed to init #' + id, e);
            return null;
        }
    }

    /* ── Public: refresh an instance after innerHTML has been changed ── */
    window.igbRefreshChoices = function (id, extra) {
        const el = document.getElementById(id);
        if (!el) return null;
        return _init(el, extra);
    };

    /* ── Public: show or hide the Choices.js wrapper (not the native select) ── */
    window.igbChoicesSetVisible = function (id, visible) {
        const inst = window._igbChoices[id];
        if (inst) {
            inst.containerOuter.element.style.display = visible ? '' : 'none';
        } else {
            const el = document.getElementById(id);
            if (el) el.style.display = visible ? '' : 'none';
        }
    };

    /* ── Public: enable or disable a Choices.js instance ── */
    window.igbChoicesSetDisabled = function (id, disabled) {
        const inst = window._igbChoices[id];
        const el   = document.getElementById(id);
        if (inst) {
            disabled ? inst.disable() : inst.enable();
        }
        if (el) el.disabled = disabled;
    };

    /* ── Selects that have static options and can be inited immediately ── */
    const STATIC_IDS = [
        // Dialer — always-present static selects
        'multiLineCount', 'agentStateSelect', 'dlrChannelSelect',
        'inboxChannelSelect', 'iosCalEvStatus', 'dialerSmartFilter',
        // Voice config
        'voiceSelection', 'buyNumberType', 'voiceRingTimeout',
        'voicePauseBetween', 'voiceMaxCallDuration', 'voiceRetryDelay',
        'voiceMaxLinesSetting', 'voiceWrapUpTime', 'voiceOnMachineAction',
        'voiceCooldownHours', 'voiceDailyMaxContact',
        // Config tab
        'calendar_select', 'smsBuyNumberType',
        'a2pPayBrandType', 'a2pBrandType', 'a2pUseCase',
        // Logs tab
        'logFilterType', 'logFilterStatus',
        // Agency dashboard
        'agCallAgentFilter', 'agActivityAgent',
        // God mode admin
        'gmLogsFilter',
    ];

    /* ── Selects populated dynamically — init now (empty/stub), refresh later ── */
    const DYNAMIC_IDS = [
        'dialerPipelineFilter',   // populated after /voice/pipelines fetch
        'dialerStageFilter',      // populated after pipeline chosen
        'dlrMobPipelineFilter',   // mobile responsive dup
        'dlrMobStageFilter',      // mobile responsive dup
        'iosCalendarPicker',      // populated after /api/fetch-calendars
        'audioInputDevice',       // populated from WebAudio enumerateDevices
        'audioOutputDevice',
        'voiceDialAttempts',      // appears in both settings panel + voice tab
    ];

    function initAll() {
        STATIC_IDS.forEach(function (id) {
            const el = document.getElementById(id);
            if (el) _init(el);
        });
        DYNAMIC_IDS.forEach(function (id) {
            const el = document.getElementById(id);
            if (el) _init(el);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAll);
    } else {
        initAll();
    }
})();
