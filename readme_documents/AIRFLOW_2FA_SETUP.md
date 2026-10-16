# Airflow two-factor login (TOTP)

This adds a second authentication factor to the Airflow web login. The first
factor is the existing EECS LDAP password (see AIRFLOW_LDAP_SETUP.md). The
second is a rotating six-digit code from an authenticator app on the user's
phone (Google Authenticator, Authy, 1Password, Microsoft Authenticator, or any
TOTP app). A stolen or shared EECS password is no longer enough to get in.

It's built from scratch in the `auth2fa/` package, with no third-party crypto
dependency: a TOTP code is an HMAC-SHA1 of the current 30-second time window
(RFC 6238), and that's all standard library.

## Try the demo first

The demo is a standalone web app that walks the whole flow (password, QR
enrollment, six-digit code) without touching the live Airflow deployment. It
keeps its state in a local SQLite file, so it needs no database.

```bash
cd <path-to-your-hammer-checkout>
.venv/bin/python -m auth2fa.demo_app --port 8099
```

Then open `http://<this-host>:8099/`. If your browser is on another machine,
tunnel the port first: `ssh -L 8099:localhost:8099 <user>@<this-host>`.

What you'll see:

1. **Sign in.** By default any username and any non-empty password are accepted:
   the demo is about the second factor, so the first one is stubbed out. To
   exercise the real EECS password check instead, start it with
   `SLEDGE_2FA_DEMO_LDAP=1` and use your own EECS uid and password.
2. **Set up two-factor.** First time through, a QR code appears. Scan it with an
   authenticator app (or type the shown key in by hand). Enter the code the app
   displays to confirm.
3. **You're in.** On later logins you'll just be asked for the current code.

There's a "Reset my 2FA" button on the landing page so you can re-run enrollment
as many times as you like.

## How the flow works

```
/login   enter uid + password
            password wrong  -> back to /login
            password right  -> user id parked in the session (NOT logged in yet)
                               redirect to /mfa
/mfa     enrolled already?
            no   -> show QR, user scans, enters a code to confirm enrollment
            yes  -> ask for the current 6-digit code
         code good -> login_user(), redirect into Airflow
         code bad  -> stay on /mfa (5 misses lock an enrolled uid for 5 min)
```

`login_user` (the step that actually grants the session) happens only after a
good code. Passing the password alone leaves a parked user id that grants
nothing.

## Use it on the real Airflow deployment

The integration ships **on**. The checked-in `webserver_config.py` already calls
`install_2fa()`, and both launchers (`sledgehammer` and
`scripts/airflow-standalone-ldap.py`) set `SLEDGE_2FA=1` unless you set it
yourself. `install_2fa()` is a no-op unless `SLEDGE_2FA=1`, so if you start
the api-server some other way (for example `sledgehammer api-server`), put
`SLEDGE_2FA=1` in its environment. `webserver_config.py` logs
`two-factor (TOTP) second factor is ON` when it installs the second factor.

1. Start the server:

   ```bash
   sledgehammer
   ```

2. Log in. The first time, you'll be walked through QR enrollment; after that,
   you'll enter a code on each login.

To turn it off, launch with `SLEDGE_2FA=0 sledgehammer` (the standalone script
honors `SLEDGE_2FA=0` too). Unsetting the variable is not enough, because the
launchers set it back to `1`. Enrollments are kept in the database, so flipping
it back on later doesn't make anyone re-enroll.

### Where secrets live

Each user's TOTP secret is one row in `hammer_poc.user_totp`, in the same
`sledgehammer_studio` Postgres the login whitelist and PD cache already use. The
table is created automatically on first use. Columns:

| column | meaning |
|---|---|
| `uid` | EECS username |
| `secret` | base32 TOTP secret |
| `confirmed` | true once the user has proven a code (enrollment finished) |
| `last_step` | the last time-step accepted for them (replay guard) |
| `failed_attempts`, `locked_until` | wrong-code counter and lockout window |
| `created_at`, `confirmed_at` | timestamps |

The demo uses a SQLite file (`auth2fa/demo_2fa.sqlite` by default, override with
`SLEDGE_2FA_DB`) with the same shape, so demo enrollments never touch Postgres.

## Admin: managing enrollments

`studio 2fa` is meant for the database owner, like `studio whitelist`:

