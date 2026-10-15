# LANDesk

A remote desktop tool for two Windows PCs on the same network. One machine
runs the server, which streams its screen and carries out the mouse and
keyboard events it is sent. The other runs the viewer, which shows that screen
in a window and sends what you do in it. No account, no third-party service,
nothing leaves your network.

It is about 2,500 lines of Python. The parts worth knowing about are the
protocol (`shared/protocol.py`), the password exchange (`shared/auth.py`) and
the server loop (`server/server.py`).

## What you are actually turning on

Read this part before running it anywhere.

The server hands whoever gets past its password a live view of the screen and
full control of the mouse and keyboard. There is no confirmation prompt on the
machine being controlled and no way to take control back except stopping the
server. Anything the person at the keyboard can do, a connected viewer can do:
read the mail that is open, open a browser, install something.

So:

- **Set a password.** The server refuses to start without one. `--no-password`
  exists for a machine on an isolated network where you genuinely do not want
  one, and it means what it says.
- **The default bind address is `127.0.0.1`** - this machine only, useful for
  trying it out. `--host 0.0.0.0` is what makes it reachable from the LAN, and
  it is a deliberate thing to type. `START_SERVER.bat` and the server window
  both use `0.0.0.0`, because sharing a screen is what they are for.
- **Do not put it on the internet.** Not port-forwarded, not on a public
  Wi-Fi network. It is built for a LAN you control.

### How the password works

The password is never sent. The server sends a random challenge; the viewer
stretches the password with PBKDF2-HMAC-SHA256 (210,000 iterations) using a
salt the server supplies, and answers with an HMAC over the challenge. The
server checks it against the same computation, and answers with a proof of its
own so the viewer knows it did not just connect to something that says yes to
everything.

Because the answer covers a fresh random challenge each time, capturing one
connection does not let anyone reuse it. Five wrong answers from an address
locks that address out for five minutes, and every rejection is delayed by a
second whether it is the first or the fiftieth.

### How the encryption works

The connection is TLS 1.2 or better. The server generates a self-signed
certificate when it starts and keeps the private key in memory; nothing is
written to disk and there is no certificate to install or fingerprint to
compare.

That sounds too easy, and on its own it would be: a self-signed certificate
nobody checks stops passive recording but not a machine in the middle. What
closes that is the password exchange - both proofs cover the certificate the
connection is actually using. Something in the middle has to present its own
certificate, so what it collects is bound to the wrong one and is worthless to
it. The password, not the certificate, is what you are trusting.

Two consequences worth knowing:

- `--no-tls` turns encryption off and has to be passed on **both** ends. Then
  the screen, the keystrokes and the clipboard cross the network in the clear.
  There is no automatic fallback - a viewer that quietly retried unencrypted
  would give an attacker a way to ask for exactly that.
- **Clipboard sharing is on by default and goes both ways.** `--no-clipboard`
  on the server turns off accepting the viewer's clipboard.

## Running it from source

Python 3.9 or newer on both machines.

```sh
pip install -r requirements.txt
```

On the machine to be controlled:

```sh
python server/server.py --password secret --host 0.0.0.0
```

Or double-click `START_SERVER.bat`, which asks for the password and passes it
through the environment rather than the command line. `server/server_gui.py`
is the same server with a window around it.

On the machine you sit at:

```sh
python viewer/viewer.py
```

Enter the server's IP address, port 9000 and the password. `ipconfig` on the
server machine gives you the address - the `IPv4 Address` line under your
Wi-Fi or Ethernet adapter, usually something like `192.168.1.42`.

If nothing connects, the Windows firewall on the server machine is the usual
reason. In an administrator Command Prompt:

```
netsh advfirewall firewall add rule name="LANDesk" dir=in action=allow protocol=TCP localport=9000
```

## Standalone executables

`BUILD_EXE.bat` builds `LANDesk-Server.exe` and `LANDesk-Viewer.exe` with
PyInstaller on any machine that has Python, and those run anywhere without it.
The build needs internet access once, to fetch dependencies. Copy the server
executable to the machine to be controlled and the viewer to the one you sit
at; uninstalling is deleting the file.

Windows SmartScreen will warn about an unsigned executable. That is expected
for something you built yourself.

The two ends speak protocol version 2 and refuse anything else with a message
saying so, so **rebuild and copy both** rather than one.

