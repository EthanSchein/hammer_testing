import os
import shutil
import subprocess
import sys

import pytest

from hammer.vlsi import pd_store
from tests.utils import fake_postgres
from tests.utils.postgres_switch import CONN, OLD_CONN, PASSPHRASE, SECRETS, Sandbox, mode, row

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

CACHE_PW = "t3am c@che"
FIELDS = ["cache.example.edu", "5432", "t1a", "team1_cache", CACHE_PW, CACHE_PW]
USE = "Use cache.example.edu:5432 for the cache? [y/N]: "
SECOND = ("Your cached results, registered workspaces and notification emails stay on barney.example.edu:5544. "
          "Switch anyway? [y/N]: ")
SPLIT = {"HAMMER_PG_HOST": "cache.example.edu", "HAMMER_PG_PORT": "5432", "HAMMER_PG_USER": "t1a",
         "HAMMER_PG_DB": "team1_cache", "SLEDGE_SEPARATE_CACHE": "1"}


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def box(tmp_path, monkeypatch, capsys, tools):
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    sandbox.db.serve("cache.example.edu", 5432, CACHE_PW, "team1_cache")
    sandbox.db.serve("barney.example.edu", 5544, "oldpw-1234", "airflow_alice", "sledgehammer_studio")
    yield sandbox
    sandbox.restore()


def test_the_cache_moves_and_airflow_stays(box) -> None:
    rc, out, err = box.run("cache", answers=[PASSPHRASE, *FIELDS, "y", "y"])
    assert rc == 0, out + err
    assert box.prompts == [
        ("getpass", f"GPG passphrase for {box.secrets}: "),
        ("input", "  host [barney.example.edu]: "),
        ("input", "  port [5544]: "),
        ("input", "  user [alice]: "),
        ("input", "  cache database [sledgehammer_studio]: "),
        ("getpass", "  password: "), ("getpass", "  password again: "),
        ("input", USE), ("input", SECOND),
    ]
    assert "  cache database team1_cache: reachable yes" in out
    assert "  cache    alice@barney.example.edu:5544/sledgehammer_studio\n" \
           "       ->  t1a@cache.example.edu:5432/team1_cache\n" \
           "  Airflow  stays on alice@barney.example.edu:5544/airflow_alice\n" in out
    assert "WARNING: Cached results, registered workspaces and notification emails stay on barney.example.edu:5544. " \
           "cache.example.edu:5432/team1_cache starts empty unless it already holds them, so register a workspace " \
           "or notification email again there (studio workspace-set, studio notify-email)." in out
    written = box.secrets.read_text().splitlines()
    assert written[0] == f"{CONN}={OLD_CONN}"
    assert written[-1] == f"HAMMER_PG_PASSWORD={CACHE_PW}"
    assert written[1:-1] == SECRETS.splitlines()[1:-1]
    assert box.prev.read_text() == SECRETS and mode(box.secrets) == 0o600
    assert box.target_lines() == SPLIT
    assert CACHE_PW not in out + err


def test_the_cache_server_wins_over_airflows_connection_only_when_marked() -> None:
    environ = {CONN: OLD_CONN}
    split = pd_store.pg_fields(environ, SPLIT)
    assert {f: split[f] for f in ("host", "port", "user", "dbname")} == {
        "host": ("cache.example.edu", "target"), "port": (5432, "target"), "user": ("t1a", "target"),
        "dbname": ("team1_cache", "target")}
    plain = pd_store.pg_fields(environ, {k: v for k, v in SPLIT.items() if k != "SLEDGE_SEPARATE_CACHE"})
    assert plain["host"] == ("barney.example.edu", "conn") and plain["dbname"] == ("team1_cache", "target")
    assert pd_store.pg_fields({**environ, "HAMMER_PG_HOST": "mine"}, SPLIT)["host"] == ("mine", "env")


def test_the_local_postgres_directory_is_kept(box) -> None:
    box.target.write_text(f"HAMMER_PG_HOST=localhost\nHAMMER_PG_PORT={box.port}\nHAMMER_PG_USER=alice\n"
                          f"HAMMER_PG_DB=sledgehammer_studio\nSLEDGE_PG_DIR={box.pg_root}\n")
    rc, out, err = box.run("cache", answers=[PASSPHRASE, *FIELDS, "y", "y", "y"])
    assert rc == 0, out + err
    assert box.target_lines() == {**SPLIT, "SLEDGE_PG_DIR": str(box.pg_root)}


