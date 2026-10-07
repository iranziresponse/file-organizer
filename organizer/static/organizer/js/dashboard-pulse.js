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
        renderController(data.controller, data.controller_unread_count);
        setRefreshState(data.activity_error ? 'partial' : 'live');
    }

    function renderController(items, unreadCount) {
        var grid = document.querySelector('[data-controller-items]');
        var inbox = document.querySelector('[data-controller-unread]');
        if (!grid || !Array.isArray(items)) return;

        grid.replaceChildren();
        grid.hidden = items.length === 0;
        items.forEach(function (item) {
            var card = el('a', 'workspace-controller__card is-' + (item.state || 'live'));
            card.href = item.url || '#';
            card.setAttribute('data-controller-kind', item.kind || 'update');
            if (item.external && /^https:\/\//i.test(card.href)) {
                card.target = '_blank';
                card.rel = 'noopener noreferrer';
            }
            var top = el('span', 'workspace-controller__top');
            var icon = el('span', 'workspace-controller__icon');
            icon.setAttribute('aria-hidden', 'true');
            var glyphs = {
                schedule: 'M7.5 3.5v3M16.5 3.5v3M4 9.5h16M8 13h3M8 16h6',
                deadline: 'M12 7.5v5l3.2 2M9 3.5h6',
                files: 'M3.8 12h16.4',
                review: 'm5 12 2.2 2.2L11 10M13.5 11H19',
                resource: 'm10 9 5 3-5 3V9Z',
                storage: 'M8.5 14h7',
                connection: 'M9 8V4m6 4V4M7 8h10v4a5 5 0 0 1-5 5v3',
                notifications: 'M18 9a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9ZM10 21h4'
            };
            var iconPaths = {
                schedule: 'M3.5 5h17v15.5h-17z',
                deadline: 'M3.5 12.5a8.5 8.5 0 1 0 17 0a8.5 8.5 0 1 0-17 0',
                files: 'M3.5 7.5h6l2 2h9v8.8a2.2 2.2 0 0 1-2.2 2.2H5.7a2.2 2.2 0 0 1-2.2-2.2V7.5Z',
                review: 'M5 19h14a1.5 1.5 0 0 0 1.5-1.5v-11A1.5 1.5 0 0 0 19 5H5a1.5 1.5 0 0 0-1.5 1.5v11A1.5 1.5 0 0 0 5 19Z',
                resource: 'M3.5 5h17v14h-17z',
                storage: 'M6.5 18.5h11a4 4 0 0 0 .3-8 6 6 0 0 0-11.5-1.2 4.6 4.6 0 0 0 .2 9.2Z',
                connection: 'M7 8h10v4a5 5 0 0 1-10 0V8Z',
                notifications: 'M18 9a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9Z'
            };
            var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.setAttribute('viewBox', '0 0 24 24');
            svg.setAttribute('fill', 'none');
            var shape = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            shape.setAttribute('d', iconPaths[item.kind] || iconPaths.notifications);
            svg.appendChild(shape);
            if (glyphs[item.kind]) {
                var detail = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                detail.setAttribute('d', glyphs[item.kind]);
                svg.appendChild(detail);
            }
            icon.appendChild(svg);
            top.appendChild(icon);
            top.appendChild(el('span', 'workspace-controller__kind', item.kind || 'Update'));
            top.appendChild(el('span', 'workspace-controller__status'));
            card.appendChild(top);
            card.appendChild(el('strong', '', item.title || 'Workspace update'));
            card.appendChild(el('span', 'workspace-controller__detail', item.detail || ''));
            card.appendChild(el('span', 'workspace-controller__action', item.action || 'Open'));
            grid.appendChild(card);
        });

        var quiet = document.querySelector('.workspace-controller__quiet');
        if (quiet) quiet.hidden = items.length > 0;
        if (inbox) {
            var count = Number(unreadCount) || 0;
            inbox.replaceChildren(document.createTextNode('Updates and alerts'));
            if (count) inbox.appendChild(el('span', '', String(count)));
        }
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
