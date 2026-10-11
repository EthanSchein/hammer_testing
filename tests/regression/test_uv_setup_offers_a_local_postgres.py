import os
import shutil
import signal
import subprocess
import sys

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="uv_setup.sh needs bash and a pty")

# Test names stay clear of conda, mamba, miniforge and pixi: uv_setup.sh drops PATH entries containing them.
STUBS = {
    "bin/uv": '#!/bin/sh\n[ "$1" = --version ] && echo "uv 0.0.0 (stub)"\nexit 0\n',
    "home/pg_local/usr/bin/pg_config": "#!/bin/sh\necho 'PostgreSQL 16 (stub)'\n",
    "checkout/tools/slang/slang": "#!/bin/sh\necho 'slang version 11.0.0 (stub)'\n",
    "checkout/.venv/bin/airflow": "#!/bin/sh\necho 3.1.0\n",
    "checkout/.venv/bin/sledgehammer": "#!/bin/sh\nexit 0\n",
}


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


def _install(tmp_path, tools, extra_path=(), answers=None):
    log = tmp_path / "calls.log"
    scripts = [n for n in sorted(os.listdir(fake_postgres.REPO / "scripts"))
               if os.path.isfile(fake_postgres.REPO / "scripts" / n)]
    checkout = fake_postgres.checkout(tmp_path / "checkout", *scripts, venv=True, log=log)
    for rel, text in STUBS.items():
        fake_postgres.executable(tmp_path / rel, text)
    fake_postgres.executable(tmp_path / "bin" / "gpg", fake_postgres.GPG)
    (checkout / ".venv" / "bin" / "activate").write_text(f'export PATH="{checkout}/.venv/bin:$PATH"\n')
    home = tmp_path / "home"
    (home / "libnsl_local" / "usr" / "lib64").mkdir(parents=True)
    (home / "libnsl_local" / "usr" / "lib64" / "libnsl.so.1").write_text("")
    (home / ".bashrc").write_text("")
    secrets = tmp_path / "secrets.env.gpg"
    env = {"HOME": str(home), "USER": "alice", "TMPDIR": str(tmp_path),
           "PATH": os.pathsep.join((str(tmp_path / "bin"), *extra_path, tools)),
           "SLEDGE_NO_LDAP": "1", "SLEDGE_NO_PLUGINS": "1", "SLEDGE_SECRETS_FILE": str(secrets)}
    argv = ["bash", str(checkout / "scripts" / "uv_setup.sh")]
    if answers is None:
        res = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=120)
        out = res.stdout + res.stderr
        rc = res.returncode
    else:
        import pty
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(argv, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, env=env, start_new_session=True)
            os.write(master, answers.encode())
            try:
                out, _ = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                out, _ = proc.communicate()
                pytest.fail(f"uv_setup.sh waited for an answer it was not given:\n{out}")
            rc = proc.returncode
        finally:
            os.close(slave)
            os.close(master)
    assert rc == 0, out
    written = dict(line.split("=", 1) for line in secrets.read_text().splitlines()) if secrets.exists() else {}
    return out, written, log, home / ".sledgehammer" / "postgres"


def _port(root) -> str:
    return dict(line.split(" = ", 1) for line in (root / "data" / "postgresql.conf").read_text().splitlines()
                if line.startswith("port = "))["port"]


def test_pressing_enter_throughout_sets_up_postgres_and_secrets_for_it(tmp_path, tools) -> None:
    pg_bin = fake_postgres.postgres(tmp_path / "pgbin", tmp_path / "calls.log")
    out, written, log, root = _install(tmp_path, tools, extra_path=(pg_bin,), answers="\n\n")
    assert "set up a local Postgres for SledgeHammer now? [Y/n]" in out
    assert len(fake_postgres.calls(log, "initdb")) == 1
    password = (root / "password").read_text().strip()
    assert written["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] == \
        f"postgresql+psycopg2://alice:{password}@localhost:{_port(root)}/airflow_alice"
    assert written["HAMMER_PG_PASSWORD"] == password
    assert written["SLEDGE_PG_DIR"] == str(root)
    assert f"schema alice@localhost:{_port(root)}/sledgehammer_studio" in fake_postgres.calls(log, "schema")
    assert password not in out


def test_postgres_installs_through_a_package_manager_the_cleanup_took_off_path(tmp_path, tools) -> None:
    pg_bin = fake_postgres.postgres(tmp_path / "pgbin", tmp_path / "calls.log")
    manager = fake_postgres.conda(tmp_path / "miniforge3" / "bin", tmp_path / "calls.log", pg_bin)
    out, written, log, root = _install(tmp_path, tools, extra_path=(manager,), answers="\n\n")
    assert "neutralized for a clean build" in out
    assert len(fake_postgres.calls(log, "conda create")) == 1
    assert (root / "bindir").read_text().strip() == str(tmp_path / "home" / ".sledgehammer" / "pg-env" / "bin")
    assert written["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"].endswith(f"@localhost:{_port(root)}/airflow_alice")


def test_no_leaves_the_secrets_prompts_as_they_were(tmp_path, tools) -> None:
    pg_bin = fake_postgres.postgres(tmp_path / "pgbin", tmp_path / "calls.log")
    out, written, log, root = _install(tmp_path, tools, extra_path=(pg_bin,), answers="n\n\n\n\n\n\npw\npw\n")
    assert "skipped. Create one later: ./scripts/sledge-local-postgres.sh" in out
    assert fake_postgres.calls(log, "initdb") == []
    assert written["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] == \
        "postgresql+psycopg2://alice:pw@localhost:5544/airflow_alice"


def test_a_failed_local_setup_falls_back_to_the_secrets_prompts(tmp_path, tools) -> None:
    out, written, log, root = _install(tmp_path, tools, answers="\n\n\n\n\n\npw\npw\n")
    assert "no Postgres found" in out
    assert "  the local Postgres setup did not finish; the secrets step below asks which Postgres to use." \
        in out.splitlines()
    assert written["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] == \
        "postgresql+psycopg2://alice:pw@localhost:5544/airflow_alice"
    assert "SLEDGE_PG_DIR" not in written


def test_without_a_tty_the_step_names_the_script(tmp_path, tools) -> None:
    pg_bin = fake_postgres.postgres(tmp_path / "pgbin", tmp_path / "calls.log")
    out, written, log, root = _install(tmp_path, tools, extra_path=(pg_bin,))
    assert "  no tty, skipping. Create one later: ./scripts/sledge-local-postgres.sh" in out.splitlines()
    assert fake_postgres.calls(log, "initdb") == []
    assert written == {}
