# Code Review — LANDesk

First reviewed 2026-09-04. **Remediated 2026-09-07** on branch
`cleanup/2026-09-07`. This file was a to-do list; it is now a record of what
was done, what was left, and why.

## What this project is

A remote desktop tool: a server that captures the screen, streams it as JPEG
frames over TCP, and carries out incoming mouse, keyboard, scroll and clipboard
events; plus a Tkinter viewer that connects to it. Both are packaged as
standalone executables with PyInstaller.

## Verdict, before and after

The 2026-09-04 verdict was "treat this as a LAN-only tool and never expose it",
with four P1 security findings. All four were reproduced against the original
code before anything was changed, and all four are fixed.

It is still a tool that hands a remote viewer full control of a machine, and
still one to run on a network you control. What has changed is that it is no
longer open by default, no longer readable by anyone on the same Wi-Fi, and no
longer worth attacking with a wordlist.

## The four P1s, reproduced first

A throwaway script drove the original server directly. It printed:

```
EXPLOITED  1. empty password: unauthenticated client gets a session
EXPLOITED  2. pass-the-hash: captured handshake replays forever
EXPLOITED  3. unbounded payload length: server waits for 4 GiB
EXPLOITED  4. failed auth is free   30/30 attempts answered in 0.03s (968/s)

4 of 4 exploits worked
```

The same four attacks against the code as it now stands:

```
blocked    1. server starts with no password   refused, and authenticated=False before handshake
blocked    2. pass-the-hash                    first=True replay=False
blocked    3. unbounded payload length         connection dropped in 0.000s, nothing buffered
blocked    4. failed auth is free              5 guesses reached the check, 25 refused outright

0 of 4 exploits worked
```

Each is also a permanent test in `tests/test_handshake.py`, named after the
finding.

## What was fixed

**P1 — no password by default, listening on every interface.**
`--host` now defaults to `127.0.0.1`, and the server refuses to start without a
password. `--no-password` is the escape hatch and says what it costs. The
`authenticated` flag no longer starts from `password_hash == ""`; it starts
`False` on every session and is only set inside the branch that verified a
response, which is the part that actually mattered — a server started without a
password had authenticated every connection before the client said a word.

The server window still binds `0.0.0.0`, because a screen-sharing window that
only accepts connections from its own machine is not a thing anyone wants. It
passes `allow_no_password` only when the user has answered the "no password
set — continue anyway?" dialog that was already there.

**P1 — everything in plaintext.**
The connection is now TLS 1.2+. `shared/tlsutil.py` generates a self-signed
P-256 certificate at each server start and keeps the key in memory; the PEM
goes through a temporary file only because `ssl` cannot load a certificate from
memory, and it is removed as soon as it has been read. `--tls-cert`/`--tls-key`
take a real certificate instead. `--no-tls` turns it off deliberately, on both
ends — the viewer does not fall back, because a fallback is a downgrade attack
with extra steps.

**P1 — pass-the-hash.**
Replaced with a challenge-response over a three-step handshake
(`shared/auth.py`). The password is never sent. The server sends salt,
iteration count and a fresh nonce; the client answers with
`HMAC(PBKDF2(password, salt), tag || nonce || channel)`. Unsalted single-round
SHA-256 became PBKDF2-HMAC-SHA256 at 210,000 iterations, which the server pays
once at startup rather than per connection, so the cost that slows an attacker
down cannot be turned into a way to exhaust the server.

`channel` is the SHA-256 of the server's certificate. That is what makes an
unverified self-signed certificate safe here: a machine in the middle has to
present its own, so the response it collects is bound to the wrong fingerprint
and will not authenticate to the real server. The password is the trust anchor,
not the certificate, and there is no fingerprint for a person to compare. The
server sends a matching proof back, so the viewer is not streaming a screen at
whatever happened to answer the port.

**P1 — unbounded payload length.**
`shared/protocol.py` now carries a per-type ceiling and a per-end set of
accepted types, and both are checked against the header before the payload is
read. A frame may be 32 MiB, a clipboard 1 MiB, and every control message 8
KiB. `MSG_FRAME` is not in `SERVER_ACCEPTS`, so a peer cannot claim the frame
ceiling on the inbound path at all. The decode path is now shared by both ends,
which the previous review asked for and is what made one length check enough.

**P2 — free authentication failures.**
Every rejection waits a second before it is sent, and `server/ratelimit.py`
locks an address out for five minutes after five failures inside five minutes.
Keyed on the address without the port, so reconnecting does not reset the
count. Pure bookkeeping with an injectable clock, tested against a fake one.

**Other findings from the review.**
The bare `except Exception` now logs a traceback and sits behind narrower
clauses for the things a peer can legitimately cause. Frame streaming skips
duplicates by sequence number, so a slow viewer receives the current screen
when the link clears rather than a backlog. `--no-clipboard` turns off the
clipboard path. Missing `LICENSE` (MIT) and `.gitignore` added, and a committed
`shared/__pycache__/protocol.cpython-314.pyc` untracked.

## Found while fixing

- **Every input message was taken on trust.** `on_key` passed any string
  straight to pynput, which raises on anything longer than one character, so a
  viewer typo ended the session; `on_mouse_move` multiplied whatever it was
  given by the screen width; `on_mouse_button` mapped anything unrecognised to
  a left click. Each now validates and clamps, and `to_pixels`, `resolve_key`
  and `clamp_scroll` are separately testable.