## Options

### Server

| Option | Default | What it does |
| --- | --- | --- |
| `--password` | *(prompts)* | Connection password. `$LANDESK_PASSWORD` and the prompt are both better than the command line, which other users on the machine can read. |
| `--no-password` | off | Start with no password at all. |
| `--host` | `127.0.0.1` | Bind address. `0.0.0.0` for the whole LAN. |
| `--port` | `9000` | TCP port. |
| `--quality` | `50` | JPEG quality, 1-95. |
| `--fps` | `20` | Target frames per second, 1-60. |
| `--no-tls` | off | Turn off encryption. The viewer needs the same flag. |
| `--tls-cert`, `--tls-key` | *(generated)* | Use your own PEM certificate instead of a generated one. Avoids the `cryptography` dependency. |
| `--no-clipboard` | off | Do not accept clipboard content from the viewer. |
| `--max-clients` | `8` | Concurrent viewers. |

### Viewer

| Option | Default | What it does |
| --- | --- | --- |
| `--host` | *(dialog)* | Connect to this address on launch instead of waiting. |
| `--port` | `9000` | Port. |
| `--password` | *(dialog)* | Password, for `--host`. |
| `--no-tls` | off | Connect without encryption, for a server started the same way. |

### Tuning

Laggy: lower `--quality` to 25-35 first, then `--fps` to 10-15. Blurry: raise
`--quality` to 70-80. High CPU on the server: lower `--fps` first.

A slow link drops frames rather than falling behind - the server sends the
current screen when the connection clears, not the backlog.

## How it fits together

```
[Viewer PC]                              [Server PC]
viewer/viewer.py                         server/server.py
    |                                        |
    |--- TCP, then TLS -------------------->  |
    |--- HELLO (protocol version) --------->  |
    |<-- CHALLENGE (salt, nonce) -----------  |
    |--- AUTH (HMAC over nonce + cert) ---->  |
    |<-- HELLO_ACK (resolution, proof) -----  |
    |                                        |  ScreenCapturer (thread)
    |<-- FRAME (JPEG) ----------------------  |<-- mss grab -> Pillow JPEG
    |<-- FRAME -----------------------------  |    (every 1/fps seconds)
    |--- MOUSE_MOVE (nx, ny) -------------->  |--> pynput mouse.position
    |--- MOUSE_BTN (button, pressed) ------>  |--> pynput press/release
    |--- KEY (key, pressed) --------------->  |--> pynput press/release
    |--- SCROLL (dx, dy) ------------------>  |--> pynput mouse.scroll
    |--- CLIPBOARD_SEND (text) ------------>  |--> pyperclip.copy
    |--- PING (once a second when idle) --->  |
```

Every message is a five-byte header - one byte of type, four of payload length
- followed by that many bytes. Each end declares which types it accepts and
what the largest payload of each type may be, and both are checked against the
header before the payload is read. Coordinates are normalised to 0..1 so the
two screens do not have to be the same size.

## Layout

```
landesk/
├── shared/
│   ├── protocol.py       message format, size limits, framed reads
│   ├── auth.py           PBKDF2 + challenge-response, no I/O in it
│   └── tlsutil.py        certificate generation and SSL contexts
├── server/
│   ├── server.py         capture, input injection, session handling
│   ├── ratelimit.py      failed-authentication lockout
│   └── server_gui.py     window around the server
├── viewer/
│   └── viewer.py         tkinter window and the network client
├── tests/                the test suite; see below
├── requirements.txt
├── BUILD_EXE.bat         builds both executables
├── landesk_*.spec        PyInstaller configuration
└── START_*.bat           run from source
```

## Tests

```sh
python tests/run.py           # everything
python tests/run.py auth      # just tests/test_auth.py
```

Standard library only. The TLS tests skip themselves if `cryptography` is not
installed. There are 101 of them and they take about eight seconds: the
protocol's limits, the password exchange, the lockout, the input translation,
the handshake against a real server on a real socket, and one end-to-end test
that drives the viewer's own client class against the real server.

The security tests were written against the previous version of this code
first, where they failed - which is to say the attacks worked. `review.md` has
the details.

## Licence

Free for personal, non-commercial use. You may use, study and modify this
software for yourself, but you may not use it commercially, and you may not
share or publish it, in its original form or modified. See [LICENSE](LICENSE)
for the full terms.
