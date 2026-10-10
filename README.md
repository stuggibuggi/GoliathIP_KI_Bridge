# Goliath IP AI Bridge

A Windows SIP intercom client and local audio bridge for Goliath/Dahua door
stations, indoor stations and an AVM FRITZ!Box. An optional OpenAI voice bot can
answer calls, use separate instructions for door and telephone callers, save
messages, and connect callers to a configured human contact.

**Status: experimental.** This is a custom SIP implementation, not a certified
telephone system. Validate it with your own devices before relying on it. The
desktop interface and built-in example bot instructions are currently German;
this documentation is English. No personal credentials are included.

## Features

- SIP registration, incoming/outgoing calls, microphone and speaker audio.
- G.711 PCMU/PCMA audio and bidirectional door-to-FRITZ!Box media bridging.
- Optional live camera preview over RTSP.
- OpenAI Realtime voice conversations with caller interruption and configurable
  voice, output volume, silence timeout and maximum call duration.
- Separate door and FRITZ!Box bot profiles; configurable delayed door answering.
- Bot-requested call termination and optional handoff to a fixed destination.
- Diagnostic received-audio recording and bot message/transcript storage.
- Optional doorbell snapshot analysis describing visible clothing and objects,
  **not identifying people**.
- Persistent connection settings and a local browser administration page.

## Requirements

- Windows, Python with Tkinter, and network access to the SIP devices.
- A compatible Goliath/Dahua SIP door station and/or a FRITZ!Box SIP account.
- Microphone/speakers for desktop calls; these are not needed for network-only
  bot calls or audio relaying.
- An OpenAI API key with access to the configured models for AI features.
  AI features use external services and may incur usage charges.

The local development environment used Python 3.14.6. Other Python versions
and dependency combinations are not guaranteed. Python 3.13+ needs the
`audioop-lts` backport, included in `requirements.txt`.

## Quick start

```powershell
git clone https://github.com/stuggibuggi/GoliathIP_KI_Bridge.git
cd GoliathIP_KI_Bridge
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe intercom_client.py
```

1. Open **Konfiguration** (Configuration).
2. Enter your door station SIP host, port, domain, account and password.
   Leave unused integrations unconnected.
3. Enter your FRITZ!Box host and the credentials of the IP telephone/intercom
   device you created there. Set the desired destination extension.
4. For video, enter the camera host, credentials, RTSP port, channel and
   substream; click **Kamera-Zugang uebernehmen** (Apply camera access).
5. Click **Einstellungen speichern** (Save settings), then connect the relevant
   SIP account from its operational tab.

Settings start without predefined network hosts or login credentials. Existing
settings are loaded automatically from `%APPDATA%\GoliathIntercom\config.json`.
The bundled `config.example.json` documents a minimal configuration; it is not
automatically imported. Editing it does not change an already running app.

See [Configuration and call flows](docs/CONFIGURATION.md) for setup details.

## Browser administration

While the desktop application is running, open <http://127.0.0.1:8765/>.
This is an embedded local server, not a hosted website. It listens only on
loopback and has **no authentication**. Passwords, API keys and RTSP URLs are
omitted from configuration responses; empty secret fields preserve existing
values when saving through the browser.

The browser page exposes settings, connection status, logs and recent message
and doorbell event information. It is not a browser-based audio softphone.
Do not expose it through a proxy, tunnel or port forwarding.

## Build a Windows application

```powershell
.venv\Scripts\python.exe -m PyInstaller --windowed --onedir --name GoliathIntercom --noconfirm intercom_client.py
```

Start `dist\GoliathIntercom\GoliathIntercom.exe`. Distribute the **entire output
folder**, including `_internal`, rather than copying only the executable.
No prebuilt executable is included in this source repository.

Before replacing an installed build, close the app when no call is active and
keep the old installation folder as a rollback copy. Settings are separate
from the installation, so an upgrade does not intentionally remove them.

## Tests

```powershell
.venv\Scripts\python.exe -m unittest test_bot_transfer test_door_vision test_connection_config test_web_server test_openai_options -v
```

Tests use mocks, synthetic data and local loopback networking. Tkinter tests
need a working desktop session. They do not contact real SIP devices or send
audio/images to OpenAI. Hardware end-to-end testing remains necessary.

## Documentation

- [Configuration and call flows](docs/CONFIGURATION.md)
- [Architecture and implementation constraints](docs/ARCHITECTURE.md)
- [Troubleshooting and acceptance tests](docs/TROUBLESHOOTING.md)
- [Privacy, credentials and safe publication](SECURITY.md)

## Contributing and license

Keep credentials, captures and recordings out of commits. Describe the device
model, reproduction steps and sanitized logs when reporting a bug. Add local
regression tests for protocol/state-machine changes and verify real audio in
both directions before claiming device compatibility.

No open-source license has been selected yet. A public repository alone does
not grant a general license to redistribute or modify its contents. The owner
should select and add a license before inviting broad reuse.

This project is not affiliated with Goliath, Dahua, AVM or OpenAI.
