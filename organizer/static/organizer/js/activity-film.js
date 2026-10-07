// The activity "film" -- the reverse-chronological stream on the dashboard.
// It does not run its own timer: dashboard-pulse.js's one consolidated poll
// carries the rows (payload.activity) and hands them here via
// window.OrchActivityFilm.update(rows). This module only owns the DOM diff,
// the one-item slide+fade for genuinely new rows, and the screen-reader
// story.
//
// Accessibility:
//   - #activity-list is role="log" aria-live="off" by default; the
//     "Announce new activity" checkbox flips it to polite (choice
//     remembered in localStorage, matching orch-theme et al.).
//   - #activity-announcer is a always-polite region that gets a THROTTLED
//     count ("3 new activity items"), never row text, at most once / 30s,
//     regardless of the checkbox.
//   - New rows animate only where CSS allows it (the .is-entering keyframe
//     lives under prefers-reduced-motion: no-preference), so reduced-motion
//     users get an instant insert.
(function () {
    var list = document.getElementById('activity-list');
    var announcer = document.getElementById('activity-announcer');
    var toggle = document.getElementById('activity-announce-toggle');
    if (!list) {
        return;
    }

    var MAX_ROWS = 10;
    var ANNOUNCE_THROTTLE_MS = 30000;
    var announceKey = list.getAttribute('data-announce-key') || 'orch-activity-announce';

    var lastAnnounceAt = 0;
    var pendingNewCount = 0;
    var announceTimer = null;

    // --- announce toggle -------------------------------------------------
    function readToggle() {
        try {
            return window.localStorage.getItem(announceKey) === '1';
        } catch (err) {
            return false;
        }
    }
    function writeToggle(on) {
        try {
            window.localStorage.setItem(announceKey, on ? '1' : '0');
        } catch (err) { /* ignore */ }
    }
    function applyToggle(on) {
        list.setAttribute('aria-live', on ? 'polite' : 'off');
    }
    if (toggle) {
        var saved = readToggle();
        toggle.checked = saved;
        applyToggle(saved);
        toggle.addEventListener('change', function () {
            writeToggle(toggle.checked);
            applyToggle(toggle.checked);
        });
    }

    // --- throttled summary announcement --------------------------------
    function flushAnnouncement() {
        announceTimer = null;
        if (pendingNewCount <= 0 || !announcer) {
            return;
        }
        var n = pendingNewCount;
        pendingNewCount = 0;
        lastAnnounceAt = Date.now();
        announcer.textContent = n + ' new activity item' + (n === 1 ? '' : 's');
    }
    function noteNew(count) {
        if (count <= 0) return;
        pendingNewCount += count;
        var wait = Math.max(0, ANNOUNCE_THROTTLE_MS - (Date.now() - lastAnnounceAt));
        if (wait === 0) {
            flushAnnouncement();
        } else if (!announceTimer) {
            announceTimer = setTimeout(flushAnnouncement, wait);
        }
    }

    // --- row rendering -------------------------------------------------
    var ICONS = {
        file: '<path d="M7 3h7l5 5v13H7z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M14 3v5h5" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>',
        alert: '<path d="M12 4 3 19h18L12 4Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M12 10v4M12 17h.01" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>',
        bell: '<path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4l2-2Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M10 20a2 2 0 0 0 4 0" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
        check: '<circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="1.7"/><path d="m8.5 12 2.5 2.5 4.5-5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>',
        clock: '<circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="1.7"/><path d="M12 7.5V12l3 2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>'
    };

    function buildRow(row) {
        var li = document.createElement('li');
        li.className = 'activity-row activity-row--' + row.kind;
        li.setAttribute('data-key', row.key);

        var iconWrap = document.createElement('span');
        iconWrap.className = 'activity-row__icon';
        iconWrap.setAttribute('aria-hidden', 'true');
        iconWrap.innerHTML = '<svg viewBox="0 0 24 24" fill="none">' + (ICONS[row.icon] || ICONS.clock) + '</svg>';
        li.appendChild(iconWrap);

        var a = document.createElement('a');
        a.className = 'activity-row__text';
        a.href = row.url || '#';
        a.textContent = row.text;
        li.appendChild(a);

        var t = document.createElement('time');
        t.className = 'activity-row__time';
        t.setAttribute('data-tabular', '');
        if (row.when_iso) t.setAttribute('datetime', row.when_iso);
        t.textContent = row.when_short;
        li.appendChild(t);

        return li;
    }

    function currentKeys() {
        var keys = {};
        list.querySelectorAll('.activity-row[data-key]').forEach(function (li) {
            keys[li.getAttribute('data-key')] = true;
        });
        return keys;
    }

    function update(rows) {
        if (!Array.isArray(rows)) return;
        var known = currentKeys();
        var isFirstFill = list.querySelector('.activity-row--empty') || !list.querySelector('.activity-row');

        // rows arrive newest-first; insert oldest-of-the-new first so the
        // final DOM order stays newest-at-top.
        var fresh = [];
        for (var i = 0; i < rows.length; i++) {
            if (!known[rows[i].key]) fresh.push(rows[i]);
        }

        if (fresh.length) {
            var emptyRow = list.querySelector('.activity-row--empty');
            if (emptyRow) emptyRow.remove();
        }

        for (var j = fresh.length - 1; j >= 0; j--) {
            var li = buildRow(fresh[j]);
            if (!isFirstFill) li.classList.add('is-entering');
            list.insertBefore(li, list.firstChild);
        }

        // Trim to the cap.
        var all = list.querySelectorAll('.activity-row');
        for (var k = all.length - 1; k >= MAX_ROWS; k--) {
            all[k].remove();
        }

        if (!isFirstFill && fresh.length) {
            noteNew(fresh.length);
        }
    }

    window.OrchActivityFilm = { update: update };
})();