- **`--fps 0` divided by zero** in two places and took the server with it.
  `validate_settings` now bounds port, quality, fps and max-clients.
- **A connection that said nothing held a slot forever.** Each handshake read
  has a 15-second timeout, an idle session has 60, and `--max-clients`
  (default 8) bounds concurrent connections.
- **The viewer raised `RuntimeError: Event loop is closed`** from `stop()` if
  the connection had already ended — clicking Disconnect after a refused
  password did it. Found by the end-to-end test, fixed in `stop()`.
- **`START_SERVER.bat` had `--password changeme` committed in it.** It now
  prompts and passes the password through the environment, since a command line
  is readable by other users on the machine.

## Deliberately not done

- **No always-on-top "someone is connected" indicator** (P2 in the original
  review). It is a new window, and the standing instruction for this archive is
  business logic only — no UI or UX changes. It remains a good idea and the
  right shape for a screen-sharing tool. The server log does record each
  connection and the server window still shows the viewer count.
- **The GUIs still store the password in plaintext** in
  `~/.landesk/*_settings.json`. Not in the repository, so nothing is exposed by
  the code being public, and removing "remember the password" is a UX decision
  rather than a bug. Worth the author's attention.
- **No git history rewrite.** Nothing sensitive was found in it — no
  credentials, no keys — so there was nothing to rewrite for, and it is the
  author's call regardless.
- **`--tls-cert` with a real certificate is not verified by the viewer.** The
  viewer accepts any certificate and relies on the channel binding, which is
  correct for the generated case and no worse for a supplied one. Pinning or CA
  verification would need somewhere in the GUI to put a fingerprint, which is a
  UI change.

## Tests

101 tests, about eight seconds, standard library only:

```sh
python tests/run.py            # everything
python tests/run.py auth       # one file
```

- `test_protocol.py` — framing, per-type ceilings, accepted-type sets, the
  header check happening before the payload read.
- `test_auth.py` — key derivation, and that a response does not carry to
  another nonce or another certificate.
- `test_ratelimit.py` — lockout, expiry, per-address keying, table growth,
  against a fake clock.
- `test_input.py` — coordinate clamping, key resolution, scroll limits, and
  that rubbish off the wire does not raise.
- `test_handshake.py` — a real server on a real socket: the happy path, the
  four P1 attacks, TLS, a simulated machine in the middle, malformed input,
  and the configuration checks.
- `test_endtoend.py` — `viewer.py`'s own `LANDeskClient` against the real
  server, over TLS and without, including a server that cannot prove it knows
  the password.

The TLS tests skip without `cryptography`. A virtualenv with it is at
`D:\dev-tools\venvs\landesk\`.

## Notes for a future session

**The wire format changed.** `PROTOCOL_VERSION` is 2, the handshake is three
steps, and version 1 is refused with a message saying so. An old executable
will not talk to a new one — rebuild both.

`cryptography` is a new dependency, and only on the server, and only to
generate a certificate. `--tls-cert`/`--tls-key` or `--no-tls` avoid it. It is
in `requirements.txt`, `BUILD_EXE.bat` and `landesk_server.spec`.

Nothing here has been through PyInstaller. The spec files were updated to
bundle `shared/auth.py`, `shared/tlsutil.py` and `server/ratelimit.py` and to
list the new hidden imports, but `BUILD_EXE.bat` has not been run — the archive
machine has no PyInstaller and the build is not something to install for one
check. **Running `BUILD_EXE.bat` once and connecting the two executables is the
first thing to do here if you have a machine with the dependencies.** The
import paths are the risk: `server.py` now puts both its own directory and its
parent on `sys.path`, which covers the frozen layout, but that is reasoning,
not a build.

Everything else was verified live: the real server, started from the command
line with real screen capture, streaming real JPEG frames over TLS to the real
viewer client, with the wrong password refused and a plaintext client turned
away. pynput was not installed, so input injection is covered by tests rather
than by a mouse moving.

## Action checklist

- [x] **P1** Require a password by default; bind to localhost unless told otherwise.
- [x] **P1** Add TLS to the connection.
- [x] **P1** Replace pass-the-hash with challenge-response and a salted, iterated hash.
- [x] **P1** Cap payload length per message type before reading.
- [x] **P2** Add a delay and a failure counter on bad authentication.
- [ ] **P2** Add a visible "someone is connected" indicator — deliberately not
      done; it is a UI change. See above.
- [x] **P3** Share the decode path via `shared/protocol.py`; add frame
      dropping; add `.gitignore` and `LICENSE`.
- [ ] Run `BUILD_EXE.bat` on a machine that can, and check both executables.

## Licence, 2026-10-02

The author chose one licence for all of their projects: free for personal,
non-commercial use, modifiable for the user's own use, and not to be
distributed, original or modified. No standard licence says that, so `LICENSE`
is a short custom text, the same in every project apart from the year and what
it excludes. The README's licence section says the same. Anything above that
calls the licence an open question, or calls the project MIT, is superseded.

## CI, 2026-10-02

`.github/workflows/ci.yml` installs `cryptography` and runs `tests/run.py` on
Python 3.12, `ubuntu-latest`. Nothing has been pushed, so it has not run on
GitHub yet. Every job was run before committing, from a fresh clone of this
branch, the way a runner would run it: Linux jobs in a throwaway Ubuntu 24.04
(the image `ubuntu-latest` uses) with the same tool versions. 101, the TLS
tests included.
