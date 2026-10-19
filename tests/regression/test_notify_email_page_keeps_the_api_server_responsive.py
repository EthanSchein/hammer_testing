import asyncio
import functools
import importlib.util
import inspect
import sys
import threading
import types
from pathlib import Path

import pytest

from hammer.vlsi import pd_store

REPO = Path(__file__).resolve().parents[2]
USER = types.SimpleNamespace(username="alice", email="", get_name=lambda: "alice")


class _App:
    """Records each route the way FastAPI registers it."""

    def __init__(self):
        self.routes = []

    def _route(self, method, path):
        def register(fn):
            self.routes.append((method, path, fn))
            return fn
        return register

    def get(self, path, **_):
        return self._route("GET", path)

    def post(self, path, **_):
        return self._route("POST", path)


class _Request:
    def __init__(self, body):
        self._body = body

    async def body(self):
        return self._body


class _SlowDatabase:
    """Stands in for pd_store: every call holds until released, as a connect to a server that is down does."""

    def __init__(self):
        self.entered = threading.Event()
        self.left = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def call(self, name):
        def slow(*args):
            self.calls.append((name,) + args)
            self.entered.set()
            self.release.wait(5)
            self.left.set()
        return slow


async def _to_thread(fn, *args, **kwargs):
    return await asyncio.get_running_loop().run_in_executor(None, functools.partial(fn, *args, **kwargs))


def _plugin(monkeypatch):
    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI, fastapi.Request = _App, object
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "fastapi.concurrency", types.SimpleNamespace(run_in_threadpool=_to_thread))
    monkeypatch.setitem(sys.modules, "fastapi.responses",
                        types.SimpleNamespace(HTMLResponse=object, JSONResponse=lambda content: content))
    monkeypatch.setitem(sys.modules, "airflow", types.ModuleType("airflow"))
    monkeypatch.setitem(sys.modules, "airflow.plugins_manager",
                        types.SimpleNamespace(AirflowPlugin=type("AirflowPlugin", (), {})))
    monkeypatch.setitem(sys.modules, "airflow.api_fastapi.core_api.security",
                        types.SimpleNamespace(GetUserDep=object))
    spec = importlib.util.spec_from_file_location("notify_email_plugin_under_test",
                                                  str(REPO / "plugins" / "notify_email_plugin.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _serve_while_watching(route, db, kwargs):
    """Serves a route as FastAPI does and says whether the event loop got control while the database call was held."""
    if inspect.iscoroutinefunction(route):
        served = asyncio.ensure_future(route(**kwargs))
    else:
        served = asyncio.ensure_future(_to_thread(route, **kwargs))
    while not db.entered.is_set() and not served.done():
        await asyncio.sleep(0.01)
    free = not db.left.is_set()
    db.release.set()
    return free, await served


@pytest.mark.parametrize("body, stored, current", [
    (b"email=alice%40example.com", ("set_notify_email", "alice", "alice@example.com"), "alice@example.com"),
    (b"email=", ("delete_notify_email", "alice"), ""),
])
def test_no_notify_email_route_holds_the_event_loop_during_a_database_call(monkeypatch, body, stored, current) -> None:
    app = _plugin(monkeypatch).app
    assert {(method, path) for method, path, _ in app.routes} >= {("GET", "/whoami"), ("POST", "/save")}
    for method, path, route in app.routes:
        db = _SlowDatabase()
        for name in ("get_notify_email", "set_notify_email", "delete_notify_email"):
            monkeypatch.setattr(pd_store, name, db.call(name))
        args = {"request": _Request(body), "user": USER}
        kwargs = {name: args[name] for name in inspect.signature(route).parameters}
        free, result = asyncio.run(_serve_while_watching(route, db, kwargs))
        assert free, f"{method} {path} holds the api-server's event loop while it waits on the database"
        if path == "/save":
            assert db.calls == [stored]
            assert result == {"ok": True, "current": current}
