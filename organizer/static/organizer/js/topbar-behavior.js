    (function () {
        var toggle = document.getElementById('theme-toggle');
        var label = document.getElementById('theme-label');
        if (!toggle || !label) return;

        function apply(theme) {
            document.documentElement.dataset.theme = theme;
            window.localStorage.setItem('orch-theme', theme);
            label.textContent = theme === 'light' ? 'Light' : 'Dark';
            toggle.setAttribute('aria-pressed', theme === 'light' ? 'true' : 'false');
            toggle.setAttribute('title', theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode');
        }

        apply(document.documentElement.dataset.theme === 'light' ? 'light' : 'dark');

        toggle.addEventListener('click', function () {
            var current = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
            apply(current === 'light' ? 'dark' : 'light');
        });
    })();

    (function () {
        var overlay = document.getElementById('guide-overlay');
        var toggle = document.getElementById('guide-toggle');
        var close = document.getElementById('guide-close');
        if (!overlay || !toggle) return;

        function open() { overlay.hidden = false; }
        function hide() { overlay.hidden = true; }

        toggle.addEventListener('click', open);
        close.addEventListener('click', hide);
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) hide();
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') hide();
        });
    })();

    // Hide the top bar while scrolling down, bring it back on scroll up.
    // Keeps it out of the way on long pages instead of pinning it forever.
    (function () {
        var topbar = document.querySelector('.topbar');
        if (!topbar) return;
        if (topbar.classList.contains('app-roof')) {
            topbar.classList.remove('topbar-hidden');
            return;
        }

        var lastY = window.scrollY;
        var ticking = false;
        var revealThreshold = 40;
        var moveThreshold = 6;

        function onScroll() {
            var currentY = window.scrollY;
            var delta = currentY - lastY;

            if (currentY <= revealThreshold) {
                topbar.classList.remove('topbar-hidden');
            } else if (delta > moveThreshold) {
                topbar.classList.add('topbar-hidden');
            } else if (delta < -moveThreshold) {
                topbar.classList.remove('topbar-hidden');
            }

            lastY = currentY;
            ticking = false;
        }

        window.addEventListener('scroll', function () {
            if (!ticking) {
                window.requestAnimationFrame(onScroll);
                ticking = true;
            }
        }, { passive: true });
    })();

    (function () {
        var rail = document.getElementById('topbar-power');
        var hero = document.querySelector('[data-power-hero]');
        if (!rail || !hero) return;
        var shell = document.body.classList.contains('has-desktop-titlebar') ? document.querySelector('.app-shell') : null;
        var scrollNode = shell || window;

        function setRail(active) {
            document.documentElement.classList.toggle('power-rail-active', active);
            rail.setAttribute('aria-hidden', active ? 'false' : 'true');
        }

        function scrollY() {
            return shell ? shell.scrollTop : window.scrollY;
        }

        function shouldCollapse() {
            var rect = hero.getBoundingClientRect();
            var topLimit = document.body.classList.contains('has-desktop-titlebar') ? 88 : 24;
            return scrollY() > 72 && rect.top < topLimit;
        }

        var ticking = false;
        function update() {
            setRail(shouldCollapse());
            ticking = false;
        }

        if ('IntersectionObserver' in window) {
            var observer = new IntersectionObserver(function () {
                update();
            }, {
                root: null,
                rootMargin: '-72px 0px 0px 0px',
                threshold: [0, 0.2, 0.6, 1]
            });
            observer.observe(hero);
        }

        scrollNode.addEventListener('scroll', function () {
            if (!ticking) {
                window.requestAnimationFrame(update);
                ticking = true;
            }
        }, { passive: true });
        window.addEventListener('resize', update);
        update();
    })();
