"""Where generated DAGs are linked and looked up, so the build and `sledgehammer run` agree."""
import configparser
import os
import re

# hammer/shell/dags_folder.py -> the checkout root is three levels up.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def resolve_dags_folder():
    """(folder, where it came from).

    Order: $HAMMER_DAGS_FOLDER, $AIRFLOW__CORE__DAGS_FOLDER, [core] dags_folder in
    airflow.cfg ($AIRFLOW_CONFIG, else $AIRFLOW_HOME/airflow.cfg), then
    $AIRFLOW_HOME/dags. AIRFLOW_HOME defaults to the checkout, where the
    sledgehammer launcher pins it. Values are read as Airflow reads them:
    %(name)s interpolation in the file, then $VARS and ~, repeated until nothing
    changes, with $AIRFLOW_HOME meaning that folder. Reads the file rather than
    asking the airflow CLI, so it is fast and needs no airflow install.
    """
    def expand_env(value: str) -> str:
        for _ in range(16):
            new = os.path.expanduser(os.path.expandvars(value))
            if new == value:
                break
            value = new
        return value

    home = expand_env(os.environ.get("AIRFLOW_HOME") or REPO)

    def expand(value: str) -> str:
        return expand_env(re.sub(r"\$(?:\{AIRFLOW_HOME\}|AIRFLOW_HOME(?!\w))", lambda m: home, value))

    for var in ("HAMMER_DAGS_FOLDER", "AIRFLOW__CORE__DAGS_FOLDER"):
        if os.environ.get(var):
            return expand(os.environ[var]), f"${var}"
    cfg = expand_env(os.environ.get("AIRFLOW_CONFIG") or os.path.join(home, "airflow.cfg"))
    parser = configparser.ConfigParser()
    try:
        parser.read(cfg)
        got = parser.get("core", "dags_folder", fallback="").strip()
    except configparser.Error:
        got = ""
    if got:
        return expand(got), cfg
    return os.path.join(home, "dags"), "$AIRFLOW_HOME/dags"
