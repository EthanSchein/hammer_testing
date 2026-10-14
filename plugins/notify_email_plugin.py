"""Airflow plugin: a self-service "My Notification Email" page.

A logged-in user sets the one address they want completion emails at, or clears
it to opt out. Whether a run actually emails is decided per-run by the toggle on
the trigger form; this page only holds the address.

Auth (Airflow 3.1): GetUserDep reads only the Authorization: Bearer header, not
cookies, and an iframe document load can't attach that header. So the page is
served without auth and its JS reads the SPA token from same-origin localStorage
and calls the guarded /whoami and /save with it. /save takes the uid from the
verified token, never the request body, so a user can only set their own.
"""

from __future__ import annotations

import base64
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from airflow.plugins_manager import AirflowPlugin
from airflow.api_fastapi.core_api.security import GetUserDep


def _uid_and_default(user) -> tuple[str, str]:
    """The logged-in uid plus any address the login carries, offered as a pre-fill default."""
    uid = (getattr(user, "username", None) or user.get_name() or "").strip()
    email = (getattr(user, "email", None) or "").strip()
    return uid, email


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Notification email</title><style>
 body { font-family: system-ui, -apple-system, sans-serif; margin: 2rem; color: #1f2328; }
 h2 { margin: 0 0 .5rem; }
 input[type=email] { width: 22rem; padding: .45rem; font-size: 1rem; }
 button { padding: .5rem 1.1rem; font-size: 1rem; margin-left: .4rem; cursor: pointer; }
 .muted { color: #656d76; font-size: .9rem; }
 .ok { color: #1a7f37; }
 .err { color: #cf222e; }
 #form { display: none; }
</style></head><body>
<h2>My notification email</h2>
<p class="muted">Signed in as <b id="who">&hellip;</b>. Set the address you want a
note at when a flow finishes. Turn the email on or off <b>per run</b> with the
"Email me when this finishes" toggle on the trigger form. Clear the box to opt out
entirely.</p>
<p id="note"></p>
<form id="form">
  <input type="email" id="email" placeholder="you@berkeley.edu">
  <button type="submit">Save</button>
</form>
<script>
(function () {
  function token() {
    try {
      var t = (window.parent && window.parent.localStorage && window.parent.localStorage.getItem('token'))
              || localStorage.getItem('token');
      if (t) return t;
    } catch (e) {}
    var m = document.cookie.match(/(?:^|; )_token=([^;]+)/);
    return m ? m[1] : null;
  }
  var who = document.getElementById('who'),
      note = document.getElementById('note'),
      form = document.getElementById('form'),
      email = document.getElementById('email');
  function setNote(msg, cls) { note.textContent = msg; note.className = cls || ''; }
  var T = token();
  if (!T) {
    who.textContent = '(unknown)';
    setNote('Please sign in to the Airflow UI first, then reload this page.', 'err');
    return;
  }
  var auth = { 'Authorization': 'Bearer ' + T };
  fetch('whoami', { headers: auth }).then(function (r) {
    if (r.status === 401 || r.status === 403) {
      throw new Error('Your Airflow session expired -- reload the Airflow UI and try again.');
    }
    if (!r.ok) throw new Error('Could not load your profile (HTTP ' + r.status + ').');
    return r.json();
  }).then(function (d) {
    who.textContent = d.uid || '(unknown)';
    email.value = d.current || d.email || '';
    form.style.display = 'block';
  }).catch(function (e) { who.textContent = '(unknown)'; setNote(e.message, 'err'); });
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var body = 'email=' + encodeURIComponent(email.value.trim());
    fetch('save', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/x-www-form-urlencoded' }, auth),
      body: body
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      if (res.ok && res.j.ok) {
        setNote(res.j.current
          ? 'Saved. Completion emails will go to ' + res.j.current + ' (when the per-run toggle is on).'
          : 'Opted out. You will not get completion emails.', 'ok');
      } else {
        setNote('Could not save: ' + (res.j.error || 'unknown error'), 'err');
      }
    }).catch(function (e) { setNote('Could not save: ' + e.message, 'err'); });
  });
})();
</script>
</body></html>"""


app = FastAPI()


@app.get("/", response_class=HTMLResponse)
def show_form() -> str:
    # Served with NO auth: an iframe document GET can't carry the Bearer header
    # GetUserDep needs, so the page is static and its JS authenticates the
    # /whoami and /save calls instead.
    return _PAGE


@app.get("/whoami")
def whoami(user: GetUserDep):
    from hammer.vlsi import pd_store
    uid, email = _uid_and_default(user)
    try:
        current = pd_store.get_notify_email(uid) or ""
    except Exception:
        current = ""
    return {"uid": uid, "email": email, "current": current}


@app.post("/save")
async def save(request: Request, user: GetUserDep):
    from hammer.vlsi import pd_store
    uid, _ = _uid_and_default(user)  # identity from the validated token, never the body
    body = (await request.body()).decode("utf-8", "ignore")
    email = (parse_qs(body).get("email", [""])[0] or "").strip()
    try:
        if email:
            pd_store.set_notify_email(uid, email)
        else:
            pd_store.delete_notify_email(uid)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return JSONResponse({"ok": True, "current": email})


_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
    "stroke='#673ab7' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
    "<rect x='2' y='4' width='20' height='16' rx='2'/>"
    "<path d='m22 7-10 6L2 7'/></svg>"
)
_ICON = "data:image/svg+xml;base64," + base64.b64encode(_SVG.encode()).decode()


class NotifyEmailPlugin(AirflowPlugin):
    name = "notify_email"
    fastapi_apps = [
        {
            "name": "Notification Email",
            "url_prefix": "/notify-email",
            "app": app,
        }
    ]
    external_views = [
        {
            "name": "My Notification Email",
            "href": "/notify-email/",
            "url_route": "notify-email",
            "destination": "nav",
            "icon": _ICON,
        }
    ]
