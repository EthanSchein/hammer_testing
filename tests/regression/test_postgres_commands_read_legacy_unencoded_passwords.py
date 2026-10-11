import shutil
import sys

import pytest

from hammer.vlsi import pd_store
from tests.utils import fake_postgres
from tests.utils.postgres_switch import CONN, PASSPHRASE, SECRETS, Sandbox, row

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

LEGACY = ["SeKrit/tail", "SeKrit#tail", "SeKrit?tail", "Se@Krit/tail"]
NEW_PASSWORD = "n3w/p#ss"


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture(params=LEGACY)
def box(request, tmp_path, monkeypatch, capsys, tools):
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools,
                      secrets=SECRETS.replace("alice:oldpw-1234@", f"alice:{request.param}@"))
    sandbox.legacy = request.param
    yield sandbox
    sandbox.restore()


@pytest.mark.parametrize("password", LEGACY)
def test_a_raw_password_in_a_conn_parses_as_written(password) -> None:
    conn = f"postgresql+psycopg2://alice:{password}@barney.example.edu:5544/airflow_alice"
    assert pd_store._parse_conn_uri(conn) == {"host": "barney.example.edu", "port": 5544, "user": "alice",
                                              "password": password, "dbname": "airflow_alice"}


def test_show_reads_a_legacy_conn_and_never_prints_its_password(box) -> None:
    box.db.serve("barney.example.edu", 5544, box.legacy, "airflow_alice", "sledgehammer_studio")
    rc, out, err = box.run("show", answers=[PASSPHRASE])
    assert rc == 0, err
    assert row(out, "Airflow's database", "host") == ("barney.example.edu", f"the encrypted secrets {box.secrets}")
    assert row(out, "Airflow's database", "reachable")[0] == "yes"
    assert "SeKrit" not in out + err and "Krit" not in out + err


def test_configure_rewrites_a_legacy_conn_encoded(box) -> None:
    box.db.serve("barney.example.edu", 5544, NEW_PASSWORD, "airflow_alice", "sledgehammer_studio")
    rc, out, err = box.run("configure", answers=[PASSPHRASE, "", "", "", "", "", NEW_PASSWORD, NEW_PASSWORD, "y"])
    assert rc == 0, out + err
    assert [p for _, p in box.prompts[1:5]] == ["  host [barney.example.edu]: ", "  port [5544]: ", "  user [alice]: ",
                                                "  Airflow database [airflow_alice]: "]
    assert box.secrets_lines()[CONN] == \
        "postgresql+psycopg2://alice:n3w%2Fp%23ss@barney.example.edu:5544/airflow_alice"
    assert box.prev.read_text().startswith(f"{CONN}=postgresql+psycopg2://alice:{box.legacy}@")
    assert "Krit" not in out + err
