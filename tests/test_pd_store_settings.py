import pytest

from hammer.vlsi import pd_store


@pytest.fixture
def no_password_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(pd_store, "_parse_airflow_cfg_conn", lambda: {})
    for var in ("HAMMER_PG_PASSWORD", "PGPASSWORD", "PGPASSFILE", "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "SLEDGE_PG_DIR"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return tmp_path


class TestPgSettings:
    def test_pgpass_file_lets_libpq_supply_the_password(self, no_password_sources, monkeypatch) -> None:
        pgpass = no_password_sources / "pgpass"
        pgpass.write_text("localhost:5433:sledgehammer_studio:someone:secret\n")
        monkeypatch.setenv("PGPASSFILE", str(pgpass))
        settings = pd_store._pg_settings()
        assert "password" not in settings
        assert settings["passfile"] == str(pgpass)
        assert {"host", "port", "user", "dbname"} <= set(settings)

    def test_default_pgpass_in_home_is_passed_to_libpq(self, no_password_sources) -> None:
        pgpass = no_password_sources / "home" / ".pgpass"
        pgpass.write_text("*:*:*:*:secret\n")
        settings = pd_store._pg_settings()
        assert "password" not in settings
        assert settings["passfile"] == str(pgpass)

    def test_pgpassword_env_is_enough(self, no_password_sources, monkeypatch) -> None:
        monkeypatch.setenv("PGPASSWORD", "secret")
        settings = pd_store._pg_settings()
        assert "password" not in settings
        assert "passfile" not in settings

    def test_no_source_raises_and_mentions_pgpass(self, no_password_sources) -> None:
        with pytest.raises(RuntimeError, match=r"\.pgpass"):
            pd_store._pg_settings()

    def test_hammer_pg_password_wins_over_pgpass(self, no_password_sources, monkeypatch) -> None:
        pgpass = no_password_sources / "pgpass"
        pgpass.write_text("*:*:*:*:from-file\n")
        monkeypatch.setenv("PGPASSFILE", str(pgpass))
        monkeypatch.setenv("HAMMER_PG_PASSWORD", "from-env")
        assert pd_store._pg_settings()["password"] == "from-env"