```bash
studio 2fa                 # list everyone's enrollment state (active / pending)
studio 2fa <eecs-uid>      # show one user's state
studio 2fa --reset <uid>   # clear a user's enrollment (lost phone, new device)
```

After a reset, that user enrolls a fresh authenticator on their next login. The
secret is never printed. `--reset` asks before it clears anything; add `--yes`
to skip the prompt.

## Recovery (lost or replaced phone)

A user who loses access to their authenticator can't generate codes, so an admin
clears their enrollment with `studio 2fa --reset <uid>`. On the next login they
scan a new QR. Because the login whitelist still gates who may log in at all,
this is safe: only an already-allowed user can re-enroll.

## Security notes

- **Replay.** A code is accepted once. The matched time-step is recorded, and any
  code from that step or earlier is refused, so a code sniffed in flight is
  useless for the rest of its 30 seconds. (A user logging in twice inside the
  same 30-second window would need to wait for the next code. That is standard
  TOTP behavior.)
- **Clock skew.** Verification accepts the code from one step on either side of
  now, covering a phone clock that's a little off.
- **Constant-time check.** Code comparison uses `hmac.compare_digest`, so timing
  doesn't leak the right digits.
- **Online guessing.** After five wrong codes a user is locked out for five
  minutes. That counter lives **per uid in `user_totp`, not in the session**, so
  re-entering the password does not reset it. That closes the obvious brute-force
  path (with a ~1e-6 per-guess hit rate, a real lockout is what protects the
  6-digit space). As a second layer, Airflow's FAB stack enables POST rate
  limiting by default (`AUTH_RATE_LIMITED`, keyed by client IP), which also
  covers `/auth/mfa` since it shares the auth blueprint; the per-uid lockout is
  the part that survives a server restart and a multi-IP attacker.
- **Enrollment binding (known limitation).** If an attacker already has a
  whitelisted user's password and that user has **never enrolled**, the attacker
  can enroll their own authenticator on first login. This is inherent to any
  self-service TOTP. Mitigate by enrolling before a password is at risk, or by
  having an admin pre-seed enrollment. Once a user is enrolled, a password alone
  can't take over the account.
- **Code reuse within a window.** An exact code can't be replayed once used. Note
  that the +/-1 skew window means up to three codes are valid at any instant and
  the guard burns only the consumed step and earlier ones; reusing a *distinct*
  later neighbor code would require capturing it separately, by which point the
  password is already compromised. Acceptable for this threat model; tighten by
  burning the whole window on success if you want strict one-code-per-window.
- **Secret at rest.** Secrets sit in Postgres in plain base32, protected by the
  database's own access control. A server-side TOTP verifier must hold the
  shared secret to check codes, so this is inherent; reading them requires
  already having Postgres access. `studio 2fa` never prints secrets. If you want
  defense-in-depth against a DB compromise, encrypt the `secret` column: a
  reasonable follow-up, not a blocker.
- **Concurrency.** The replay guard is a check-then-act across two short
  transactions, so two simultaneous logins with the same valid code could both
  succeed. This only ever yields duplicate sessions of the *same*
  already-fully-authenticated identity (both factors required to reach it), so
  there's no privilege gain; an atomic conditional update is a possible
  hardening.
- **What this is not.** This is app-level TOTP, not WebAuthn/hardware keys and not
  SSO. It assumes the LDAP first factor in front of it. It does not protect the
  Airflow REST API tokens, only the interactive web login.

## Files

- `auth2fa/totp.py`: RFC 6238 code generation and verification (stdlib only).
- `auth2fa/store.py`: secret storage; SQLite (demo) and Postgres (deployment).
- `auth2fa/service.py`: enrollment and verify logic shared by both front ends.
- `auth2fa/qr.py`: renders the enrollment QR (uses segno if installed).
- `auth2fa/demo_app.py`: the standalone demo.
- `auth2fa/fab_integration.py`: the Airflow login override; `install_2fa()`.
- `auth2fa/test_totp.py`, `auth2fa/test_integration.py`: tests (RFC vectors,
  replay, and the full /login -> /mfa flow through a real FAB app).

Optional: `segno` (pure Python) renders the QR inline. Without it, enrollment
still works: the page shows the key for manual entry. Install with
`uv pip install segno` if you want the inline image.