def test_a_failed_login_can_stop_with_nothing_changed(box) -> None:
    rc, out, _ = box.run("cache", answers=[PASSPHRASE, *FIELDS[:4], "wrong", "wrong", "n"])
    assert rc == 1
    assert "  cache database team1_cache: reachable no (" in out and "password authentication failed" in out
    assert box.prompts[-1] == ("input", "Continue anyway? [y/N]: ")
    assert box.secrets.read_text() == SECRETS and not box.prev.exists() and not box.target.exists()


@pytest.mark.parametrize("confirm", [["n"], ["y", "n"]])
def test_either_no_changes_nothing(box, confirm) -> None:
    rc, out, _ = box.run("cache", answers=[PASSPHRASE, *FIELDS, *confirm])
    assert rc == 1 and "Nothing changed." in out
    assert box.secrets.read_text() == SECRETS and not box.prev.exists() and not box.target.exists()


def test_a_dry_run_changes_nothing(box, monkeypatch) -> None:
    monkeypatch.setenv("SLEDGE_DRYRUN", "1")
    rc, out, _ = box.run("cache", answers=[PASSPHRASE, *FIELDS])
    assert rc == 0 and "nothing was changed or started" in out
    assert box.secrets.read_text() == SECRETS and not box.target.exists()


def test_with_no_secrets_the_password_goes_to_pgpass(tmp_path, monkeypatch, capsys, tools) -> None:
    box = Sandbox(tmp_path, monkeypatch, capsys, tools, secrets=None)
    box.db.serve("cache.example.edu", 5432, CACHE_PW, "team1_cache")
    pgpass = box.home / ".pgpass"
    pgpass.write_text("other.example.edu:5432:db:u:keep-me\ncache.example.edu:5432:team1_cache:t1a:old-password\n")
    try:
        rc, out, err = box.run("cache", answers=[*FIELDS, "y", "y"])
        assert rc == 0, out + err
        assert f"There are no encrypted secrets, so it puts the password first in {pgpass} (mode 600)" in out
        assert pgpass.read_text().splitlines() == [f"cache.example.edu:5432:team1_cache:t1a:{CACHE_PW}",
                                                   "other.example.edu:5432:db:u:keep-me"]
        assert mode(pgpass) == 0o600 and not box.secrets.exists()
        assert box.target_lines() == SPLIT
        assert CACHE_PW not in out + err
        settings = pd_store._pg_settings({}, box.target_lines())
        assert (settings["host"], settings["dbname"], settings["passfile"]) == ("cache.example.edu", "team1_cache",
                                                                                str(pgpass))
    finally:
        box.restore()


def test_pgpass_fields_are_escaped(tmp_path) -> None:
    from hammer.shell import sledgehammer_postgres
    path = tmp_path / "pgpass"
    sledgehammer_postgres._write_pgpass(str(path), "db.example.edu", 5432, "team1_cache", "t1a", "a:b\\c")
    assert path.read_text() == "db.example.edu:5432:team1_cache:t1a:a\\:b\\\\c\n"


def test_the_new_line_comes_before_a_wildcard_and_odd_bytes_survive(tmp_path) -> None:
    from hammer.shell import sledgehammer_postgres
    target = tmp_path / "dotfiles" / "pgpass"
    target.parent.mkdir()
    target.write_bytes(b"*:*:*:t1a:stale\nlab:5432:db:u:caf\xe9\n")
    link = tmp_path / ".pgpass"
    link.symlink_to(target)
    sledgehammer_postgres._write_pgpass(str(link), "db.example.edu", 5432, "team1_cache", "t1a", "new")
    assert link.is_symlink()
    assert target.read_bytes() == b"db.example.edu:5432:team1_cache:t1a:new\n*:*:*:t1a:stale\nlab:5432:db:u:caf\xe9\n"


