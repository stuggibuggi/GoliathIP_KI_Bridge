# Configuration and call flows

## Accounts and network

The application has two independent SIP clients: one for the door system and
one for the FRITZ!Box. Camera credentials are separate from SIP credentials.
Do not assume a device's web administrator password is its SIP password.

| Setting | Meaning |
| --- | --- |
| Door host | IP address or hostname of the door system's SIP registrar |
| Door SIP port | Registrar UDP port; default 5060 |
| SIP domain | Door registrar domain; default `VDP` for the supported setup |
| Door account | Virtual indoor-station identity configured on your door system |
| FRITZ!Box account | SIP username/password configured for an IP telephone/intercom |
| Destination | Internal or otherwise permitted number to ring through the FRITZ!Box |
| Camera host/account | RTSP endpoint and camera-specific credentials |
| Camera port/channel/substream | Default 554 / 1 / 1; substream 0 or 1 |

The camera settings generator uses the Dahua path
`/cam/realmonitor?channel=CHANNEL&subtype=SUBSTREAM` and percent-encodes username
and password. A different camera URL can be entered in the door tab's RTSP
field. Click Apply camera access before saving separate camera-field changes.
Camera credentials are persisted inside the RTSP URL.

Transport ports are currently fixed in the source:

| Integration | Local SIP UDP | Local RTP UDP | Remote SIP UDP |
| --- | --- | --- | --- |
| Door | 5070 | 17800 | Configurable, default 5060 |
| FRITZ!Box | 5071 | 17801 | 5060 |
| Bot handoff | Dynamically allocated | Dynamically allocated | 5060 |

Run one application instance. Allow required traffic on the trusted local
network if Windows Firewall prompts. Do not expose these services to the public
Internet. Audio uses ordinary RTP, not SRTP.

Some device identities remain installation-specific in the implementation:
`8001` is used as the direct door destination and to recognize doorbell camera
events. `**620` is blocked as a handoff self-call destination. `9901#0` and
`**610` appear as example/default call targets, not discovered devices. Adjust
the source and tests if your installation uses a different numbering plan.
This version is configurable, but not completely vendor/installation agnostic.

## Desktop operations

- **Dashboard**: the default main tab, refreshed once per second. Shows locally
  tracked SIP registration and remaining lifetime, active call legs, bridge/
  handoff state, enabled versus running bot profiles, configured models/voices,
  camera options, web-server state and application uptime. It does not prove
  incoming reachability, camera image delivery or an active OpenAI connection.
  Status text accompanies colors; the tab scrolls at smaller window sizes.
  The bot's general actions share one left-aligned row; results appear below
  instead of moving individual buttons as status messages change.

- **Tuerstation**: register the virtual indoor station, dial a target, answer or
  reject, select desktop audio devices, play a diagnostic tone, preview video.
- **Fritzbox-Bridge**: register the second SIP account and configure bridging.
- **KI-Sprachbot**: configure API access and separate door/telephone profiles.
- **Konfiguration**: central connection fields with a scrollable layout.

Save explicitly. For connection changes, reconnect or restart the application.
Changes to bot settings apply to newly created bot sessions, not necessarily
to an existing conversation.

## Bot setup

Click **Modelle aktualisieren** to fetch model IDs using the configured API key.
The desktop comboboxes and browser suggestion dropdowns separate Realtime and
image-analysis candidates. This is a name-based filter, not capability metadata
from the Models API. Manual IDs and saved selections remain usable even when
not returned. Refresh failures preserve the previous list and settings. Lists
are cached in memory only, until the app closes; refresh after changing API keys.

Built-in voices come from a maintained documented list, not a voice-list API;
`marin` and `cedar` are included. Door and telephone voices remain independent.
Click **Modell/Stimme testen** to open a temporary Realtime session and verify
the requested voice and G.711 PCMU session configuration. It does not generate
speech or establish a SIP call, so it is not an end-to-end audio test.

For image analysis, **Bildmodell testen (API-Kosten)** sends only an artificial
blue image through the Responses API. It never uses a camera snapshot, but can
incur API usage charges. The selected `vision_model` is also used for actual
doorbell image analysis. Never use a Realtime model in this field.

The browser retrieves options through the local app; it does not receive the
stored API key. Save new settings/key before refreshing from the browser.
Changes apply to new calls/images, not existing bot sessions.

API references: [Models](https://developers.openai.com/api/reference/resources/models/methods/list),
[Realtime voices and sessions](https://developers.openai.com/api/docs/guides/realtime-conversations),
[Image inputs](https://developers.openai.com/api/docs/guides/images-vision).

Enter an API key and model, then configure each profile's instructions, voice,
volume, silence limit and maximum call duration. Enable automatic answering
only for the intended source. The door answering delay is 0–40 seconds:
zero means immediate answering. A delayed answer is cancelled when its original
call disappears; it is not supposed to answer a later unrelated call.

Output volume is configurable from 0 to 400 percent. Start at 100 percent;
excessive gain can clip and distort the signal. Caller interruption uses server
voice activity detection and clears queued bot audio when speech is detected.
Speaker echo/background sound can cause false interruptions.

Use neutral, explicit instructions. A telephone-origin call is not proof that
the caller is a trusted family member: externally forwarded calls can reach
the same account. Do not disclose household absence or security information.

## Handoff to a person

Enable handoff separately for each bot profile, enter a single approved target
and set a ring timeout of 5–120 seconds. The bot can request that target only;
it cannot choose arbitrary destinations.

After its announcement, the app dials the destination through the FRITZ!Box.
If answered, it relays RTP in both directions and closes the AI conversation.
If busy, unavailable or timed out, the original caller remains with the bot.
Ending either connected leg ends the other. The app must remain running:
this is an application-managed bridge, not a network-side SIP REFER transfer.

## Doorbell image analysis

Enable it in **KI-Sprachbot → Kameraanalyse** only after camera preview works.
It uses the bot API key and its own configurable image-analysis model. Each
recognized doorbell call triggers a fresh snapshot and an English-documented,
German-language description of visible clothing, objects and person count.
It does not determine names or identity from a face and does not infer
sensitive characteristics. A visitor's name must be self-reported.

The result is logged and supplied only to the matching door bot call. Failure
does not prevent ordinary answering. Snapshot analysis may finish after the
greeting. Indoor-station and FRITZ!Box calls do not trigger doorbell snapshots.

Local JPG storage is optional and off by default. Event JSON files are stored
even when analysis fails. Retention is 1–365 days, default seven; cleanup runs
while the application operates, hourly and before new analyses. It does not
remove unrelated recordings or transcripts.
