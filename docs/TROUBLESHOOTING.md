# Troubleshooting and acceptance tests

## Browser page does not open

1. Confirm the desktop process is running.
2. Open `http://127.0.0.1:8765/`, using HTTP, not HTTPS.
3. Check the desktop log for the admin-server startup or port-binding error.
4. Check `Get-NetTCPConnection -LocalPort 8765` for another process occupying it.

Older builds processed one connection at a time and could stall when a browser
opened an idle connection. This source version enables threaded handling and
includes a regression test for that case. The server is deliberately unavailable
from other computers on the LAN.

## Registration succeeds but incoming calls later fail

Look for repeated REGISTER requests and matching successful replies. Inspect
the granted expiry, Contact address/port, CSeq, registration identity and
whether another client is using the same credentials. Outgoing calls working
does not prove that the registrar still routes incoming calls to this client.

Test an internal call first, then an external call with forwarding. A busy
external call may originate in FRITZ!Box routing, another active dialog, the
provider, or the client. Use a sanitized packet capture to distinguish these
cases; absence of an incoming INVITE in the app log alone does not identify
the responsible component.

## Ringing or connected, but no speech

- Check the SDP audio host/port and negotiated codec.
- Test the output tone and a manual call separately from AI.
- Use diagnostic received-audio recording to determine whether speech reaches
  the app; capture only with appropriate notice/consent.
- Check firewall, competing app instances and whether both RTP directions work.
- If preview works but audio does not, remember RTSP video and RTP call audio
  are independent network flows.
- Investigate a device microphone problem only after confirming incoming RTP
  packets and decoded audio; a packet count does not prove intelligible speech.

## Bot greets but does not respond

Check the log for incoming speech events/transcriptions, not just successful
API connection and generated audio. Confirm the input codec matches the media
leg. Repeated false interruptions or unexpected transcriptions can indicate
echo, television/background audio, codec problems or VAD sensitivity.

Do not lower the detection threshold blindly; test quiet speech and background
noise separately. Check the configured maximum duration and silence timer.

## Camera does not work

Test preview first. Verify the camera's RTSP account, IP, port, channel and
substream; the camera password need not equal its SIP password. After applying
new camera fields, save and restart preview. A failed snapshot or API request
should leave the bot usable with a neutral greeting.

## Logs and rollback

Settings and crash diagnostics live under `%APPDATA%\GoliathIntercom`.
Bot recordings/transcripts use the configured messages directory. Camera
events use `Klingelprotokoll`. Logs may contain caller IDs, addresses, prompts
and transcripts. Redact them before sharing.

To roll back, close an idle application and restore the previous complete
installation directory. Keep the user-data directory intact. Back up settings
privately before migrations; never attach that backup to a public issue.

## Hardware acceptance checklist

Before relying on a release, test:

- Door, indoor and FRITZ!Box calls in both directions.
- Consecutive calls and incoming reachability after several idle registration cycles.
- Speech in both directions, diagnostic tone, and volume without clipping.
- Bot interruption, silence termination and goodbye-triggered SIP termination.
- Successful handoff, busy target, timeout and caller hangup while target rings.
- Delayed door answering cancelled when another station answers first.
- Camera failure, current-image association and retention cleanup.
- Restart with persisted settings and concurrent browser tabs.

Unit tests alone do not establish any of these device-level guarantees.