def test_with_airflow_on_the_stack_env_files_conn_the_password_goes_to_pgpass(box, monkeypatch, tmp_path) -> None:
    env_file = tmp_path / "env.sh"
    env_file.write_text(f"export {CONN}='postgresql+psycopg2:///airflow_alice?host=/var/run/postgresql'\n")
    monkeypatch.setenv("SLEDGE_ENV_FILE", str(env_file))
    rc, out, err = box.run("cache", answers=[PASSPHRASE, *FIELDS, "y", "y"])
    assert rc == 0, out + err
    assert f"Airflow's connection comes from the stack env file {env_file}, not the encrypted secrets, so it puts " \
           f"the password first in {box.home / '.pgpass'}" in out
    assert box.secrets.read_text() == SECRETS and not box.prev.exists()
    assert (box.home / ".pgpass").read_text() == f"cache.example.edu:5432:team1_cache:t1a:{CACHE_PW}\n"
    assert CONN not in out.split("This switch changes:")[1]


def test_after_source_venv_sh_the_secrets_take_it_and_the_shells_old_password_is_named(box, monkeypatch) -> None:
    monkeypatch.setenv(CONN, OLD_CONN)
    monkeypatch.setenv("HAMMER_PG_PASSWORD", "oldpw-1234")
    rc, out, err = box.run("cache", answers=[PASSPHRASE, *FIELDS, "y", "y"])
    assert rc == 0, out + err
    assert box.secrets_lines()["HAMMER_PG_PASSWORD"] == CACHE_PW
    assert not (box.home / ".pgpass").exists()
    assert "WARNING: HAMMER_PG_PASSWORD is set in this shell's environment, so it keeps winning" in out
    assert f"WARNING: {CONN}" not in out


def test_show_reports_the_split(box) -> None:
    assert box.run("cache", answers=[PASSPHRASE, *FIELDS, "y", "y"])[0] == 0
    rc, out, err = box.run("show", answers=[PASSPHRASE])
    assert rc == 0, out + err
    assert "Cache (DAG tasks; set apart from Airflow by sledgehammer postgres cache)" in out
    assert row(out, "Cache", "host") == ("cache.example.edu", str(box.target))
    assert row(out, "Cache", "reachable") == ("yes", "")
    assert "NOTE: Airflow and the cache point at different servers: Airflow at barney.example.edu:5544, the cache " \
           "at cache.example.edu:5432." in out


def test_show_says_the_shells_airflow_conn_does_not_move_the_cache(box, monkeypatch) -> None:
    assert box.run("cache", answers=[PASSPHRASE, *FIELDS, "y", "y"])[0] == 0
    monkeypatch.setenv(CONN, OLD_CONN)
    rc, out, err = box.run("show", answers=[PASSPHRASE])
    assert rc == 0, out + err
    assert f"the cache follows {box.target} instead, as sledgehammer postgres cache set it." in out
    assert row(out, "Cache", "host") == ("cache.example.edu", str(box.target))


def test_a_host_line_in_the_secrets_is_named_as_what_the_switch_leaves(tmp_path, monkeypatch, capsys, tools) -> None:
    box = Sandbox(tmp_path, monkeypatch, capsys, tools, secrets=SECRETS + "HAMMER_PG_HOST=pinned.example.edu\n")
    box.db.serve("cache.example.edu", 5432, CACHE_PW, "team1_cache")
    try:
        rc, out, _ = box.run("cache", answers=[PASSPHRASE, *FIELDS, "n"])
        assert "this switch rewrites only HAMMER_PG_PASSWORD there" in out
        assert f"{CONN} and HAMMER_PG_PASSWORD there" not in out
    finally:
        box.restore()


def test_configure_puts_the_cache_back_beside_airflow(box) -> None:
    assert box.run("cache", answers=[PASSPHRASE, *FIELDS, "y", "y"])[0] == 0
    box.db.serve("db2.example.edu", 6543, "pw2", "airflow_alice", "cache_x")
    rc, out, err = box.run("configure", answers=[PASSPHRASE, "db2.example.edu", "6543", "", "", "cache_x",
                                                 "pw2", "pw2", "y", "y"])
    assert rc == 0, out + err
    assert ("input", "  cache database [sledgehammer_studio]: ") in box.prompts
    assert box.target_lines() == {"HAMMER_PG_HOST": "db2.example.edu", "HAMMER_PG_PORT": "6543",
                                  "HAMMER_PG_USER": "alice", "HAMMER_PG_DB": "cache_x"}


