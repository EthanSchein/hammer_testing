"""
Airflow plugin: add a "pgAdmin" entry to the nav / sidebar.

A nav item with ``destination="nav"``, an absolute ``href``, and no ``url_route``
renders as a plain new-tab link (``<a href target="_blank">``): the React UI
computes ``isExternal = (url_route is None)`` and only iframes items that have a
``url_route`` (the ``plugin/<route>`` route). The backend keeps it too --
get_plugin_info dumps ``plugin.external_views``, which retains url_route-less
items. So clicking "pgAdmin" opens it directly in a new tab, never hitting the
iframe route (which pgAdmin's ``X-Frame-Options: SAMEORIGIN`` would blank out).

pgAdmin listens on port 5050 (see PGADMIN_SETUP.md). The link is rendered in
your browser, which reaches pgAdmin through the same SSH tunnel you use for
the Airflow UI, so ``localhost:5050`` is the correct host from the browser's
point of view. Override with the ``PGADMIN_URL`` env var if it lives elsewhere.
"""

from __future__ import annotations

import base64
import os
import socket
import urllib.request

from airflow.plugins_manager import AirflowPlugin

# The nav link must point at whatever port pgAdmin actually came up on. pgAdmin
# auto-increments off a busy port (5050 -> 5051 -> ...), so we can't hardcode
# it. The api_server that loads this plugin runs on the SAME host as pgAdmin,
# so we just ASK pgAdmin directly: probe localhost ports and find the live one.
# This is immune to a stale/cross-host file or a wrong env var.
#
# Resolution order:
#   1. PGADMIN_URL env var                     (explicit override, always wins)
#   2. probe localhost for a live pgAdmin      (the normal, self-correcting path)
#   3. the launcher's recorded URL file        (fallback if the probe finds none)
#   4. http://localhost:$PGADMIN_PORT/browser/ (last resort, default 5050)
_PGADMIN_URL_FILE = os.path.expanduser("~/.pgadmin/airflow_pgadmin_url")


def _is_pgadmin(port: int) -> bool:
    """True if a pgAdmin server actually answers on 127.0.0.1:<port>."""
    try:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return False  # nothing listening -> skip fast
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/browser/", timeout=3) as r:
            return "pgadmin" in r.read(8192).decode("utf-8", "ignore").lower()
    except Exception:
        return False


def _live_pgadmin_url():
    """PGADMIN_URL, else the URL of a pgAdmin that answers on this host, else None."""
    override = os.environ.get("PGADMIN_URL")
    if override:
        return override
    base = int(os.environ.get("PGADMIN_PORT", "5050"))
    for port in range(base, base + 12):
        if _is_pgadmin(port):
            return f"http://localhost:{port}/browser/"
    return None


def _resolve_pgadmin_url() -> str:
    live = _live_pgadmin_url()
    if live:
        return live
    base = int(os.environ.get("PGADMIN_PORT", "5050"))
    try:
        with open(_PGADMIN_URL_FILE) as f:
            url = f.read().strip()
        if url:
            return url
    except OSError:
        pass
    return f"http://localhost:{base}/browser/"


# The lite stack starts no pgAdmin, so it links only to one that answers.
_LITE = os.environ.get("SLEDGE_LITE", "").strip().lower() not in ("", "0", "false", "no", "off")
PGADMIN_URL = _live_pgadmin_url() if _LITE else _resolve_pgadmin_url()

# Inline database-cylinder glyph (Postgres blue) as a data URI so the nav item
# has an icon without depending on any external asset.
_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
    "stroke='#336791' stroke-width='2' stroke-linecap='round' "
    "stroke-linejoin='round'>"
    "<ellipse cx='12' cy='5' rx='9' ry='3'/>"
    "<path d='M21 12c0 1.66-4 3-9 3s-9-1.34-9-3'/>"
    "<path d='M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5'/></svg>"
)
_PGADMIN_ICON = "data:image/svg+xml;base64," + base64.b64encode(_SVG.encode()).decode()


class PgAdminLinkPlugin(AirflowPlugin):
    name = "pgadmin_link"
    external_views = [
        {
            "name": "pgAdmin",
            "href": PGADMIN_URL,
            # No url_route: the UI computes isExternal = (url_route is None) and
            # renders this as a plain <a href target="_blank"> new-tab link
            # straight to pgAdmin, instead of the "plugin/<route>" iframe that
            # pgAdmin's X-Frame-Options: SAMEORIGIN blanks out.
            "destination": "nav",
            "icon": _PGADMIN_ICON,
        }
    ] if PGADMIN_URL else []
