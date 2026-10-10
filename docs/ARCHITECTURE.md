# Architecture

The application is a desktop process with an embedded Flask administration
server. Most code is in `intercom_client.py`; `door_vision.py` handles snapshots,
image analysis and event retention.

```text
Door/indoor station -- SIP + RTP -- App -- SIP + RTP -- FRITZ!Box/telephone
                                      |
                                      +-- Realtime WebSocket -- OpenAI voice bot
                                      +-- RTSP snapshot + HTTPS -- Image analysis
                                      +-- Tkinter desktop UI
                                      +-- HTTP loopback admin UI
```

## Main components

| Component | Responsibility |
| --- | --- |
| `SipCallClient` | UDP registration, authentication, SIP transactions/dialogs and call commands |
| `AudioEngine` | Desktop microphone/speaker, G.711 conversion, test tone and diagnostic WAV |
| `BridgeController` / `BridgeRelay` | Connect two network media legs, including PCMU/PCMA conversion |
| `VoiceBotEndpoint` | Realtime session, RTP audio, interruption, tools, messages and watchdog |
| `TransferEndpoint` | Reserve media endpoint for an outbound handoff leg |
| `VideoPreview` | RTSP preview using OpenCV |
| `DoorVision` | Fresh snapshots, descriptive image request and scoped retention |
| `AdminServer` | Flask routes on `127.0.0.1:8765` |
| `App` | Tkinter UI, configuration persistence, event polling and orchestration |

SIP workers consume command queues and emit events. The Tkinter main thread
polls events; bot logging uses a queue instead of touching widgets directly.
The current web administration still uses Tkinter-backed configuration access;
it is not an independently deployable service.

## Media and AI

Audio is sampled at 8 kHz, normally sent as 160-sample / 20 ms RTP packets.
Supported conversational codecs are G.711 PCMU (payload 0) and PCMA (payload 8).
Other advertised device codecs are not automatically supported.

The voice bot uses the Realtime WebSocket interface and G.711 audio input/output.
Instructions, tools and voice activity detection are configured per session.
The `end_call` tool requests hangup after farewell playback; `transfer_call`
requests the approved human handoff. Network/dialog success must still be
validated against the far-end SIP response.

Camera analysis uses an independent HTTPS Responses request with `store: false`.
This request setting is not a promise that no provider-side processing or
retention can ever occur. See [Security and privacy](../SECURITY.md).

## Protocol constraints

The custom client implements Digest registration/authentication, incoming and
outgoing dialogs, route/contact handling, ACK, CANCEL and BYE. It is not a full
SIP stack: interoperability, session refreshes and device-specific behavior
need real-device tests. It does not provide SIP TLS, SRTP, multi-user PBX
features or production-grade high availability.

Registration refresh uses the granted expiry, with a timeout/retry for pending
REGISTER requests. The FRITZ!Box path also contains an idle-only full
deregistration/re-registration workaround every 240 seconds. This is a local
compatibility workaround, **not a verified claim about all FRITZ!OS versions**.
Do not assume it resolves every incoming-call failure.

The browser server handles requests in separate threads so an idle browser
connection does not monopolize its only request handler. It remains loopback-
only and unauthenticated.
