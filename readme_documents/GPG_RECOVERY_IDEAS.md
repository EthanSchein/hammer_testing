# GPG recovery ideas

What happens if the Airflow secrets passphrase is lost, and the options for
adding a real recovery path. None of this is wired up right now: the setup is
passphrase-only (see [SECRETS.md](SECRETS.md)). This is the menu for if and when
we want more.

## What's actually at stake

If the passphrase is lost, most of the secrets are disposable:

- `sql_alchemy_conn` and `HAMMER_PG_PASSWORD`: just reset the Postgres role's
  password.
- `secret_key`, `jwt_secret`, `internal_api_secret_key`: regenerate them; you
  only invalidate existing sessions and tokens.
- `fernet_key`: this is the one that matters. It decrypts the connection
  passwords and Variables stored in the Airflow database, so losing it means
  re-entering those. A fresh standalone setup usually has none, so even this is
  often painless.

So forgetting the passphrase is rarely a catastrophe; it's mostly regeneration.

## Today: regenerate

There's no mechanism. You rebuild each secret:

    # DB password
    psql ... -c "ALTER ROLE <you> WITH PASSWORD '<new>';"
    # fernet_key
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # secret_key / jwt_secret / internal_api_secret_key
    # (secret_key and internal_api_secret_key must be the same value)
    python -c "import secrets; print(secrets.token_hex(32))"

Then move the old `.gpg` aside and encrypt the new values into a fresh one, as a
new operator does in [SECRETS.md](SECRETS.md).
`./scripts/sledge-secrets-create.sh` does the encrypting for you once the old
file is out of the way: it prompts for the Postgres connection and generates the
keys itself. Re-enter any stored Airflow connections afterwards.

## A recovery key

Encrypt the secrets file to a passphrase *and* a second GPG key, so either one
can open it. The recovery key's private half stays offline, away from the
machine. That's the emergency way back in, with no plaintext copy anywhere.

Where to keep that private key, best to worst:

- A hardware token (YubiKey or OpenPGP smartcard). The key can't be copied off
  the device, and using it needs the device plus a PIN, which makes it real
  two-factor.
- A password manager with 2FA.
- An encrypted USB stick or a printed paper key: offline, but single-factor.

A TOTP app (Google Authenticator and the like) doesn't fit: a rotating code
isn't a key, it only gates a service. You'd only get TOTP-gated recovery if the
key lived behind a service that enforced it.

## Other options

- Team escrow: also encrypt to a teammate's GPG key, so any of them can help you
  back in. Good for a shared instance where you don't want to be the only person
  who can recover it.
- Print just the `fernet_key` and seal it somewhere. It's the only secret that
  loses data, so this covers the irreversible case with almost no effort.
- Rotate the passphrase periodically, so a leak only matters for a bounded window.

## Adding a recovery key later

The secret fields in `airflow.cfg` are blank, so re-encrypt the existing `.gpg`
in your checkout instead of re-reading the cfg:

    gpg --decrypt .sledgehammer/airflow-secrets.env.gpg \
     | gpg --symmetric --encrypt -r <recovery-key> --trust-model always \
           --cipher-algo AES256 -o .sledgehammer/airflow-secrets.env.gpg.new
    mv .sledgehammer/airflow-secrets.env.gpg{.new,}

The plaintext only ever passes through the pipe.
