#!/usr/bin/env python3
"""
Pull the secret fields out of airflow.cfg, encrypt them with GPG, and blank them.

Run this while airflow.cfg still has its real values. It reads fernet_key,
secret_key, jwt_secret, internal_api_secret_key and sql_alchemy_conn, writes
them as env-var lines, and symmetric-encrypts them (you choose the passphrase)
to <checkout>/.sledgehammer/airflow-secrets.env.gpg. The plaintext only ever lives
in a 0600 temp file that gets shredded.

fernet_key has to be captured exactly -- if it changes, the connection passwords
Airflow stored in its database can no longer be decrypted.

At the end it offers to blank the secrets out of airflow.cfg so you can commit it.

    ./scripts/sledge-secrets-init.py
"""

import configparser
import os
import re
import subprocess
import sys
import tempfile

WANT = ["fernet_key", "internal_api_secret_key", "sql_alchemy_conn",
        "secret_key", "jwt_secret"]


def _cfg_path() -> str:
    return os.environ.get("AIRFLOW_CONFIG") or os.path.join(
        os.environ.get("AIRFLOW_HOME", os.getcwd()), "airflow.cfg")


def _blank_cfg(path: str, keys) -> None:
    """Empty each key's value in airflow.cfg, leaving the rest of the file intact."""
    pat = re.compile(r"^(\s*(?:%s)\s*=).*$" % "|".join(re.escape(k) for k in keys))
    lines = []
    with open(path) as f:
        for line in f:
            m = pat.match(line)
            lines.append(m.group(1) + " \n" if m else line)
    mode = os.stat(path).st_mode & 0o777
    tmp = path + ".new"
    with open(tmp, "w") as f:
        f.writelines(lines)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def main() -> None:
    cfg = _cfg_path()
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.expanduser(os.environ.get(
        "SLEDGE_SECRETS_FILE", os.path.join(repo, ".sledgehammer", "airflow-secrets.env.gpg")))

    # gpg needs GPG_TTY to prompt for the passphrase over SSH.
    try:
        if sys.stdin.isatty():
            os.environ.setdefault("GPG_TTY", os.ttyname(sys.stdin.fileno()))
    except Exception:
        pass

    if not os.path.exists(cfg):
        sys.exit(f"airflow.cfg not found at {cfg} (set AIRFLOW_CONFIG or AIRFLOW_HOME).")

    cp = configparser.ConfigParser(interpolation=None)
    cp.read(cfg)

    found = {}  # cfg key -> (env var name, value)
    for sec in cp.sections():
        for k in cp[sec]:
            if k in WANT and cp[sec][k].strip():
                found[k] = (f"AIRFLOW__{sec.upper()}__{k.upper()}", cp[sec][k].strip())

    if "sql_alchemy_conn" not in found:
        sys.exit("sql_alchemy_conn is already blank in airflow.cfg -- nothing to capture.")

    lines = ["# Airflow secrets, loaded into the env at launch. Never commit.", ""]
    for k in WANT:
        if k in found:
            lines.append(f"{found[k][0]}={found[k][1]}")

    sys.path.insert(0, repo)
    try:
        from hammer.shell.sledgehammer_postgres import _env_value
        from hammer.vlsi import pd_store
    except Exception as e:
        sys.exit(f"could not import hammer from {repo} ({e}); run this with the checkout's venv active "
                 "(source ./venv.sh). Nothing was written.")

    # pd_store reads HAMMER_PG_PASSWORD for the sledgehammer_studio cache DB.
    pw = pd_store._parse_conn_uri(found["sql_alchemy_conn"][1]).get("password")
    if pw:
        lines += ["", f"HAMMER_PG_PASSWORD={_env_value(pw)}"]

    missing = [k for k in WANT if k not in found]
    if missing:
        print(f"NOTE: blank/absent in airflow.cfg, skipped: {missing}")

    body = ("\n".join(lines) + "\n").encode()

    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        os.chmod(os.path.dirname(out), 0o700)
    except OSError:
        pass

    # Encrypt to a temp file and rename on success, so a failure can't clobber an
    # existing secrets file.
    out_new = out + ".new"
    fd, tmp = tempfile.mkstemp(prefix=".secrets-", dir=os.path.dirname(out))
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, body)
        os.close(fd)
        print(f"Encrypting {len(found)} secret(s) -> {out}")
        print(">>> Choose YOUR passphrase at the gpg prompt (you'll enter it twice). <<<")
        r = subprocess.run(["gpg", "--symmetric", "--cipher-algo", "AES256",
                            "--yes", "-o", out_new, tmp])
        if r.returncode != 0:
            if os.path.exists(out_new):
                os.remove(out_new)
            sys.exit("gpg failed; the existing secrets file is left unchanged.")
        os.chmod(out_new, 0o600)
        os.replace(out_new, out)
    finally:
        subprocess.run(["shred", "-u", tmp], check=False)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

    print(f"\nWrote {out} (chmod 600).")
    print(f"Verify it decrypts:  gpg --decrypt {out} | grep -c =")

    try:
        ans = input("\nBlank these secrets in airflow.cfg now so it's safe to commit? [y/N] ")
    except EOFError:
        ans = ""
    if ans.strip().lower() == "y":
        _blank_cfg(cfg, list(found.keys()))
        print(f"Blanked {len(found)} secret(s) in {cfg}. It's now safe to commit.")
    else:
        print("Left airflow.cfg untouched -- blank it before you git add it.")


if __name__ == "__main__":
    main()
