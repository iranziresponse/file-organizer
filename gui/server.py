"""Runs the Django dashboard on a background thread inside the same process
as the tray app, so there is no separate `manage.py runserver` step for the
packaged exe.

This calls Django's internal WSGI plumbing directly (the same handler
`manage.py runserver` ends up using) instead of going through the
`runserver` management command itself. `runserver` reruns the full system
check framework and a pending-migrations check on every launch -- both
already-redundant with the explicit migrate call in gui/app.py -- plus
argument parsing and banner printing. On a real machine that overhead
measured close to half of this thread's total time to first response, and
it's pure startup latency the user sits through on every launch, not a
one-time cost.
"""

import logging
import socket
import threading

from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.servers.basehttp import run as run_wsgi_server
from django.core.servers.basehttp import get_internal_wsgi_application

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8765


def _select_dashboard_port(host, preferred_port):
    """Use the standard port when available, otherwise select a free local
    port so an unrelated service cannot prevent Orch from opening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, preferred_port))
            return preferred_port
        except OSError:
            probe.bind((host, 0))
            return probe.getsockname()[1]


def start_dashboard_server(host=DASHBOARD_HOST, port=DASHBOARD_PORT):
    # Deliberately not in organizer.apps.OrganizerConfig.ready(): ready()
    # fires for every management command, including `manage.py test` --
    # and it fires before the test runner switches to the test database,
    # so a DB write there would land on the real db.sqlite3, not a test
    # fixture. This is the one place that's guaranteed to only run when
    # the real app is actually starting up.
    global DASHBOARD_PORT

    port = _select_dashboard_port(host, port)
    DASHBOARD_PORT = port

    from organizer.core import jobs
    jobs.mark_stale_tasks_as_interrupted()

    def _serve():
        # StaticFilesHandler wrapping the plain WSGI app is exactly what
        # `runserver --insecure` does under the hood (see Django's
        # contrib.staticfiles runserver override) -- Orch has no separate
        # web server in front of it, so this is still how static assets
        # get served, in dev and in the packaged exe.
        handler = StaticFilesHandler(get_internal_wsgi_application())
        try:
            run_wsgi_server(host, port, handler, threading=True)
        except OSError:
            logging.getLogger(__name__).exception(
                "Orch's dashboard server failed to bind %s:%s", host, port
            )

    thread = threading.Thread(target=_serve, daemon=True, name="organizer-dashboard")
    thread.start()
    return thread


def dashboard_url(host=DASHBOARD_HOST, port=None):
    if port is None:
        port = DASHBOARD_PORT
    return f"http://{host}:{port}/"
