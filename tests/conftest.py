import hashlib
import os
import sys

import pytest

from hammer.logging import HammerVLSILogging
from hammer.vlsi import rtl_check

# The CLI driver checks that hammer-shell-test is on PATH.  Running
# `.venv/bin/python -m pytest` without activating the venv leaves its bin/ off
# PATH and fails ~70 driver tests with "hammer-shell does not appear to be on
# the path", so put the interpreter's own bin/ first.
_BIN = os.path.dirname(sys.executable)
if _BIN not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _BIN + os.pathsep + os.environ.get("PATH", "")


@pytest.fixture(autouse=True)
def _restore_logging_callbacks():
    saved = list(HammerVLSILogging.callbacks)
    yield
    HammerVLSILogging.callbacks = saved


@pytest.fixture(autouse=True)
def _no_code_fingerprint_memo(monkeypatch):
    monkeypatch.setenv("HAMMER_CODE_FP_CACHE", "off")


@pytest.fixture(autouse=True)
def _no_rtl_fingerprint_memo(monkeypatch):
    monkeypatch.setenv("HAMMER_RTL_FP_CACHE", "off")


_PLACEHOLDER_RTL_TEST_MODULES = {"test_cli_driver", "test_flowgraph", "test_force_rerun", "test_rerun_messages"}


def _byte_hash_digest(paths, include_dirs=(), defines=(), top_module=None):
    file_hashes = []
    for path in sorted({os.path.realpath(p) for p in paths}):
        with open(path, "rb") as f:
            file_hashes.append(hashlib.sha256(f.read()).hexdigest() + "\n")
    return hashlib.sha256("".join(file_hashes).encode("utf-8")).hexdigest(), []


@pytest.fixture(autouse=True)
def _no_sledgehammer_hand_off(request, monkeypatch):
    """Keep sledgehammer_cli.main() from re-executing into a real checkout's venv; classes with real_hand_off = True opt out."""
    if not getattr(request.cls, "real_hand_off", False):
        from hammer.shell import sledgehammer_cli
        monkeypatch.setattr(sledgehammer_cli, "_delegate_to_checkout_venv", lambda: None)


@pytest.fixture(autouse=True)
def _byte_hash_placeholder_rtl(request, monkeypatch):
    if request.module.__name__.rsplit(".", 1)[-1] in _PLACEHOLDER_RTL_TEST_MODULES:
        monkeypatch.setattr(rtl_check, "digest_units", _byte_hash_digest)
        monkeypatch.setattr(rtl_check, "digest_files", _byte_hash_digest)


@pytest.fixture(autouse=True)
def _no_developer_stack_env(monkeypatch):
    """Keep a developer's stack env file (~/.sledgehammer/env.sh, or a stack_env.sh above the cwd) out of the CLIs under test; only SLEDGE_ENV_FILE is honored."""
    from hammer.shell import sledgehammer_cli
    monkeypatch.delenv("SLEDGE_ENV_FILE", raising=False)
    monkeypatch.setattr(sledgehammer_cli, "_find_stack_env",
                        lambda: os.environ.get("SLEDGE_ENV_FILE") if os.path.isfile(os.environ.get("SLEDGE_ENV_FILE", "")) else None)


@pytest.fixture(autouse=True)
def _no_developer_postgres_target(tmp_path_factory, monkeypatch):
    """Keep this checkout's own .sledgehammer/postgres.env out of the code under test."""
    from hammer.vlsi import pd_store
    monkeypatch.setattr(pd_store, "SECRETS_FILE",
                        str(tmp_path_factory.getbasetemp() / "no-checkout-secrets" / "airflow-secrets.env.gpg"))
