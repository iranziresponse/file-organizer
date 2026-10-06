// Keeps the dashboard's Tier 1 live strip (#pulse-strip) fresh from
// GET /api/pulse/ -- the same dict organizer.core.pulse.get_snapshot()
// server-rendered the strip from on first paint, so a poll never looks
// different from a hard reload.
//
// Cadence: one consolidated poll, 5s while anything is running, 30s when
// everything is idle, and skipped entirely while the window is hidden
// (minimized to tray) -- the same document.hidden guard status-bar.js
// already uses. This replaces what would otherwise be a third fixed-rate
// timer on the page.
(function () {
    var strip = document.getElementById('pulse-strip');
    if (!strip || !strip.hasAttribute('data-pulse-strip')) {
        return;
    }
    var refresh = document.getElementById('pulse-refresh');
    var refreshLabel = refresh && refresh.querySelector('[data-pulse-refresh-label]');

    var POLL_ACTIVE_MS = 5000;
    var POLL_IDLE_MS = 30000;
    var timerId = null;
    var requestInFlight = false;
    // Seeded from the server-rendered strip so the first poll waits the
    // right amount of time instead of always hammering at 5s.
    var lastActive = strip.getAttribute('data-pulse-active') === '1';

    function setRefreshState(state) {
        if (!refresh || !refreshLabel || refresh.getAttribute('data-state') === state) {
            return;
        }
        refresh.setAttribute('data-state', state);
        refresh.classList.toggle('is-error', state === 'error' || state === 'partial');
        refreshLabel.textContent = state === 'error'
            ? 'Live updates unavailable'
            : state === 'partial'
                ? 'Activity unavailable'
                : state === 'updating'
                    ? 'Checking…'
                    : 'Live · just now';
    }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }

    function badge(kind, data, forceActive) {
        var a = el('a', 'pulse-badge is-' + (forceActive ? 'active' : (data.state || 'active')));
        a.href = data.url || '#';
        a.setAttribute('data-badge', kind);
        if (data.indeterminate) {
            a.appendChild(el('span', 'pulse-badge__spinner'));
        } else {
            a.appendChild(el('span', 'pulse-badge__dot'));
        }
        a.lastChild.setAttribute('aria-hidden', 'true');
        a.appendChild(el('span', 'pulse-badge__label', data.label || ''));
        var meta = data.detail;
        if (!meta && kind === 'in_flight' && data.count > 1) {
            meta = data.count + ' running';
        }
        if (meta) {
            var m = el('span', 'pulse-badge__meta', meta);
            m.setAttribute('data-tabular', '');
            a.appendChild(m);
        }
        return a;
    }

    function render(data) {
        if (!data || !data.has_profile) {
            return;
        }
        if (data.pulse_error) {
            setRefreshState('error');
            return;
        }
        var next = document.createDocumentFragment();
        if (data.sorting) next.appendChild(badge('sorting', data.sorting, false));
        if (data.in_flight) next.appendChild(badge('in_flight', data.in_flight, true));
        if (data.lecture) next.appendChild(badge('lecture', data.lecture, false));
        if (data.needs_you) next.appendChild(badge('needs_you', data.needs_you, true));
        if (data.deadline) next.appendChild(badge('deadline', data.deadline, false));

        strip.innerHTML = '';
        strip.appendChild(next);
        lastActive = !!data.active;
        strip.setAttribute('data-pulse-active', lastActive ? '1' : '0');

        // Same consolidated poll feeds the activity film, so the page
        // never grows a second timer for it.
        if (!data.activity_error && data.activity && window.OrchActivityFilm) {
            window.OrchActivityFilm.update(data.activity);
        }
        setRefreshState(data.activity_error ? 'partial' : 'live');
    }

    function scheduleNext() {
        timerId = setTimeout(tick, lastActive ? POLL_ACTIVE_MS : POLL_IDLE_MS);
    }

    function tick() {
        if (requestInFlight) {
            return;
        }
        if (document.hidden) {
            // Same reasoning as status-bar.js: keep the loop alive but do
            // no work while nobody is looking at the page.
            scheduleNext();
            return;
        }
        if (refresh && refresh.getAttribute('data-state') === 'error') {
            setRefreshState('updating');
        }
        requestInFlight = true;
        fetch('/api/pulse/', { credentials: 'same-origin' })
            .then(function (r) {
                if (!r.ok) {
                    throw new Error('Live dashboard request failed with HTTP ' + r.status);
                }
                return r.json();
            })
            .then(render)
            .catch(function (error) {
                console.warn('Orch could not refresh live dashboard data.', error);
                setRefreshState('error');
            })
            .then(function () {
                requestInFlight = false;
                scheduleNext();
            });
    }

    if (refresh) {
        refresh.addEventListener('click', function () {
            if (timerId) clearTimeout(timerId);
            setRefreshState('updating');
            tick();
        });
    }

    document.addEventListener('visibilitychange', function () {
        if (!document.hidden) {
            if (timerId) clearTimeout(timerId);
            setRefreshState('updating');
            tick();
        }
    });

    scheduleNext();
})();