def test_use_local_after_a_cache_on_the_local_server_moves_the_cache_back(box) -> None:
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE, "y", "local"])
    assert rc == 0, out + err
    password = box.local_password()
    box.db.serve("localhost", box.port, password, "airflow_alice", "sledgehammer_studio", "other_cache")
    rc, out, err = box.run("cache", answers=[PASSPHRASE, "localhost", str(box.port), "alice", "other_cache",
                                             password, password, "y", "y"])
    assert rc == 0, out + err
    assert box.target_lines()["HAMMER_PG_DB"] == "other_cache"
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE, "y", "local"])
    assert rc == 0, out + err
    assert "do not follow it yet" not in out
    assert (f"  cache    alice@localhost:{box.port}/other_cache\n"
            f"       ->  alice@localhost:{box.port}/sledgehammer_studio") in out
    assert [p for _, p in box.prompts[-2:]] == ["Switch SledgeHammer to your local Postgres? [y/N]: ",
                                                'Type "local" to confirm the switch: ']
    assert "SLEDGE_SEPARATE_CACHE" not in box.target_lines()
    assert box.target_lines()["HAMMER_PG_DB"] == "sledgehammer_studio"


# Stands in for Airflow: ends the launch at its import and reports what the DAG tasks would inherit.
STANDALONE_STUB = ("import os\n"
                   "print('[stub] ' + ' '.join(v + '=' + os.environ.get(v, '') for v in "
                   "('HAMMER_PG_HOST', 'HAMMER_PG_PORT', 'HAMMER_PG_USER', 'HAMMER_PG_DB')))\n"
                   "raise SystemExit(0)\n")


@pytest.mark.parametrize("marked", [True, False])
def test_the_launch_hands_the_cache_server_to_the_dag_tasks(tmp_path, marked) -> None:
    repo = fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py", "sledge-local-postgres.sh")
    home = tmp_path / "home"
    home.mkdir()
    stubs = tmp_path / "stubs"
    for pkg in ("airflow", "airflow/cli", "airflow/cli/commands"):
        (stubs / pkg).mkdir(parents=True)
        (stubs / pkg / "__init__.py").write_text("")
    (stubs / "airflow" / "cli" / "commands" / "standalone_command.py").write_text(STANDALONE_STUB)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    secrets = private / "airflow-secrets.env.gpg"
    secrets.write_text("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=postgresql+psycopg2://alice:pw@airflow.example.edu:5544/"
                       f"airflow_alice\nHAMMER_PG_PASSWORD={CACHE_PW}\n")
    target = SPLIT if marked else {k: v for k, v in SPLIT.items() if k != "SLEDGE_SEPARATE_CACHE"}
    (private / "postgres.env").write_text("".join(f"{k}={v}\n" for k, v in target.items()))
    gpg = fake_postgres.executable(tmp_path / "gpgbin" / "gpg", fake_postgres.GPG)
    res = subprocess.run([sys.executable, str(repo / "scripts" / "airflow-standalone-ldap.py")],
                         capture_output=True, text=True, timeout=120, cwd=str(home),
                         env={"HOME": str(home), "USER": "alice", "SLEDGE_NO_PGADMIN": "1",
                              "PATH": os.pathsep.join((str(gpg.parent), os.environ["PATH"])),
                              "SLEDGE_SECRETS_FILE": str(secrets), "HAMMER_PG_CONNECT_TIMEOUT": "1",
                              "PYTHONPATH": os.pathsep.join((str(stubs), str(fake_postgres.REPO)))})
    if marked:
        want = "HAMMER_PG_HOST=cache.example.edu HAMMER_PG_PORT=5432 HAMMER_PG_USER=t1a HAMMER_PG_DB=team1_cache"
    else:
        want = "HAMMER_PG_HOST=airflow.example.edu HAMMER_PG_PORT=5544 HAMMER_PG_USER=alice HAMMER_PG_DB=team1_cache"
    assert f"[stub] {want}" in res.stdout, res.stdout + res.stderr
