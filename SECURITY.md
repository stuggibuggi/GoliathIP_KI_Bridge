# Security and privacy

This is an experimental application for a trusted local network, not a hardened
PBX or access-control system. Do not use AI output to authorize physical access.

## Credentials

Passwords and API keys are stored in plaintext in
`%APPDATA%\GoliathIntercom\config.json`. Camera credentials are embedded in the
persisted RTSP URL. Protect the Windows account and backups. UI masking is not
encryption. The browser configuration endpoint omits those secret values.

Do not commit settings, recordings, transcripts, screenshots, packet captures,
crash logs, build artifacts or copied user-data directories. `.gitignore` is a
guardrail, not a substitute for reviewing staged files; it does not erase
secrets already present in Git history. Rotate exposed credentials immediately.

## Network exposure

The browser administrator listens on loopback only and has no authentication.
Other local processes can access it. Do not forward or proxy it publicly.
SIP/RTP in this implementation is not encrypted; use a trusted segmented LAN
and appropriately scoped firewall rules. Do not reuse household administrator
credentials when a dedicated camera/SIP account is available.

## Audio, images and identity

Enabling the bot sends caller audio to OpenAI. Enabling image analysis sends a
fresh camera image as well. API use may be billable. Provide appropriate caller
and visitor notice and obtain any required permission before recording or
processing conversations/images. This project does not automatically satisfy
privacy or recording-law requirements.

Image analysis is descriptive, not biometric identification. It is instructed
not to infer names, sensitive attributes or identity. Descriptions can still
be wrong; never treat them as proof of who is present. A visitor-provided name
is only a self-report.

Camera JPG storage is optional. Camera-event retention is separate from voice
recording/transcript retention; voice files are not automatically removed by
the camera cleanup setting. Review stored messages regularly and delete them
according to your own policy. `store: false` on image requests is not a blanket
provider-retention guarantee.

## Reporting

Do not post credentials, unredacted captures or private caller information in
GitHub issues. Describe the suspected weakness without sensitive data and
contact the repository owner to arrange private disclosure.
