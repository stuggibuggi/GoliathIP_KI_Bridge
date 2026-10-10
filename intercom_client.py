#!/usr/bin/env python3
"""Windows-Softphone fuer die Goliath/Dahua-Tuersprechanlage.

Registriert sich als SIP-Innenstation an der Tuerstation, kann Anrufe annehmen/
ablehnen/fuehren (echtes Mikrofon+Lautsprecher-Audio per RTP/G.711) und zeigt bei
einem Anruf das Live-Kamerabild der Tuerstation per RTSP an.

Start:
    python intercom_client.py
"""
import asyncio
import audioop
import base64
import hashlib
import json
import os
import queue
import random
import re
import socket
import string
import struct
import sys
import threading
import time
import tkinter as tk
import traceback
import urllib.error
import urllib.request
import urllib.parse
import wave
from tkinter import ttk

import cv2
import numpy as np
import sounddevice as sd
import websockets
from flask import Flask, jsonify, request
from PIL import Image, ImageTk
from werkzeug.serving import make_server
from door_vision import DoorVision, BOT_VISION_RULES
from openai_options import VOICES, OptionsError, fetch_catalog, test_realtime, test_vision

RTP_CLOCK = 8000
SAMPLES_PER_PACKET = 160  # 20ms bei 8kHz
# Manche Fritz!OS-Versionen aktualisieren ihre interne Anruf-Weiterleitungs-
# tabelle bei einem reinen REGISTER-Refresh nicht zuverlaessig -- die
# Registrierung bleibt laut 200 OK gueltig, eingehende Anrufe laufen aber
# irgendwann ins Leere. Ein vollstaendiger Ab-/Neuanmelde-Zyklus (wie er
# beim App-Neustart automatisch passiert) behebt das; hier wird das
# periodisch nachgebildet, ohne die App neu starten zu muessen.
FULL_REREGISTER_INTERVAL = 240  # Sekunden
CONFIG_PATH = os.path.join(os.environ.get("APPDATA", "."), "GoliathIntercom", "config.json")
CRASH_LOG_PATH = os.path.join(os.environ.get("APPDATA", "."), "GoliathIntercom", "crash.log")


def log_crash(text):
    """Schreibt in eine Datei statt nur ins GUI-Protokoll -- wichtig bei einer
    --windowed-.exe ohne Konsole, wo ein stiller Fehler sonst nirgends sichtbar
    waere."""
    try:
        os.makedirs(os.path.dirname(CRASH_LOG_PATH), exist_ok=True)
        with open(CRASH_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def _thread_excepthook(args):
    tb = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    log_crash(f"Unbehandelter Fehler in Thread '{args.thread.name}':\n{tb}")


threading.excepthook = _thread_excepthook


# ---------------------------------------------------------------------------
# SIP-Hilfsfunktionen (Digest-Auth, Nachrichten-Parsing)
# ---------------------------------------------------------------------------

def rand_hex(n=16):
    return "".join(random.choice(string.hexdigits.lower()) for _ in range(n))


def get_local_ip(target_ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 5060))
        return s.getsockname()[0]
    finally:
        s.close()


def parse_sip_message(data):
    text = data.decode("utf-8", errors="replace")
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    start_line = lines[0]
    headers = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        headers.setdefault(key.strip(), []).append(val.strip())
    msg = {"headers": headers, "body": body, "raw": text}
    if start_line.startswith("SIP/2.0"):
        parts = start_line.split(" ", 2)
        msg["type"] = "response"
        msg["status"] = int(parts[1])
        msg["reason"] = parts[2] if len(parts) > 2 else ""
    else:
        parts = start_line.split(" ")
        msg["type"] = "request"
        msg["method"] = parts[0]
        msg["request_uri"] = parts[1] if len(parts) > 1 else ""
    return msg


def header(msg, name, default=""):
    vals = msg["headers"].get(name)
    return vals[0] if vals else default


def parse_auth_challenge(value):
    value = value.split(" ", 1)[1] if value.startswith("Digest") else value
    params = {}
    for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', value):
        params[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    return params


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def parse_sdp(body):
    ip, port = None, None
    for line in body.splitlines():
        if line.startswith("c=IN IP4 "):
            ip = line.split()[-1]
        if line.startswith("m=audio "):
            port = int(line.split()[1])
    return ip, port


def negotiate_codec(body):
    for line in body.splitlines():
        if line.startswith("m=audio "):
            for pt in line.split()[3:]:
                if pt in ("8", "0"):
                    return int(pt)
    return 8


# ---------------------------------------------------------------------------
# Audio: Mikrofon -> RTP/G.711 -> Netzwerk, und Netzwerk -> G.711 -> Lautsprecher
# ---------------------------------------------------------------------------

class AudioEngine:
    def __init__(self, log):
        self.log = log
        self.input_device = None
        self.output_device = None
        self.rtp_sock = None
        self.in_stream = None
        self.out_stream = None
        self.recv_thread = None
        self.running = False
        self.muted = False
        self.test_tone = False
        self.test_tone_pos = 0
        self.test_tone_buf = self._build_test_tone()
        self.remote_addr = None
        self.play_buffer = bytearray()
        self.play_lock = threading.Lock()
        self.seq = random.randint(0, 65535)
        self.timestamp = random.randint(0, 0xFFFFFFFF)
        self.ssrc = random.randint(0, 0xFFFFFFFF)
        self.tx_pt = 8
        self.record_enabled = False
        self.record_dir = "Aufnahmen"
        self.record_file = None
        self.record_path = None

    def start(self, remote_ip, remote_port, local_rtp_port, payload_type):
        self.stop()  # evtl. Reste eines vorherigen Versuchs sauber freigeben
        self.remote_addr = (remote_ip, remote_port)
        self.tx_pt = payload_type
        if self.record_enabled:
            try:
                os.makedirs(self.record_dir, exist_ok=True)
                self.record_path = os.path.join(
                    self.record_dir, f"{time.strftime('%Y-%m-%d_%H-%M-%S')}.wav")
                self.record_file = wave.open(self.record_path, "wb")
                self.record_file.setnchannels(1)
                self.record_file.setsampwidth(2)
                self.record_file.setframerate(RTP_CLOCK)
                self.log(f"[AUDIO] Aufnahme gestartet: {self.record_path}")
            except OSError as e:
                self.log(f"[AUDIO] Aufnahme konnte nicht gestartet werden: {e}")
                self.record_file = None
        try:
            self.rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.rtp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.rtp_sock.bind(("0.0.0.0", local_rtp_port))
            self.rtp_sock.settimeout(0.5)
            self.running = True
            self.play_buffer = bytearray()
            self.in_stream = sd.RawInputStream(
                samplerate=RTP_CLOCK, channels=1, dtype="int16",
                blocksize=SAMPLES_PER_PACKET, device=self.input_device,
                callback=self._on_mic)
            self.out_stream = sd.RawOutputStream(
                samplerate=RTP_CLOCK, channels=1, dtype="int16",
                blocksize=SAMPLES_PER_PACKET, device=self.output_device,
                callback=self._on_speaker)
            self.in_stream.start()
            self.out_stream.start()
        except Exception as e:
            self.log(f"[AUDIO] Fehler beim Start (Port/Mikrofon/Lautsprecher): {e}")
            self.stop()
            return False
        self.recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.recv_thread.start()
        self.log(f"[AUDIO] Gestartet, Ziel {remote_ip}:{remote_port}, Codec PT={payload_type}")
        return True

    def stop(self):
        self.running = False
        for s in (self.in_stream, self.out_stream):
            if s:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass
        self.in_stream = self.out_stream = None
        if self.recv_thread:
            self.recv_thread.join(timeout=1)
        self.recv_thread = None
        if self.rtp_sock:
            try:
                self.rtp_sock.close()
            except OSError:
                pass
            self.rtp_sock = None
        with self.play_lock:
            self.play_buffer = bytearray()
        self.remote_addr = None
        if self.record_file:
            try:
                self.record_file.close()
                self.log(f"[AUDIO] Aufnahme gespeichert: {self.record_path}")
            except Exception:
                pass
            self.record_file = None

    def _build_test_tone(self, freq=440, period_s=1.0, beep_s=0.4):
        period = int(RTP_CLOCK * period_s)
        beep = int(RTP_CLOCK * beep_s)
        t = np.arange(period) / RTP_CLOCK
        tone = np.sin(2 * np.pi * freq * t) * 0.5
        tone[beep:] = 0.0
        fade = min(200, beep)
        if fade > 0:
            tone[beep - fade:beep] *= np.linspace(1, 0, fade)
        return (tone * 32767).astype(np.int16)

    def _next_test_tone_chunk(self, frames):
        buf = self.test_tone_buf
        n = len(buf)
        pos = self.test_tone_pos
        end = pos + frames
        if end <= n:
            chunk = buf[pos:end]
        else:
            chunk = np.concatenate([buf[pos:], buf[: end - n]])
        self.test_tone_pos = end % n
        return chunk.tobytes()

    def _on_mic(self, indata, frames, time_info, status):
        if not self.running or self.remote_addr is None:
            return
        if self.test_tone:
            pcm = self._next_test_tone_chunk(frames)
        elif self.muted:
            pcm = b"\x00\x00" * frames
        else:
            pcm = bytes(indata)
        try:
            payload = audioop.lin2ulaw(pcm, 2) if self.tx_pt == 0 else audioop.lin2alaw(pcm, 2)
        except audioop.error:
            return
        self.seq = (self.seq + 1) & 0xFFFF
        self.timestamp = (self.timestamp + frames) & 0xFFFFFFFF
        pkt = struct.pack("!BBHII", 0x80, self.tx_pt & 0x7F, self.seq, self.timestamp, self.ssrc) + payload
        try:
            self.rtp_sock.sendto(pkt, self.remote_addr)
        except OSError:
            pass

    def _on_speaker(self, outdata, frames, time_info, status):
        need = frames * 2
        with self.play_lock:
            if len(self.play_buffer) >= need:
                chunk = bytes(self.play_buffer[:need])
                del self.play_buffer[:need]
            else:
                chunk = bytes(self.play_buffer) + b"\x00" * (need - len(self.play_buffer))
                self.play_buffer = bytearray()
        outdata[:] = chunk

    def _recv_loop(self):
        cap = RTP_CLOCK * 2  # max. 1s Puffer gegen Latenzaufbau
        while self.running:
            try:
                data, _addr = self.rtp_sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 12:
                continue
            pt = data[1] & 0x7F
            payload = data[12:]
            try:
                if pt == 0:
                    pcm = audioop.ulaw2lin(payload, 2)
                elif pt == 8:
                    pcm = audioop.alaw2lin(payload, 2)
                else:
                    continue
            except audioop.error:
                continue
            with self.play_lock:
                self.play_buffer.extend(pcm)
                if len(self.play_buffer) > cap:
                    del self.play_buffer[: len(self.play_buffer) - cap]
            if self.record_file:
                try:
                    self.record_file.writeframes(pcm)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Video: RTSP-Livebild der Tuerstation (unabhaengig vom SIP-Anruf)
# ---------------------------------------------------------------------------

class VideoPreview:
    def __init__(self):
        self.running = False
        self.thread = None
        self.lock = threading.Lock()
        self.latest = None
        self.error = None

    def start(self, rtsp_url):
        self.stop()
        self.error = None
        self.running = True
        self.thread = threading.Thread(target=self._run, args=(rtsp_url,), daemon=True)
        self.thread.start()

    def _run(self, rtsp_url):
        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            self.error = "Verbindung zum Videostream fehlgeschlagen (URL/Zugangsdaten pruefen)"
            self.running = False
            return
        while self.running:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.1)
                continue
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame)
            img.thumbnail((480, 360))
            with self.lock:
                self.latest = img
        cap.release()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=1)
        self.thread = None
        with self.lock:
            self.latest = None

    def get_frame(self):
        with self.lock:
            return self.latest

    def pop_error(self):
        err, self.error = self.error, None
        return err


# ---------------------------------------------------------------------------
# Bridge: reine Audio-Durchreichung zwischen Tuerstation und Fritzbox, ohne
# PC-Soundkarte. RelayEndpoint erfuellt dasselbe .start()/.stop()-Interface wie
# AudioEngine (Duck-Typing) und kann daher einem SipCallClient als "audio_engine"
# untergeschoben werden, statt echten Ton auszugeben.
# ---------------------------------------------------------------------------

class RelayEndpoint:
    def __init__(self, on_ready, on_stop, log):
        self.on_ready = on_ready
        self.on_stop = on_stop
        self.log = log

    def start(self, remote_ip, remote_port, local_rtp_port, payload_type):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", local_rtp_port))
            sock.settimeout(0.5)
        except OSError as e:
            self.log(f"[BRIDGE] Konnte RTP-Port {local_rtp_port} nicht oeffnen: {e}")
            return False
        self.on_ready(sock, (remote_ip, remote_port), payload_type)
        return True

    def stop(self):
        self.on_stop()


class TransferEndpoint:
    """Reserviert einen eigenen RTP-Port ohne Audiohardware oder Registrierung."""
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.info = None

    def start(self, remote_ip, remote_port, local_rtp_port, payload_type):
        self.info = (self.sock, (remote_ip, remote_port), payload_type)
        return True

    def stop(self):
        self.sock.close()


class BridgeRelay:
    """Transkodiert und leitet RTP-Pakete zwischen zwei Beinen (A und B) weiter."""

    def __init__(self, a_info, b_info, log):
        self.a_sock, self.a_addr, self.a_pt = a_info
        self.b_sock, self.b_addr, self.b_pt = b_info
        self.log = log
        self.running = False
        self.threads = []
        self._state = {
            "a2b": [random.randint(0, 65535), random.randint(0, 0xFFFFFFFF), random.randint(0, 0xFFFFFFFF)],
            "b2a": [random.randint(0, 65535), random.randint(0, 0xFFFFFFFF), random.randint(0, 0xFFFFFFFF)],
        }

    def start(self):
        self.running = True
        t1 = threading.Thread(target=self._forward,
                               args=(self.a_sock, self.b_sock, self.b_addr, self.b_pt, "a2b"), daemon=True)
        t2 = threading.Thread(target=self._forward,
                               args=(self.b_sock, self.a_sock, self.a_addr, self.a_pt, "b2a"), daemon=True)
        self.threads = [t1, t2]
        for t in self.threads:
            t.start()
        self.log("[BRIDGE] Audio-Weiterleitung Tuer <-> Fritzbox gestartet")

    def stop(self):
        self.running = False
        for t in self.threads:
            t.join(timeout=1)
        for s in (self.a_sock, self.b_sock):
            try:
                s.close()
            except OSError:
                pass
        self.log("[BRIDGE] Audio-Weiterleitung beendet")

    @staticmethod
    def _decode(pt, payload):
        try:
            if pt == 0:
                return audioop.ulaw2lin(payload, 2)
            if pt == 8:
                return audioop.alaw2lin(payload, 2)
        except audioop.error:
            return None
        return None

    @staticmethod
    def _encode(pt, pcm):
        try:
            if pt == 0:
                return audioop.lin2ulaw(pcm, 2)
            if pt == 8:
                return audioop.lin2alaw(pcm, 2)
        except audioop.error:
            return None
        return None

    def _forward(self, src_sock, dst_sock, dst_addr, dst_pt, direction):
        state = self._state[direction]
        count = 0
        last_silence_report = time.time()
        while self.running:
            try:
                data, _addr = src_sock.recvfrom(2048)
            except socket.timeout:
                if count == 0 and time.time() - last_silence_report > 3:
                    self.log(f"[BRIDGE] {direction}: noch keine Pakete auf {src_sock.getsockname()} empfangen")
                    last_silence_report = time.time()
                continue
            except OSError:
                break
            if len(data) < 12:
                continue
            src_pt = data[1] & 0x7F
            pcm = self._decode(src_pt, data[12:])
            if pcm is None:
                self.log(f"[BRIDGE] {direction}: unbekannter/nicht unterstuetzter Codec PT={src_pt}, Paket verworfen")
                continue
            out_payload = self._encode(dst_pt, pcm)
            if out_payload is None:
                continue
            count += 1
            if count in (1, 50) or count % 250 == 0:
                self.log(f"[BRIDGE] {direction}: {count} Pakete weitergeleitet "
                         f"(PT {src_pt}->{dst_pt}) an {dst_addr}")
            state[0] = (state[0] + 1) & 0xFFFF
            state[1] = (state[1] + len(pcm) // 2) & 0xFFFFFFFF
            hdr = struct.pack("!BBHII", 0x80, dst_pt & 0x7F, state[0], state[1], state[2])
            try:
                dst_sock.sendto(hdr + out_payload, dst_addr)
            except OSError:
                pass


class BridgeController:
    """Verbindet zwei RelayEndpoints (Tuer-Bein, Fritzbox-Bein) zu einer BridgeRelay,
    sobald beide Seiten ihre Medien-Verbindungsdaten gemeldet haben."""

    def __init__(self, log):
        self.log = log
        self.door_info = None
        self.fritz_info = None
        self.relay = None

    def door_ready(self, sock, remote_addr, pt):
        self.door_info = (sock, remote_addr, pt)
        self._maybe_start()

    def door_stopped(self):
        self.door_info = None
        self._teardown()

    def fritz_ready(self, sock, remote_addr, pt):
        self.fritz_info = (sock, remote_addr, pt)
        self._maybe_start()

    def fritz_stopped(self):
        self.fritz_info = None
        self._teardown()

    def _maybe_start(self):
        if self.door_info and self.fritz_info and not self.relay:
            self.relay = BridgeRelay(self.door_info, self.fritz_info, self.log)
            self.relay.start()

    def _teardown(self):
        if self.relay:
            self.relay.stop()
            self.relay = None


# ---------------------------------------------------------------------------
# KI-Sprachbot: nimmt Anrufe entgegen und fuehrt ein Gespraech per OpenAI
# Realtime API. Die Realtime API akzeptiert G.711 (u-law/A-law) direkt als
# Audioformat -- kein Resampling noetig, RTP-Payloads gehen fast unveraendert
# durch. Erfuellt dasselbe .start()/.stop()-Interface wie AudioEngine/
# RelayEndpoint (Duck-Typing), daher als "audio_engine" bei SipCallClient
# einsetzbar.
#
# Hinweis: Die exakten Feldnamen des Realtime-API-Protokolls (session.update
# etc.) koennen sich mit neueren API-Versionen aendern. Fehler von OpenAI
# werden komplett geloggt, um Anpassungen einfach zu machen.
# ---------------------------------------------------------------------------

class VoiceBotEndpoint:
    def __init__(self, config, log, on_timeout=None, on_transfer=None):
        self.config = config
        self.log = log
        self.on_timeout = on_timeout
        self.on_transfer = on_transfer
        self.transfer_pending = False
        self.bridged = False
        self.media_bridge = None
        self.rtp_receive_paused = threading.Event()
        self.rtp_send_paused = threading.Event()
        self.control_queue = queue.Queue()
        self.transfer_calls = set()
        self.sock = None
        self.thread = None
        self.running = False
        self.remote_addr = None
        self.payload_type = 0

        self.outbound_buf = bytearray()
        try:
            self.output_gain = max(0.0, min(4.0, float(config.get("volume_percent", 100)) / 100.0))
        except (TypeError, ValueError):
            self.output_gain = 1.0
        self.outbound_lock = threading.Lock()
        self.inbound_queue = queue.Queue()

        self.seq = random.randint(0, 65535)
        self.timestamp = random.randint(0, 0xFFFFFFFF)
        self.ssrc = random.randint(0, 0xFFFFFFFF)

        self.last_activity = time.time()
        self.session_start = None
        self.call_dir = None
        self.wav_file = None
        self.seen_event_types = set()
        self.delta_count = 0
        self.transcript_lines = []
        self.response_in_progress = False
        self.pending_hangup = False
        self.pending_hangup_since = None

    def start(self, remote_ip, remote_port, local_rtp_port, payload_type):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("0.0.0.0", local_rtp_port))
            self.sock.settimeout(0.5)
        except OSError as e:
            self.log(f"[BOT] Port {local_rtp_port} nicht verfuegbar: {e}")
            return False
        self.remote_addr = (remote_ip, remote_port)
        self.payload_type = payload_type
        self.running = True
        self.last_activity = time.time()
        self.session_start = time.time()
        self.outbound_buf = bytearray()
        self._prepare_call_dir()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.running = False
        if self.media_bridge:
            self.media_bridge.stop()
            self.media_bridge = None
        if self.thread:
            self.thread.join(timeout=3)
        self.thread = None
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self._finalize_call_dir()

    # -- Aufzeichnung -----------------------------------------------------
    def _prepare_call_dir(self):
        base = self.config.get("save_dir") or "Nachrichten"
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        self.call_dir = os.path.join(base, ts)
        self.wav_file = None
        try:
            os.makedirs(self.call_dir, exist_ok=True)
            self.wav_file = wave.open(os.path.join(self.call_dir, "anruf.wav"), "wb")
            self.wav_file.setnchannels(1)
            self.wav_file.setsampwidth(2)
            self.wav_file.setframerate(RTP_CLOCK)
        except OSError as e:
            self.log(f"[BOT] Konnte Nachrichtenordner nicht anlegen: {e}")
            self.call_dir = None
        self.transcript_lines = []

    def _finalize_call_dir(self):
        if self.wav_file:
            try:
                self.wav_file.close()
            except Exception:
                pass
            self.wav_file = None
        if self.call_dir and self.transcript_lines:
            try:
                with open(os.path.join(self.call_dir, "transkript.txt"), "w", encoding="utf-8") as f:
                    f.write("\n".join(self.transcript_lines))
                self.log(f"[BOT] Nachricht gespeichert unter {self.call_dir}")
            except OSError as e:
                self.log(f"[BOT] Konnte Transkript nicht speichern: {e}")
        self.call_dir = None

    def _add_transcript(self, line):
        stamp = time.strftime("%H:%M:%S")
        self.transcript_lines.append(f"[{stamp}] {line}")
        self.log(f"[BOT] {line}")

    # -- RTP <-> Puffer -----------------------------------------------------
    def _decode(self, payload):
        try:
            if self.payload_type == 0:
                return audioop.ulaw2lin(payload, 2)
            if self.payload_type == 8:
                return audioop.alaw2lin(payload, 2)
        except audioop.error:
            return None
        return None

    def _rtp_recv_loop(self):
        while self.running:
            if self.bridged:
                self.rtp_receive_paused.set()
                time.sleep(0.1)
                continue
            try:
                data, _addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 12:
                continue
            payload = data[12:]
            if not self.transfer_pending and not self.bridged:
                self.inbound_queue.put(payload)
            pcm = self._decode(payload)
            if pcm and self.wav_file:
                try:
                    self.wav_file.writeframes(pcm)
                except Exception:
                    pass

    def _rtp_send_loop(self):
        frame_bytes = SAMPLES_PER_PACKET  # 160 Byte G.711 = 20ms
        next_tick = time.time()
        sent_count = 0
        last_empty_report = time.time()
        while self.running:
            if self.bridged:
                self.rtp_send_paused.set()
                time.sleep(0.1)
                continue
            with self.outbound_lock:
                if len(self.outbound_buf) >= frame_bytes:
                    chunk = bytes(self.outbound_buf[:frame_bytes])
                    del self.outbound_buf[:frame_bytes]
                else:
                    chunk = None
            if chunk:
                # G.711 erst dekodieren; PCM-Verstaerkung saettigt bei 16 Bit
                # statt bei Uebersteuerung umzuschlagen. RTP-Timing bleibt gleich.
                if self.output_gain != 1.0:
                    decode = audioop.ulaw2lin if self.payload_type == 0 else audioop.alaw2lin
                    encode = audioop.lin2ulaw if self.payload_type == 0 else audioop.lin2alaw
                    chunk = encode(audioop.mul(decode(chunk, 2), 2, self.output_gain), 2)
                self.seq = (self.seq + 1) & 0xFFFF
                self.timestamp = (self.timestamp + SAMPLES_PER_PACKET) & 0xFFFFFFFF
                hdr = struct.pack("!BBHII", 0x80, self.payload_type & 0x7F,
                                   self.seq, self.timestamp, self.ssrc)
                try:
                    self.sock.sendto(hdr + chunk, self.remote_addr)
                    sent_count += 1
                    if sent_count in (1, 25) or sent_count % 250 == 0:
                        self.log(f"[BOT] {sent_count} Audio-Pakete an Tuerstation gesendet "
                                 f"(PT={self.payload_type}, Ziel={self.remote_addr})")
                except OSError as e:
                    self.log(f"[BOT] Fehler beim Senden an Tuerstation: {e}")
            elif sent_count == 0 and time.time() - last_empty_report > 3:
                self.log("[BOT] Noch keine Audiodaten von OpenAI zum Senden vorhanden")
                last_empty_report = time.time()
            next_tick += 0.02
            delay = next_tick - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.time()

    def _watchdog_loop(self):
        silence_timeout = float(self.config.get("silence_timeout", 20))
        max_duration = float(self.config.get("max_duration", 180))
        self.log(f"[BOT] Stille-Watchdog gestartet (Timeout={silence_timeout:.0f}s, "
                 f"Max.Dauer={max_duration:.0f}s)")
        last_report = time.time()
        while self.running:
            time.sleep(1)
            if self.transfer_pending or self.bridged:
                continue
            if self.pending_hangup:
                with self.outbound_lock:
                    buffered = len(self.outbound_buf)
                waited = time.time() - (self.pending_hangup_since or time.time())
                if (not self.response_in_progress and buffered == 0) or waited > 15:
                    self.log(f"[BOT] Verabschiedung gesendet -- beende Gespraech "
                             f"(end_call, nach {waited:.1f}s)")
                    self._trigger_hangup()
                    return
                continue
            idle = time.time() - self.last_activity
            if time.time() - last_report > 5:
                self.log(f"[BOT] Watchdog: {idle:.0f}s seit letzter Aktivitaet "
                         f"(Grenze {silence_timeout:.0f}s), running={self.running}")
                last_report = time.time()
            if idle > silence_timeout:
                self.log(f"[BOT] {silence_timeout:.0f}s Stille -- beende Gespraech (rufe on_timeout auf)")
                self._trigger_hangup()
                self.log(f"[BOT] on_timeout aufgerufen, Watchdog beendet sich")
                return
            if self.session_start and time.time() - self.session_start > max_duration:
                self.log(f"[BOT] Maximale Gespraechsdauer ({max_duration:.0f}s) erreicht -- beende")
                self._trigger_hangup()
                return
        self.log("[BOT] Watchdog beendet (self.running wurde False, bevor Timeout erreicht war)")

    def _trigger_hangup(self):
        self.log("[BOT] _trigger_hangup: setze running=False und rufe on_timeout-Callback auf")
        self.running = False
        if self.on_timeout:
            self.on_timeout()

    # -- Hauptschleife / OpenAI Realtime -------------------------------------
    def _run(self):
        rtp_recv_t = threading.Thread(target=self._rtp_recv_loop, daemon=True)
        rtp_send_t = threading.Thread(target=self._rtp_send_loop, daemon=True)
        watchdog_t = threading.Thread(target=self._watchdog_loop, daemon=True)
        rtp_recv_t.start()
        rtp_send_t.start()
        watchdog_t.start()
        try:
            asyncio.run(self._openai_session())
        except Exception as e:
            self.log(f"[BOT] Fehler in der OpenAI-Sitzung: {e}")
        finally:
            if self.bridged:
                # RTP-Threads bleiben fuer das durchgestellte Gespraech aktiv.
                pass
            else:
                self._finish_openai_run(rtp_recv_t, rtp_send_t)

    def _finish_openai_run(self, rtp_recv_t, rtp_send_t):
        was_running = self.running
        self.running = False
        rtp_recv_t.join(timeout=1)
        rtp_send_t.join(timeout=1)
        if was_running:
            self.log("[BOT] OpenAI-Sitzung unerwartet beendet, waehrend der Anruf "
                     "noch aktiv war -- lege automatisch auf")
            if self.on_timeout:
                self.on_timeout()

    async def _openai_session(self):
        # GA-Schema (seit 12.05.2026, loest die alte Beta-API ab): verschachtelt unter
        # session.audio.input/output statt flacher Felder, session.type = "realtime",
        # Audioformat als MIME-artiges Objekt {"type": "audio/pcmu"|"audio/pcma"}.
        audio_mime = "audio/pcmu" if self.payload_type == 0 else "audio/pcma"
        model = self.config.get("model", "gpt-realtime")
        url = f"wss://api.openai.com/v1/realtime?model={model}"
        headers = {
            "Authorization": f"Bearer {self.config.get('api_key', '')}",
        }
        async with websockets.connect(url, additional_headers=headers, max_size=None) as ws:
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "model": model,
                    "instructions": self.config.get("instructions", ""),
                    "audio": {
                        "input": {
                            "format": {"type": audio_mime},
                            # far_field + hoehere Schwellenwerte: Die Tuerstation hat keine
                            # Echo-Unterdrueckung, das Mikrofon hoert den eigenen Lautsprecher.
                            # Ohne dies haelt der VAD das Echo der Bot-Stimme fuer den Anrufer
                            # und unterbricht den Bot staendig selbst (beobachtet: staerkeres
                            # "Stottern" durch Dauer-Selbstunterbrechung).
                            "noise_reduction": {"type": "far_field"},
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": 0.7,
                                "prefix_padding_ms": 300,
                                "silence_duration_ms": 500,
                                "interrupt_response": True,
                                "create_response": True,
                            },
                            "transcription": {"model": "whisper-1"},
                        },
                        "output": {
                            "format": {"type": audio_mime},
                            "voice": self.config.get("voice", "alloy"),
                        },
                    },
                    "tools": ([{
                        "type": "function", "name": "transfer_call",
                        "description": "Verbindet mit der freigegebenen Zielnummer. Nur auf Wunsch des Anrufers. Kuendige zuerst kurz die Verbindung an. Bei Fehlschlag biete eine Nachricht an. Waehle niemals selbst eine andere Nummer.",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    }] if self.config.get("transfer_enabled") else []) + [{
                        "type": "function",
                        "name": "end_call",
                        "description": (
                            "Beendet den Anruf. WICHTIG: Rufe dies erst auf, NACHDEM du dich "
                            "bereits verbal vom Anrufer verabschiedet hast (z.B. 'Auf "
                            "Wiederhoeren!' oder 'Tschuess!'). Nutze diese Funktion, sobald klar "
                            "ist, dass das Gespraech zu Ende ist -- der Anrufer sich verabschiedet, "
                            "sein Anliegen erledigt ist, oder er erkennbar auflegen moechte."
                        ),
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    }],
                    "tool_choice": "auto",
                },
            }))
            await ws.send(json.dumps({"type": "response.create"}))
            self.log("[BOT] Mit OpenAI Realtime verbunden, Begruessung wird ausgeloest")

            uplink_task = asyncio.create_task(self._uplink(ws))
            try:
                while self.running and not self.bridged:
                    try:
                        result = None if self.response_in_progress else self.control_queue.get_nowait()
                    except queue.Empty:
                        result = None
                    if result:
                        await ws.send(json.dumps(result))
                        if result.get("item", {}).get("type") == "function_call_output":
                            await ws.send(json.dumps({"type": "response.create"}))
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        break
                    try:
                        self._handle_event(json.loads(raw))
                    except (json.JSONDecodeError, KeyError) as e:
                        self.log(f"[BOT] Konnte OpenAI-Nachricht nicht verarbeiten: {e}")
            finally:
                uplink_task.cancel()

    async def _uplink(self, ws):
        loop = asyncio.get_event_loop()
        while self.running:
            payload = await loop.run_in_executor(None, self._get_inbound)
            if payload is None:
                continue
            if self.transfer_pending or self.bridged:
                continue
            b64 = base64.b64encode(payload).decode("ascii")
            try:
                await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64}))
            except websockets.exceptions.ConnectionClosed:
                return

    def _get_inbound(self):
        try:
            return self.inbound_queue.get(timeout=0.5)
        except queue.Empty:
            return None

    def _handle_event(self, ev):
        kind = ev.get("type", "")
        if kind not in self.seen_event_types:
            self.seen_event_types.add(kind)
            self.log(f"[BOT] Neuer Ereignistyp von OpenAI: {kind}")

        if kind == "response.output_audio.delta":
            audio = base64.b64decode(ev.get("delta", ""))
            with self.outbound_lock:
                self.outbound_buf.extend(audio)
            self.delta_count += 1
            if self.delta_count in (1, 25) or self.delta_count % 250 == 0:
                self.log(f"[BOT] {self.delta_count} Audio-Chunks von OpenAI empfangen "
                         f"(zuletzt {len(audio)} Byte)")
            self.last_activity = time.time()
        elif kind == "conversation.item.input_audio_transcription.completed":
            text = ev.get("transcript", "").strip()
            if text:
                self._add_transcript(f"Anrufer: {text}")
                self.last_activity = time.time()
        elif kind == "response.output_audio_transcript.done":
            text = ev.get("transcript", "").strip()
            if text:
                self._add_transcript(f"Bot: {text}")
        elif kind == "error":
            self.log(f"[BOT] OpenAI-Fehler: {ev.get('error', ev)}")
        elif kind == "session.updated":
            self.log(f"[BOT] Sitzung bestaetigt: {ev.get('session', {})}")
        elif kind == "response.created":
            self.response_in_progress = True
        elif kind == "response.done":
            self.response_in_progress = False
            # Falls das Modell end_call waehrend dieser Antwort aufgerufen hat, ist
            # jetzt die komplette Verabschiedung bei OpenAI fertig generiert -- der
            # Watchdog wartet zusaetzlich noch, bis der Ausgangspuffer leer gesendet
            # ist, bevor er auflegt (siehe _watchdog_loop).
        elif kind == "response.function_call_arguments.done":
            if ev.get("name") == "transfer_call":
                call_id = ev.get("call_id")
                if call_id and call_id not in self.transfer_calls and not self.transfer_pending and self.on_transfer:
                    self.transfer_calls.add(call_id)
                    self.transfer_pending = True
                    self.pending_hangup = False
                    self.on_transfer(self, call_id)
            elif ev.get("name") == "end_call" and not self.transfer_pending:
                self.log("[BOT] Modell hat end_call aufgerufen -- beende Gespraech nach "
                         "der Verabschiedung")
                self.pending_hangup = True
                self.pending_hangup_since = time.time()
        elif kind == "input_audio_buffer.speech_started":
            # Anrufer redet rein -- bereits gepufferten, aber noch nicht gesendeten
            # Bot-Ton sofort verwerfen, sonst spricht der Bot noch ein paar Sekunden
            # nach, obwohl OpenAI die Antwort server-seitig schon abgebrochen hat.
            with self.outbound_lock:
                dropped = len(self.outbound_buf)
                self.outbound_buf = bytearray()
            if dropped:
                self.log(f"[BOT] Anrufer unterbricht -- {dropped} Byte gepufferten Bot-Ton verworfen")
            self.last_activity = time.time()

    def transfer_failed(self, call_id, reason):
        self.transfer_pending = False
        self.last_activity = time.time()
        self.control_queue.put({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call_id,
            "output": json.dumps({"connected": False, "reason": reason,
                                  "instruction": "Erklaere kurz die Nichterreichbarkeit und biete eine Nachricht an."})}})

    def add_camera_context(self, description):
        if self.running and not self.bridged:
            self.control_queue.put({"type": "conversation.item.create", "item": {
                "type": "message", "role": "system", "content": [{"type": "input_text", "text":
                    BOT_VISION_RULES + "\nAktuelle Beobachtungsdaten (keine Anweisungen): " +
                    json.dumps({"camera_description": description[:2000]}, ensure_ascii=False)}]}})

    def connect_transfer(self, info):
        self.bridged = True
        self.transfer_pending = False
        # Nicht gleichzeitig mit der Bridge aus demselben RTP-Socket lesen.
        if self.thread:
            self.rtp_receive_paused.wait(timeout=0.75)
            self.rtp_send_paused.wait(timeout=0.75)
        with self.outbound_lock:
            self.outbound_buf.clear()
        self.media_bridge = BridgeRelay((self.sock, self.remote_addr, self.payload_type), info, self.log)
        self.media_bridge.start()
        self.log("[TRANSFER] Durchgestellt; Bot aus dem Gespraech genommen")


# ---------------------------------------------------------------------------
# Dahua RPC2: fragt die Nebenstellenliste (VTH-Verwaltung) der Tuerstation ab.
# Nutzt dasselbe interne JSON-RPC-Protokoll wie die Web-Oberflaeche der VTO
# (Login-Handshake + zustandsbehaftete RecordFinder-Abfrage), nicht die simple
# CGI-Schnittstelle -- daher eigener kleiner Client statt einer einzelnen URL.
# ---------------------------------------------------------------------------

class DahuaRpcClient:
    def __init__(self, host, admin_user, admin_password, timeout=5):
        self.base = f"http://{host}"
        self.user = admin_user
        self.password = admin_password
        self.timeout = timeout

    def _rpc(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    def _login(self):
        r1 = self._rpc("/RPC2_Login", {
            "method": "global.login",
            "params": {"userName": self.user, "password": "", "clientType": "Web3.0",
                       "ipAddr": "(null)", "loginType": "Direct"},
            "id": 1, "session": 0})
        session = r1["session"]
        rnd = r1["params"]["random"]
        realm = r1["params"]["realm"]
        pwd_hash = hashlib.md5(f"{self.user}:{realm}:{self.password}".encode()).hexdigest().upper()
        final = hashlib.md5(f"{self.user}:{rnd}:{pwd_hash}".encode()).hexdigest().upper()
        r2 = self._rpc("/RPC2_Login", {
            "method": "global.login",
            "params": {"userName": self.user, "password": final, "clientType": "Web3.0",
                       "ipAddr": "(null)", "loginType": "Direct",
                       "authorityType": "Default", "passwordType": "Default"},
            "id": 2, "session": session})
        if r2.get("result") is not True:
            raise RuntimeError(f"Login an der Tuerstation fehlgeschlagen: {r2}")
        return session

    def get_room_list(self):
        session = self._login()
        rid = [2]

        def call(method, params=None, obj=None):
            rid[0] += 1
            payload = {"method": method, "id": rid[0], "session": session}
            if params is not None:
                payload["params"] = params
            if obj is not None:
                payload["object"] = obj
            return self._rpc("/RPC2", payload)

        r_create = call("RecordFinder.factory.create", {"name": "VideoTalkContact"})
        obj = r_create.get("result")
        if not isinstance(obj, int):
            raise RuntimeError(f"Abfrage konnte nicht gestartet werden: {r_create}")
        try:
            call("RecordFinder.startFind", {"condition": {"Type": "VTH"}}, obj=obj)
            r_find = call("RecordFinder.doFind", {"count": 100}, obj=obj)
            return r_find.get("params", {}).get("records", [])
        finally:
            call("RecordFinder.destroy", {}, obj=obj)


# ---------------------------------------------------------------------------
# SIP-Client: Registrierung + Anrufsteuerung (laeuft in eigenem Thread)
# ---------------------------------------------------------------------------

class SipCallClient:
    def __init__(self, server_ip, server_port, user, password, domain,
                 local_port, rtp_port, events, audio_engine, leg="door", register_on_run=True):
        self.server_ip = server_ip
        self.server_port = server_port
        self.user = user
        self.password = password
        self.domain = domain
        self.local_ip = get_local_ip(server_ip)
        self.local_port = local_port
        self.rtp_port = rtp_port
        self.events = events
        self.audio = audio_engine
        self.leg = leg
        self.register_on_run = register_on_run

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.local_ip, self.local_port))
        self.local_port = self.sock.getsockname()[1]
        self.sock.settimeout(0.2)

        self.commands = queue.Queue()
        self.running = True

        self.reg_call_id = rand_hex(20) + "@" + self.local_ip
        self.reg_from_tag = rand_hex(8)
        self.cseq = 0
        self.registered = False
        self.registered_until = 0
        self.last_expires = 3600
        self.reg_pending = None
        self.last_full_reregister = time.time()

        self.call = None  # dict, siehe _start_outgoing_call/_on_request(INVITE)
        self.call_timeout = 30

    def emit(self, kind, **data):
        data["type"] = kind
        data["leg"] = self.leg
        self.events.put(data)

    def log(self, text):
        self.emit("log", text=text)

    # -- oeffentliche, thread-sichere Befehle -----------------------------
    def cmd_call(self, target):
        self.commands.put(("call", target))

    def cmd_answer(self, expected_call_id=None, audio_engine=None):
        self.commands.put(("answer", expected_call_id, audio_engine))

    def cmd_reject(self):
        self.commands.put(("reject",))

    def cmd_hangup(self):
        self.commands.put(("hangup",))

    def cmd_camera_context(self, call_id, description):
        self.commands.put(("camera_context", call_id, description))

    def cmd_stop(self):
        self.commands.put(("stop",))

    # -- SIP-Grundbausteine -------------------------------------------------
    def next_cseq(self):
        self.cseq += 1
        return self.cseq

    def uri(self, user=None):
        return f"sip:{user or self.user}@{self.domain}"

    def build_auth_header(self, method, uri, challenge):
        realm = challenge.get("realm", self.domain)
        nonce = challenge.get("nonce", "")
        qop = challenge.get("qop")
        opaque = challenge.get("opaque")
        ha1 = md5(f"{self.user}:{realm}:{self.password}")
        ha2 = md5(f"{method}:{uri}")
        parts = [f'Authorization: Digest username="{self.user}"', f'realm="{realm}"',
                 f'nonce="{nonce}"', f'uri="{uri}"', "algorithm=MD5"]
        if qop:
            nc, cnonce = "00000001", rand_hex(8)
            response = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
            parts += [f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
        else:
            response = md5(f"{ha1}:{nonce}:{ha2}")
        parts.append(f'response="{response}"')
        if opaque:
            parts.append(f'opaque="{opaque}"')
        return ", ".join(parts)

    def build_local_sdp(self):
        return (
            f"v=0\r\no=IntercomClient 0 0 IN IP4 {self.local_ip}\r\ns=IntercomClient\r\n"
            f"c=IN IP4 {self.local_ip}\r\nt=0 0\r\n"
            f"m=audio {self.rtp_port} RTP/AVP 8 0\r\n"
            f"a=rtpmap:8 PCMA/8000\r\na=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n"
        )

    def send_raw(self, msg, dest=None):
        first_line = msg.split("\r\n", 1)[0]
        self.log(f">> {first_line}")
        self.sock.sendto(msg.encode(), dest or (self.server_ip, self.server_port))

    def route_destination(self, route_set):
        if not route_set:
            return None
        m = re.search(r'@([\w\.\-]+):(\d+)', route_set[0])
        if m:
            return (m.group(1), int(m.group(2)))
        return None

    def send_response(self, req, status, reason, extra_headers=None, body="", to_tag=None):
        lines = [f"SIP/2.0 {status} {reason}"]
        for v in req["headers"].get("Via", []):
            lines.append(f"Via: {v}")
        lines.append(f"From: {header(req, 'From')}")
        to_hdr = header(req, "To")
        if "tag=" not in to_hdr and status != 100:
            # Dasselbe Tag MUSS bei allen Antworten auf dasselbe INVITE und bei
            # spaeteren In-Dialog-Anfragen (BYE) wiederverwendet werden, sonst
            # erkennt die Gegenseite den Dialog nicht wieder (z.B. 481 beim BYE).
            to_hdr += f";tag={to_tag or rand_hex(8)}"
        lines.append(f"To: {to_hdr}")
        lines.append(f"Call-ID: {header(req, 'Call-ID')}")
        lines.append(f"CSeq: {header(req, 'CSeq')}")
        if extra_headers:
            lines.extend(extra_headers)
        lines.append(f"Content-Length: {len(body)}")
        self.send_raw("\r\n".join(lines) + "\r\n\r\n" + body)

    # -- Registrierung --------------------------------------------------
    def build_register(self, expires, auth_header=None):
        cseq = self.next_cseq()
        branch = "z9hG4bK" + rand_hex(12)
        lines = [
            f"REGISTER sip:{self.domain} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch};rport",
            "Max-Forwards: 70",
            f"From: <{self.uri()}>;tag={self.reg_from_tag}",
            f"To: <{self.uri()}>",
            f"Call-ID: {self.reg_call_id}",
            f"CSeq: {cseq} REGISTER",
            f"Contact: <sip:{self.user}@{self.local_ip}:{self.local_port}>",
            f"Expires: {expires}",
            "User-Agent: IntercomClient/1.0",
        ]
        if auth_header:
            lines.append(auth_header)
        lines.append("Content-Length: 0")
        return "\r\n".join(lines) + "\r\n\r\n", cseq

    def start_register(self, expires=3600):
        msg, cseq = self.build_register(expires)
        self.send_raw(msg)
        self.reg_pending = {"cseq": cseq, "expires": expires, "sent_at": time.time()}

    def full_reregister(self):
        # Explizite Ab- (Expires: 0) und sofortige Neuanmeldung statt reinem
        # Refresh -- siehe FULL_REREGISTER_INTERVAL weiter oben. Die Antwort
        # auf das De-Register wird bewusst nicht abgewartet/verfolgt, nur die
        # anschliessende frische Registrierung zaehlt.
        msg, _ = self.build_register(0)
        self.send_raw(msg)
        self.log("[INFO] Regelmaessige volle Neu-Registrierung (De-Register + Register)")
        self.start_register()

    def maybe_reregister(self):
        if self.reg_pending is not None:
            # Keine Antwort erhalten (Paket verloren, Server kurz weg, etc.) --
            # ohne diese Zeitueberschreitung bleibt reg_pending fuer immer
            # gesetzt und es wird NIE wieder ein REGISTER geschickt, ohne dass
            # irgendein Fehler im Log auftaucht.
            if time.time() - self.reg_pending.get("sent_at", 0) > 8:
                self.log("[WARNUNG] Keine Antwort auf REGISTER erhalten -- versuche erneut")
                self.reg_pending = None
            else:
                return
        if (self.leg == "fritz" and self.registered and self.call is None
                and time.time() - self.last_full_reregister > FULL_REREGISTER_INTERVAL):
            # self.call is None: nicht mitten in einem laufenden Gespraech
            # de-/neu-registrieren. Eigentlich betrifft das laut SIP nur das
            # Anmelde-Binding, nicht den laufenden Dialog -- aber sicherheits-
            # halber wird der Zyklus verschoben, bis die Leitung frei ist.
            self.last_full_reregister = time.time()
            self.full_reregister()
            return
        margin = max(5, min(15, self.last_expires // 3))
        if not self.registered or time.time() > self.registered_until - margin:
            self.start_register()

    def on_register_response(self, msg):
        if self.reg_pending is None:
            return
        cseq_num = header(msg, "CSeq").split(" ")[0]
        if cseq_num != str(self.reg_pending["cseq"]):
            return
        if msg["status"] in (401, 407):
            hdr_name = "WWW-Authenticate" if msg["status"] == 401 else "Proxy-Authenticate"
            challenge = parse_auth_challenge(header(msg, hdr_name))
            auth = self.build_auth_header("REGISTER", f"sip:{self.domain}", challenge)
            expires = self.reg_pending["expires"]
            out, cseq = self.build_register(expires, auth)
            self.send_raw(out)
            self.reg_pending = {"cseq": cseq, "expires": expires, "sent_at": time.time()}
        elif msg["status"] == 200:
            expires = int(header(msg, "Expires", str(self.reg_pending["expires"])))
            self.last_expires = expires
            self.registered_until = time.time() + expires
            was_registered = self.registered
            self.registered = True
            self.reg_pending = None
            if not was_registered:
                self.emit("registered")
                self.log(f"Registriert als {self.user} (Expires {expires}s)")
        else:
            self.registered = False
            self.reg_pending = None
            self.emit("reg_failed", reason=f"{msg['status']} {msg['reason']}")

    def deregister_blocking(self):
        try:
            self.sock.settimeout(1.0)
            msg, cseq = self.build_register(0)
            self.send_raw(msg)
            data, _ = self.sock.recvfrom(65535)
            resp = parse_sip_message(data)
            if resp["type"] == "response" and resp["status"] in (401, 407):
                hdr_name = "WWW-Authenticate" if resp["status"] == 401 else "Proxy-Authenticate"
                challenge = parse_auth_challenge(header(resp, hdr_name))
                auth = self.build_auth_header("REGISTER", f"sip:{self.domain}", challenge)
                out, cseq = self.build_register(0, auth)
                self.send_raw(out)
                self.sock.recvfrom(65535)
        except (socket.timeout, OSError):
            pass

    # -- Ausgehender Anruf ------------------------------------------------
    def build_invite(self, target, call_id, from_tag, auth_header=None):
        cseq = self.next_cseq()
        branch = "z9hG4bK" + rand_hex(12)
        sdp = self.build_local_sdp()
        lines = [
            f"INVITE sip:{target}@{self.domain} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch}",
            "Max-Forwards: 70",
            f"From: <{self.uri()}>;tag={from_tag}",
            f"To: <{self.uri(target)}>",
            f"Call-ID: {call_id}",
            f"CSeq: {cseq} INVITE",
            f"Contact: <sip:{self.user}@{self.local_ip}:{self.local_port}>",
            "Content-Type: application/sdp",
        ]
        if auth_header:
            lines.append(auth_header)
        lines.append(f"Content-Length: {len(sdp)}")
        return "\r\n".join(lines) + "\r\n\r\n" + sdp, cseq, branch

    def _start_outgoing_call(self, target):
        if self.call is not None:
            self.emit("call_failed", reason="Bereits ein Gespraech aktiv")
            return
        call_id = rand_hex(20) + "@" + self.local_ip
        from_tag = rand_hex(8)
        msg, cseq, branch = self.build_invite(target, call_id, from_tag)
        self.send_raw(msg)
        self.call = {
            "role": "caller", "state": "ringing_out", "call_id": call_id,
            "from_tag": from_tag, "target": target, "invite_cseq": cseq,
            "branch": branch, "auth_sent": False, "sent_at": time.time(),
            "deadline": time.time() + self.call_timeout,
        }
        self.emit("calling", target=target)
        self.log(f"Rufe {target} an ...")

    def send_ack(self, resp, call_id, invite_cseq, target, from_tag, branch=None):
        branch = branch or ("z9hG4bK" + rand_hex(12))
        # Enthaelt die Antwort Record-Route, MUSS das ACK diese als Route-Header
        # uebernehmen und an den Contact der Gegenseite gehen (RFC3261 12.1.2/12.2.1.1) --
        # sonst kann der B2BUA (die Tuerstation) das ACK seiner internen Sitzung nicht
        # zuordnen und wiederholt endlos das 200 OK.
        route_set = resp["headers"].get("Record-Route", [])
        contact = header(resp, "Contact").strip("<>")
        request_uri = contact or f"sip:{target}@{self.domain}"
        lines = [
            f"ACK {request_uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch}",
            "Max-Forwards: 70",
            f"From: <{self.uri()}>;tag={from_tag}",
            f"To: {header(resp, 'To')}",
            f"Call-ID: {call_id}",
            f"CSeq: {invite_cseq} ACK",
        ]
        for rr in route_set:
            lines.append(f"Route: {rr}")
        lines.append("Content-Length: 0")
        self.send_raw("\r\n".join(lines) + "\r\n\r\n", dest=self.route_destination(route_set))

    def send_invite_ack(self, msg, call_id, cseq, target, from_tag, branch):
        # ACK fuer eine finale NICHT-2xx-Antwort (401/403/486/487/...) auf ein von
        # uns gesendetes INVITE -- Pflicht nach RFC3261 17.1.1.3, selbes Branch/
        # CSeq wie das urspruengliche INVITE. Ohne dieses ACK wiederholt der
        # Server die Antwort endlos (beobachtet: > 10 Minuten Dauerschleife).
        lines = [
            f"ACK sip:{target}@{self.domain} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch}",
            "Max-Forwards: 70",
            f"From: <{self.uri()}>;tag={from_tag}",
            f"To: {header(msg, 'To')}",
            f"Call-ID: {call_id}",
            f"CSeq: {cseq} ACK",
            "Content-Length: 0",
        ]
        self.send_raw("\r\n".join(lines) + "\r\n\r\n")

    def send_cancel(self):
        c = self.call
        lines = [
            f"CANCEL sip:{c['target']}@{self.domain} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={c['branch']}",
            "Max-Forwards: 70",
            f"From: <{self.uri()}>;tag={c['from_tag']}",
            f"To: <{self.uri(c['target'])}>",
            f"Call-ID: {c['call_id']}",
            f"CSeq: {c['invite_cseq']} CANCEL",
            "Content-Length: 0",
        ]
        self.send_raw("\r\n".join(lines) + "\r\n\r\n")

    def send_bye_dialog(self, call_id, target, from_tag, to_hdr, route_set=None, remote_contact=None):
        cseq = self.next_cseq()
        branch = "z9hG4bK" + rand_hex(12)
        route_set = route_set or []
        request_uri = remote_contact or f"sip:{target}@{self.domain}"
        lines = [
            f"BYE {request_uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch}",
            "Max-Forwards: 70",
            f"From: <{self.uri()}>;tag={from_tag}",
            f"To: {to_hdr}",
            f"Call-ID: {call_id}",
            f"CSeq: {cseq} BYE",
        ]
        for rr in route_set:
            lines.append(f"Route: {rr}")
        lines.append("Content-Length: 0")
        self.send_raw("\r\n".join(lines) + "\r\n\r\n", dest=self.route_destination(route_set))

    def send_bye_to_caller(self, invite_req):
        branch = "z9hG4bK" + rand_hex(12)
        cseq = self.next_cseq()
        # Muss dasselbe Tag verwenden, das wir schon in der 200-OK-Antwort auf
        # dieses INVITE benutzt haben -- sonst ordnet die Gegenseite das BYE
        # keinem bekannten Dialog zu (z.B. 481 Call Leg/Transaction Does Not Exist).
        local_tag = self.call.get("local_tag") if self.call else None
        local_tag = local_tag or rand_hex(8)
        lines = [
            f"BYE {header(invite_req, 'Contact').strip('<>')} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch}",
            "Max-Forwards: 70",
            f"From: <{self.uri()}>;tag={local_tag}",
            f"To: {header(invite_req, 'From')}",
            f"Call-ID: {header(invite_req, 'Call-ID')}",
            f"CSeq: {cseq} BYE",
            "Content-Length: 0",
        ]
        self.send_raw("\r\n".join(lines) + "\r\n\r\n")

    def on_invite_response(self, msg):
        c = self.call
        if c is None or c["role"] != "caller":
            return
        cseq_num = header(msg, "CSeq").split(" ")[0]
        if cseq_num != str(c["invite_cseq"]):
            return
        if msg["status"] in (401, 407) and not c["auth_sent"]:
            # ACK die (unauthentifizierte) Original-Anfrage, BEVOR die neue
            # authentifizierte INVITE geschickt wird -- Pflicht, sonst
            # wiederholt der Server die 401/407-Antwort endlos.
            self.send_invite_ack(msg, c["call_id"], c["invite_cseq"], c["target"], c["from_tag"], c["branch"])
            hdr_name = "WWW-Authenticate" if msg["status"] == 401 else "Proxy-Authenticate"
            challenge = parse_auth_challenge(header(msg, hdr_name))
            uri = f"sip:{c['target']}@{self.domain}"
            auth = self.build_auth_header("INVITE", uri, challenge)
            out, cseq, branch = self.build_invite(c["target"], c["call_id"], c["from_tag"], auth)
            self.send_raw(out)
            c["invite_cseq"] = cseq
            c["branch"] = branch
            c["auth_sent"] = True
            c["deadline"] = time.time() + self.call_timeout
            return
        if msg["status"] == 180:
            self.emit("ringing_out")
            return
        if msg["status"] < 200:
            return
        if msg["status"] == 200:
            self.send_ack(msg, c["call_id"], c["invite_cseq"], c["target"], c["from_tag"], branch=c["branch"])
            if c["state"] == "cancelling":
                self.send_bye_dialog(c["call_id"], c["target"], c["from_tag"], header(msg, "To"),
                                     route_set=msg["headers"].get("Record-Route", []),
                                     remote_contact=header(msg, "Contact").strip("<>"))
                self.call = None
                return
            if c.get("confirmed"):
                self.log("(200 OK erneut erhalten -- Gegenseite hat unser ACK vermutlich nicht "
                          "erkannt; ACK wurde erneut gesendet, Anruf bleibt wie er ist)")
                return
            c["invite_resp"] = msg
            c["route_set"] = msg["headers"].get("Record-Route", [])
            c["remote_contact"] = header(msg, "Contact").strip("<>")
            remote_ip, remote_port = parse_sdp(msg["body"])
            pt = negotiate_codec(msg["body"])
            if self.audio.start(remote_ip, remote_port, self.rtp_port, pt):
                c["state"] = "connected"
                c["confirmed"] = True
                self.emit("call_connected", peer=c["target"])
                self.log(f"Verbunden mit {c['target']}")
            else:
                self.send_bye_dialog(c["call_id"], c["target"], c["from_tag"], header(msg, "To"),
                                      route_set=c["route_set"], remote_contact=c["remote_contact"])
                self.emit("call_failed", reason="Audio konnte nicht gestartet werden")
                self.call = None
        else:
            self.send_invite_ack(msg, c["call_id"], c["invite_cseq"], c["target"], c["from_tag"], c["branch"])
            if c["state"] != "cancelling":
                self.emit("call_failed", reason=f"{msg['status']} {msg['reason']}")
            self.call = None

    # -- Eingehender Anruf --------------------------------------------------
    def on_invite_request(self, req, addr):
        if self.call is not None:
            self.log(f"[WARNUNG] Eingehender Anruf von {header(req, 'From')} abgewiesen "
                      f"(486 Busy Here) -- es ist bereits ein Anruf aktiv (Zustand: {self.call.get('state')})")
            self.send_response(req, 486, "Busy Here")
            return
        call_id = header(req, "Call-ID")
        local_tag = rand_hex(8)
        self.call = {
            "role": "callee", "state": "ringing_in", "call_id": call_id,
            "invite_req": req, "deadline": time.time() + 45, "local_tag": local_tag,
        }
        self.send_response(req, 100, "Trying")
        self.send_response(req, 180, "Ringing", to_tag=local_tag)
        caller = header(req, "From")
        self.emit("incoming_call", caller=caller, call_id=call_id)
        self.log(f"Eingehender Anruf von {caller}")

    def answer_call(self):
        c = self.call
        if c is None or c["role"] != "callee" or c["state"] != "ringing_in":
            return
        req = c["invite_req"]
        sdp = self.build_local_sdp()
        self.send_response(req, 200, "OK",
                            [f"Contact: <sip:{self.user}@{self.local_ip}:{self.local_port}>",
                             "Content-Type: application/sdp"], body=sdp, to_tag=c["local_tag"])
        remote_ip, remote_port = parse_sdp(req["body"])
        pt = negotiate_codec(req["body"])
        if self.audio.start(remote_ip, remote_port, self.rtp_port, pt):
            c["state"] = "connected"
            self.emit("call_connected", peer=header(req, "From"))
            self.log("Anruf angenommen")
        else:
            self.send_bye_to_caller(req)
            self.emit("call_failed", reason="Audio konnte nicht gestartet werden")
            self.call = None

    def reject_call(self):
        c = self.call
        if c is None or c["role"] != "callee":
            return
        self.send_response(c["invite_req"], 486, "Busy Here", to_tag=c.get("local_tag"))
        self.emit("call_ended", reason="abgelehnt")
        self.call = None

    def hangup(self):
        c = self.call
        if c is None:
            return
        if c["role"] == "callee" and c["state"] == "ringing_in":
            self.reject_call()
            return
        if c["role"] == "caller" and c["state"] == "ringing_out":
            self.send_cancel()
            # self.call bewusst NICHT sofort loeschen: die Gegenseite schickt
            # gleich noch eine finale Antwort (i.d.R. 487) auf das urspruengliche
            # INVITE, die wir per ACK bestaetigen muessen (siehe on_invite_response).
            # Sonst wiederholt der Server die Antwort endlos. _check_timeouts()
            # raeumt "cancelling" notfalls nach ein paar Sekunden auf.
            c["state"] = "cancelling"
            c["deadline"] = time.time() + 10
            self.emit("call_ended", reason="abgebrochen")
            return
        if c["state"] == "connected":
            self.audio.stop()
            if c["role"] == "caller":
                self.send_bye_dialog(c["call_id"], c["target"], c["from_tag"],
                                      header(c["invite_resp"], "To"),
                                      route_set=c.get("route_set"),
                                      remote_contact=c.get("remote_contact"))
            else:
                self.send_bye_to_caller(c["invite_req"])
            self.emit("call_ended", reason="aufgelegt")
            self.call = None

    def on_bye(self, req):
        self.send_response(req, 200, "OK")
        if self.call is not None:
            self.audio.stop()
            self.emit("call_ended", reason="Gegenseite hat aufgelegt")
            self.call = None

    def on_cancel(self, req):
        self.send_response(req, 200, "OK")
        c = self.call
        if c is not None and c.get("invite_req") is not None:
            self.send_response(c["invite_req"], 487, "Request Terminated", to_tag=c.get("local_tag"))
        if self.call is not None:
            self.audio.stop()
            self.emit("call_ended", reason="Anruf abgebrochen")
            self.call = None

    # -- Hauptschleife ---------------------------------------------------
    def run(self):
        if self.register_on_run:
            self.start_register()
        while self.running:
            try:
                self._drain_commands()
                if self.register_on_run:
                    self.maybe_reregister()
                self._check_timeouts()
                try:
                    data, addr = self.sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                msg = parse_sip_message(data)
                if msg["type"] == "response":
                    self.log(f"<< SIP/2.0 {msg['status']} {msg['reason']} "
                             f"(CSeq: {header(msg, 'CSeq')}, From-addr: {addr})")
                    cseq_method = header(msg, "CSeq").split(" ")[-1] if header(msg, "CSeq") else ""
                    if cseq_method == "INVITE" and msg["status"] == 200:
                        self.log("---- volle 200-OK-Nachricht ----\n" + msg["raw"] + "\n---- Ende ----")
                else:
                    self.log(f"<< {msg['method']} (CSeq: {header(msg, 'CSeq')}, From-addr: {addr})")
                    if msg["method"] == "INVITE":
                        self.log("---- eingehende INVITE-Nachricht ----\n" + msg["raw"] + "\n---- Ende ----")
                if msg["type"] == "response":
                    method = header(msg, "CSeq").split(" ")[-1] if header(msg, "CSeq") else ""
                    if method == "REGISTER" and self.reg_pending:
                        self.on_register_response(msg)
                    elif method == "INVITE":
                        self.on_invite_response(msg)
                else:
                    self._on_request(msg, addr)
            except Exception as e:
                log_crash(f"Fehler in SIP-Schleife (leg={self.leg}):\n{traceback.format_exc()}")
                self.log(f"[FEHLER] Unerwarteter Fehler in der SIP-Schleife: {e}")
                if self.call is not None:
                    self.audio.stop()
                    self.emit("call_ended", reason="Interner Fehler")
                    self.call = None

        if self.registered:
            self.deregister_blocking()
        self.sock.close()
        self.emit("stopped")

    def _on_request(self, req, addr):
        method = req["method"]
        if method == "OPTIONS":
            self.send_response(req, 200, "OK", ["Allow: INVITE, ACK, BYE, CANCEL, OPTIONS"])
        elif method == "INVITE":
            self.on_invite_request(req, addr)
        elif method == "BYE":
            self.on_bye(req)
        elif method == "CANCEL":
            self.on_cancel(req)
        # ACK und Unbekanntes: keine Antwort noetig / ignorieren

    def _drain_commands(self):
        while True:
            try:
                cmd = self.commands.get_nowait()
            except queue.Empty:
                return
            if cmd[0] == "call":
                self._start_outgoing_call(cmd[1])
            elif cmd[0] == "answer":
                if len(cmd) > 1 and cmd[1] is not None:
                    if not self.call or self.call["call_id"] != cmd[1] or self.call["state"] != "ringing_in":
                        continue
                    if cmd[2] is not None:
                        self.audio = cmd[2]
                self.answer_call()
            elif cmd[0] == "camera_context":
                if (self.call and self.call.get("call_id") == cmd[1]
                        and isinstance(self.audio, VoiceBotEndpoint)):
                    self.audio.add_camera_context(cmd[2])
            elif cmd[0] == "reject":
                self.reject_call()
            elif cmd[0] == "hangup":
                self.hangup()
            elif cmd[0] == "stop":
                self.running = False

    def _check_timeouts(self):
        c = self.call
        if c is None:
            return
        if time.time() > c.get("deadline", 0):
            if c["state"] == "ringing_in":
                self.reject_call()
                self.emit("call_ended", reason="Zeitueberschreitung (nicht angenommen)")
            elif c["state"] == "ringing_out":
                self.send_cancel()
                self.emit("call_failed", reason="Zeitueberschreitung (keine Antwort)")
                c["state"] = "cancelling"
                c["deadline"] = time.time() + 10
            elif c["state"] == "cancelling":
                # Auf das CANCEL kam nie eine finale Antwort (487 o.ae.) --
                # nach Ablauf der Frist Zustand zwangsweise raeumen, sonst
                # bleibt der Anruf fuer immer haengen.
                self.log("[WARNUNG] Keine finale Antwort auf CANCEL erhalten -- Anrufzustand wird zwangsweise geraeumt")
                self.call = None


# ---------------------------------------------------------------------------
# Web-Admin: lokaler Flask-Server zur Fernkonfiguration/-ueberwachung, laeuft
# parallel zur Desktop-Oberflaeche im selben Prozess. Config-Aenderungen aus
# dem Web-Thread werden ueber root.after() in den Tkinter-Hauptthread
# marshalliert, da Tkinter selbst nicht thread-sicher ist.
# ---------------------------------------------------------------------------

ADMIN_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<title>Goliath Intercom -- Web-Admin</title>
<style>
body{font-family:-apple-system,Segoe UI,sans-serif;max-width:900px;margin:20px auto;padding:0 16px;
     background:#1a1a1a;color:#e0e0e0}
h1{font-size:20px}
h2{font-size:15px;border-bottom:1px solid #444;padding-bottom:4px;margin-top:28px}
.grid{display:grid;grid-template-columns:180px 1fr;gap:6px 10px;align-items:center;margin:10px 0}
input[type=text],input[type=password],textarea,select{width:100%;box-sizing:border-box;background:#2a2a2a;
     color:#eee;border:1px solid #555;border-radius:4px;padding:5px}
textarea{height:80px;font-family:inherit}
button{background:#3a6ea5;color:#fff;border:none;border-radius:4px;padding:7px 14px;cursor:pointer;margin:4px 4px 4px 0}
button:hover{background:#4a7eb5}
.status{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0}
.pill{padding:4px 10px;border-radius:12px;background:#333;font-size:13px}
.pill.on{background:#2d6a2d}
.pill.off{background:#5a2d2d}
#log{background:#111;color:#9f9;font-family:Consolas,monospace;font-size:12px;height:260px;
     overflow-y:scroll;padding:8px;border-radius:4px;white-space:pre-wrap}
.msg{font-size:13px;padding:3px 0;border-bottom:1px solid #333}
label{font-size:13px;color:#aaa}
</style></head>
<body>
<h1>Goliath Intercom -- Web-Admin</h1>

<h2>Status</h2>
<div class="status" id="status">Lade ...</div>

<h2>Konfiguration</h2>
<div class="grid">
  <label>Tuerstation Server-IP</label><input id="server" type="text">
  <label>Tuerstation SIP-Port</label><input id="port" type="text">
  <label>SIP-Domaene</label><input id="domain" type="text">
  <label>Tuerstation Benutzer</label><input id="user" type="text">
  <label>Tuerstation Passwort</label><input id="pass" type="password" placeholder="unveraendert lassen = leer">
  <label>Fritzbox Server-IP</label><input id="fritz_server" type="text">
  <label>Fritzbox Benutzer</label><input id="fritz_user" type="text">
  <label>Fritzbox Passwort</label><input id="fritz_pass" type="password" placeholder="unveraendert lassen = leer">
  <label>Fritzbox Zielnummer</label><input id="fritz_target" type="text">
  <label>Kamera RTSP-URL</label><input id="rtsp" type="password" placeholder="unveraendert lassen = leer; rtsp://USER:PASS@HOST:554/...">
  <label>Bot API-Key</label><input id="bot_key" type="password" placeholder="unveraendert lassen = leer">
  <label>Realtime-Modell</label><input id="bot_model" type="text" list="realtimeModels"><datalist id="realtimeModels"></datalist>
</div>
<button onclick="refreshModels()">Modelle aktualisieren</button>
<span id="modelStatus">Modelle noch nicht von API geladen</span>
<p>Kandidaten nach Modellnamen gefiltert, Kompatibilitaet separat testen. Eigene Modell-ID bleibt moeglich.</p>

<h2>Bot: Tuerklingel -- Ansprache</h2>
<label>Stimme</label><select id="bot_voice"></select>
<button onclick="testModel('door')">Modell/Stimme testen</button>
<label>Bot-Annahme nach (0-40 s)</label><input id="bot_answer_delay" type="number" min="0" max="40" step="1">
<p><label><input id="transfer_enabled" type="checkbox"> Weiterleitung auf Wunsch erlauben</label></p>
<label>Zielnummer (via Fritzbox)</label><input id="transfer_target" type="text">
<label>Klingelzeit (s)</label><input id="transfer_timeout" type="number" min="5" max="120">
<label>Bot-Lautstaerke (%)</label><input id="bot_volume" type="number" min="0" max="400" step="25">
<textarea id="bot_prompt"></textarea>

<h2>Bot: Fritzbox (**620) -- Ansprache</h2>
<label>Stimme</label><select id="bot_voice_fritz"></select>
<button onclick="testModel('fritz')">Modell/Stimme testen</button>
<p><label><input id="transfer_enabled_fritz" type="checkbox"> Weiterleitung auf Wunsch erlauben</label></p>
<label>Zielnummer (via Fritzbox)</label><input id="transfer_target_fritz" type="text">
<label>Klingelzeit (s)</label><input id="transfer_timeout_fritz" type="number" min="5" max="120">
<label>Bot-Lautstaerke (%)</label><input id="bot_volume_fritz" type="number" min="0" max="400" step="25">
<textarea id="bot_prompt_fritz"></textarea>

<button onclick="saveConfig()">Speichern</button>
<span id="saveMsg"></span>

<h2>Kameraanalyse und Klingelprotokoll</h2>
<p>Aktivierung sendet das aktuelle Klingelbild an OpenAI. Keine Gesichtserkennung oder Identitaetsbestimmung.</p>
<label><input id="vision_enabled" type="checkbox"> Bildanalyse aktivieren</label>
<label><input id="vision_save_images" type="checkbox"> Bilder lokal speichern</label>
<label>Bildanalyse-Modell</label><input id="vision_model" type="text" list="visionModels"><datalist id="visionModels"></datalist>
<button onclick="testModel('vision')">Bildmodell testen (API-Kosten)</button>
<p>Der Bildmodell-Test sendet nur ein synthetisches Testbild, keine Kameraaufnahme.</p>
<div id="testResult"></div>
<label>Bild/Protokoll loeschen nach Tagen (1-365)</label><input id="vision_retention" type="number" min="1" max="365">
<button onclick="saveConfig()">Einstellungen speichern</button>
<pre id="doorEvents">Noch keine Klingelereignisse geladen.</pre>

<h2>Nachrichten (letzte)</h2>
<div id="messages">Lade ...</div>

<h2>Protokoll</h2>
<div id="log"></div>

<script>
function fillOptions(id, values, selected){
  const field = document.getElementById(id);
  field.replaceChildren();
  for (const value of [...new Set([...values, ...(selected ? [selected] : [])])]) {
    const option = document.createElement('option'); option.value = value; option.textContent = value; field.append(option);
  }
  if (selected) field.value = selected;
}
async function refreshModels(){
  const status = document.getElementById('modelStatus'); status.textContent = 'Lade Modelle ...';
  try {
    const r = await fetch('/api/openai/models', {method:'POST'}); const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Modellabruf fehlgeschlagen');
    fillOptions('realtimeModels', data.realtime_models, ''); fillOptions('visionModels', data.vision_models, '');
    status.textContent = data.note + ' (' + data.all_models.length + ' Modelle)';
  } catch (error) { status.textContent = error.message + '; bisherige Auswahl bleibt erhalten'; }
}
async function testModel(kind){
  if (kind === 'vision' && !confirm('Synthetisches Testbild an OpenAI senden? API-Kosten koennen anfallen.')) return;
  const result = document.getElementById('testResult'); result.textContent = 'Pruefe ...';
  const model = document.getElementById(kind === 'vision' ? 'vision_model' : 'bot_model').value;
  const voice = document.getElementById(kind === 'fritz' ? 'bot_voice_fritz' : 'bot_voice').value;
  try {
    const r = await fetch('/api/openai/test', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({kind,model,voice})});
    const data = await r.json(); result.textContent = data.text || data.error || 'Test fehlgeschlagen';
  } catch (error) { result.textContent = 'Test nicht erreichbar'; }
}
async function loadConfig(){
  const r = await fetch('/api/config'); const c = await r.json();
  for (const k of ['server','port','domain','user','fritz_server','fritz_user','fritz_target','bot_model'])
    document.getElementById(k).value = c[k] || '';
  document.getElementById('bot_prompt').value = c.bot_prompt || '';
  document.getElementById('bot_prompt_fritz').value = c.bot_prompt_fritz || '';
  for (const k of ['bot_volume','bot_volume_fritz']) document.getElementById(k).value = c[k] ?? 100;
  document.getElementById('bot_answer_delay').value = c.bot_answer_delay ?? '10';
  for (const k of ['vision_enabled','vision_save_images']) document.getElementById(k).checked = !!c[k];
  document.getElementById('vision_model').value = c.vision_model ?? 'gpt-4.1-mini';
  fillOptions('bot_voice', c.openai_voices || [], c.bot_voice || 'alloy');
  fillOptions('bot_voice_fritz', c.openai_voices || [], c.bot_voice_fritz || 'alloy');
  fillOptions('realtimeModels', c.realtime_models || [], '');
  fillOptions('visionModels', c.vision_models || [], '');
  document.getElementById('vision_retention').value = c.vision_retention ?? '7';
  for (const suffix of ['', '_fritz']) {
    document.getElementById('transfer_enabled'+suffix).checked = !!c['transfer_enabled'+suffix];
    document.getElementById('transfer_target'+suffix).value = c['transfer_target'+suffix] ?? '**610';
    document.getElementById('transfer_timeout'+suffix).value = c['transfer_timeout'+suffix] ?? '30';
  }
}
async function saveConfig(){
  const data = {};
  for (const k of ['bot_voice','bot_voice_fritz']) data[k] = document.getElementById(k).value;
  for (const k of ['vision_enabled','vision_save_images']) data[k] = document.getElementById(k).checked;
  for (const k of ['vision_model','vision_retention']) data[k] = document.getElementById(k).value;
  for (const suffix of ['', '_fritz']) {
    data['transfer_enabled'+suffix] = document.getElementById('transfer_enabled'+suffix).checked;
    data['transfer_target'+suffix] = document.getElementById('transfer_target'+suffix).value;
    data['transfer_timeout'+suffix] = document.getElementById('transfer_timeout'+suffix).value;
  }
  for (const k of ['server','port','domain','user','pass','rtsp','fritz_server','fritz_user','fritz_pass','fritz_target','bot_key','bot_model','bot_prompt','bot_prompt_fritz','bot_volume','bot_volume_fritz','bot_answer_delay']){
    const v = document.getElementById(k).value;
    if (v !== '') data[k] = v;
  }
  await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)});
  document.getElementById('saveMsg').textContent = 'Gespeichert.';
  setTimeout(()=>document.getElementById('saveMsg').textContent='', 2000);
}
function pill(label, on){ return `<span class="pill ${on?'on':'off'}">${label}: ${on?'ja':'nein'}</span>`; }
async function refreshStatus(){
  const r = await fetch('/api/status'); const s = await r.json();
  document.getElementById('status').innerHTML =
    pill('Tuer registriert', s.door_registered) + pill('Fritzbox registriert', s.fritz_registered) +
    pill('Bot aktiv', s.bot_active) + pill('Bridge aktiv', s.auto_bridge_active) +
    `<span class="pill">Tuer-Anruf: ${s.door_call || '-'}</span>` +
    `<span class="pill">Fritz-Anruf: ${s.fritz_call || '-'}</span>`;
}
async function refreshLog(){
  try {
    const events = await (await fetch('/api/door-events')).json();
    document.getElementById('doorEvents').textContent = events.map(e => e.timestamp + '\\n' + (e.description || e.error || '') + (e.image ? '\\nBild: '+e.image : '')).join('\\n\\n') || 'Noch keine Klingelereignisse.';
  } catch (_) {}
  const r = await fetch('/api/log?n=200'); const j = await r.json();
  const el = document.getElementById('log');
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
  el.textContent = j.lines.join('\\n');
  if (atBottom) el.scrollTop = el.scrollHeight;
}
async function refreshMessages(){
  const r = await fetch('/api/messages'); const j = await r.json();
  document.getElementById('messages').innerHTML = j.messages.length
    ? j.messages.map(m => `<div class="msg">[${m.channel}] ${m.name}</div>`).join('')
    : '<div class="msg">Keine Nachrichten.</div>';
}
loadConfig();
refreshStatus(); refreshLog(); refreshMessages();
setInterval(refreshStatus, 3000);
setInterval(refreshLog, 2000);
setInterval(refreshMessages, 10000);
</script>
</body></html>
"""


class AdminServer:
    def __init__(self, app_ref, port=8765):
        self.app_ref = app_ref
        self.port = port
        self.flask_app = Flask(__name__)
        self.flask_app.url_map.strict_slashes = False
        self.server = None
        self.thread = None
        self._setup_routes()

    def _setup_routes(self):
        fa = self.flask_app
        a = self.app_ref

        @fa.route("/")
        def index():
            return ADMIN_HTML

        @fa.route("/api/config", methods=["GET"])
        def get_config():
            cfg = dict(on_ui(a._collect_config))
            cfg['openai_voices'] = list(VOICES)
            cfg['realtime_models'] = a.model_catalog.get('realtime_models', [])
            cfg['vision_models'] = a.model_catalog.get('vision_models', [])
            for key in ("pass", "fritz_pass", "bot_key", "rtsp"):
                if cfg.get(key):
                    cfg[key] = ""
            return jsonify(cfg)

        @fa.route("/api/config", methods=["POST"])
        def set_config():
            data = request.get_json(force=True, silent=True) or {}
            if not isinstance(data, dict):
                return jsonify({'error': 'Konfiguration muss ein Objekt sein'}), 400
            for key in ("pass", "fritz_pass", "bot_key", "rtsp"):
                if data.get(key) == "":
                    data.pop(key, None)
            def apply():
                a._apply_config(data)
                a._save_config()
            try:
                on_ui(apply)
                return jsonify({"ok": True})
            except OptionsError as error:
                return jsonify({'error': str(error)}), 503

        def on_ui(callback):
            # No background thread may read or modify Tk variables directly.
            if threading.current_thread() is threading.main_thread():
                return callback()
            done, result = threading.Event(), {}
            def execute():
                try:
                    result['value'] = callback()
                except Exception:
                    result['error'] = True
                finally:
                    done.set()
            a.events.put({'type': 'ui_callback', 'callback': execute})
            if not done.wait(3) or result.get('error'):
                raise OptionsError('Anwendungsoberflaeche antwortet nicht')
            return result['value']

        @fa.route('/api/openai/models', methods=['POST'])
        def models():
            try:
                key = on_ui(lambda: a.var_bot_key.get().strip())
                catalog = fetch_catalog(key)
                on_ui(lambda: a._set_model_catalog(catalog))
                return jsonify(catalog)
            except OptionsError as error:
                return jsonify({'error': str(error)}), 400
            except Exception:
                return jsonify({'error': 'Modellabruf fehlgeschlagen'}), 502

        @fa.route('/api/openai/test', methods=['POST'])
        def model_test():
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict) or data.get('kind') not in ('door', 'fritz', 'vision'):
                return jsonify({'error': 'Ungueltiger Testtyp'}), 400
            try:
                key = on_ui(lambda: a.var_bot_key.get().strip())
                if data['kind'] == 'vision':
                    text = test_vision(key, data.get('model'))
                else:
                    text = test_realtime(key, data.get('model'), data.get('voice'))
                return jsonify({'ok': True, 'text': text})
            except OptionsError as error:
                return jsonify({'error': str(error)}), 400
            except Exception:
                return jsonify({'error': 'Modelltest fehlgeschlagen'}), 502

        @fa.route("/api/status")
        def status():
            return jsonify({
                "door_registered": bool(a.sip and a.sip.registered),
                "fritz_registered": bool(a.sip_fritz and a.sip_fritz.registered),
                "door_call": a.sip.call["state"] if (a.sip and a.sip.call) else None,
                "fritz_call": a.sip_fritz.call["state"] if (a.sip_fritz and a.sip_fritz.call) else None,
                "bot_active": a.bot_active,
                "auto_bridge_active": a.auto_bridge_active,
            })

        @fa.route("/api/log")
        def log():
            n = request.args.get("n", 200, type=int)
            return jsonify({"lines": list(a.log_lines)[-n:]})

        @fa.route("/api/messages")
        def messages():
            base = on_ui(lambda: a.var_bot_dir.get().strip())
            result = []
            for sub in ("tuer", "fritzbox"):
                d = os.path.join(base, sub)
                if os.path.isdir(d):
                    for name in sorted(os.listdir(d), reverse=True)[:50]:
                        result.append({"channel": sub, "name": name})
            return jsonify({"messages": result})

        @fa.route("/api/door-events")
        def door_events():
            return jsonify(a.door_vision.recent())

        @fa.route("/api/crashlog")
        def crashlog():
            try:
                with open(CRASH_LOG_PATH, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                text = "(kein Absturz-Log vorhanden)"
            return jsonify({"text": text[-20000:]})

    def start(self):
        try:
            self.server = make_server("127.0.0.1", self.port, self.flask_app, threaded=True)
        except OSError as e:
            self.app_ref._log(f"[WEB] Admin-Server konnte Port {self.port} nicht oeffnen: {e}")
            return
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.app_ref._log(f"[WEB] Admin-Server gestartet auf Port {self.port}")

    def stop(self):
        if self.server:
            self.server.shutdown()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        root.title("Goliath Intercom Client")
        root.geometry("640x820")
        root.minsize(560, 600)

        self.log_lines = []
        self.events = queue.Queue()
        self.audio = AudioEngine(self._log)
        self.audio.record_dir = os.path.join(
            os.environ.get("APPDATA", "."), "GoliathIntercom", "Aufnahmen")
        self.video = VideoPreview()
        self.sip = None
        self.sip_thread = None
        self.call_start = None

        self.sip_fritz = None
        self.sip_fritz_thread = None
        self.bridge = BridgeController(self._log)
        self.door_relay = RelayEndpoint(self.bridge.door_ready, self.bridge.door_stopped, self._log)
        self.fritz_relay = RelayEndpoint(self.bridge.fritz_ready, self.bridge.fritz_stopped, self._log)
        self.auto_bridge_active = False
        self.auto_bridge_pending = False
        self.reverse_bridge_pending = False
        self.bot_active = False
        self.bot_transfer = None
        self.transfer_serial = 0
        self.door_bot_pending = None
        self.door_vision = DoorVision(os.path.join(os.environ.get("APPDATA", "."), "GoliathIntercom", "Klingelprotokoll"))
        self.door_vision_results = {}
        self.vision_cleanup_at = 0

        self.devices_in, self.devices_out = self._list_devices()
        self.admin_server = None
        self.model_catalog = {'all_models': [], 'realtime_models': [], 'vision_models': [], 'voices': list(VOICES)}
        self.model_refresh_running = False
        self.voice_dropdowns = []

        self._build_ui()
        self._load_config()
        self.admin_server = AdminServer(self, port=8765)
        self.admin_server.start()
        try:
            self.lbl_admin_url.config(text="Web-Admin: http://localhost:8765 (nur lokal)")
        except OSError:
            self.lbl_admin_url.config(text="Web-Admin: http://localhost:8765")
        self.root.report_callback_exception = self._report_callback_exception
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll)

    def _report_callback_exception(self, exc_type, exc_value, exc_tb):
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log_crash(f"Tkinter-Callback-Fehler:\n{tb}")
        try:
            self._log(f"[FEHLER] Interner Anzeige-Fehler (siehe crash.log): {exc_value}")
        except Exception:
            pass

    def _list_devices(self):
        ins, outs = [("System-Standard", None)], [("System-Standard", None)]
        try:
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0:
                    ins.append((d["name"], i))
                if d["max_output_channels"] > 0:
                    outs.append((d["name"], i))
        except Exception as e:
            self._log(f"[AUDIO] Geraeteliste nicht verfuegbar: {e}")
        return ins, outs

    def _build_ui(self):
        topbar = ttk.Frame(self.root)
        topbar.pack(fill="x", padx=8, pady=(6, 0))
        ttk.Button(topbar, text="Einstellungen speichern", command=self._save_config).pack(side="left")
        ttk.Button(topbar, text="Absturz-Log oeffnen", command=self._on_open_crash_log).pack(side="left", padx=4)
        self.lbl_admin_url = ttk.Label(topbar, text="Web-Admin: --")
        self.lbl_admin_url.pack(side="left", padx=12)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        tab_door = ttk.Frame(self.notebook)
        tab_fritz = ttk.Frame(self.notebook)
        tab_bot = ttk.Frame(self.notebook)
        self.notebook.add(tab_door, text="Tuerstation")
        self.notebook.add(tab_fritz, text="Fritzbox-Bridge")
        self.notebook.add(tab_bot, text="KI-Sprachbot")

        conn = ttk.LabelFrame(tab_door, text="Verbindung zur Tuerstation")
        conn.pack(fill="x", padx=8, pady=6)

        self.var_server = tk.StringVar(value="")
        self.var_port = tk.StringVar(value="5060")
        self.var_user = tk.StringVar(value="")
        self.var_pass = tk.StringVar(value="")
        self.var_domain = tk.StringVar(value="VDP")
        self.var_rtsp = tk.StringVar(
            value="")

        self._labeled_entry(conn, "Server-IP", self.var_server, 0)
        self._labeled_entry(conn, "Port", self.var_port, 1)
        self._labeled_entry(conn, "Benutzername", self.var_user, 2)
        self._labeled_entry(conn, "Passwort", self.var_pass, 3, show="*")
        self._labeled_entry(conn, "Domaene", self.var_domain, 4)
        self._labeled_entry(conn, "RTSP-URL (Video)", self.var_rtsp, 5, show="*")

        row = ttk.Frame(conn)
        row.grid(row=6, column=0, columnspan=2, sticky="we", pady=4)
        self.btn_connect = ttk.Button(row, text="Verbinden", command=self._on_connect)
        self.btn_connect.pack(side="left", padx=4)
        self.lbl_status = ttk.Label(row, text="Nicht verbunden", foreground="red")
        self.lbl_status.pack(side="left", padx=8)

        devf = ttk.LabelFrame(tab_door, text="Audiogeraete")
        devf.pack(fill="x", padx=8, pady=6)
        ttk.Label(devf, text="Mikrofon:").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        self.cmb_in = ttk.Combobox(devf, values=[n for n, _ in self.devices_in], state="readonly", width=40)
        self.cmb_in.current(0)
        self.cmb_in.grid(row=0, column=1, padx=4, pady=2)
        ttk.Label(devf, text="Lautsprecher:").grid(row=1, column=0, sticky="w", padx=4, pady=2)
        self.cmb_out = ttk.Combobox(devf, values=[n for n, _ in self.devices_out], state="readonly", width=40)
        self.cmb_out.current(0)
        self.cmb_out.grid(row=1, column=1, padx=4, pady=2)

        callf = ttk.LabelFrame(tab_door, text="Anruf")
        callf.pack(fill="x", padx=8, pady=6)
        ttk.Label(callf, text="Zimmernr.:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.var_target = tk.StringVar(value="9901#0")
        ttk.Entry(callf, textvariable=self.var_target, width=15).grid(row=0, column=1, padx=4)
        self.btn_call = ttk.Button(callf, text="Anrufen", command=self._on_call, state="disabled")
        self.btn_call.grid(row=0, column=2, padx=4)
        self.btn_hangup = ttk.Button(callf, text="Auflegen", command=self._on_hangup, state="disabled")
        self.btn_hangup.grid(row=0, column=3, padx=4)
        self.btn_mute = ttk.Button(callf, text="Stumm", command=self._on_mute_toggle, state="disabled")
        self.btn_mute.grid(row=0, column=4, padx=4)
        self.btn_tone = ttk.Button(callf, text="Testton", command=self._on_tone_toggle, state="disabled")
        self.btn_tone.grid(row=0, column=5, padx=4)
        self.lbl_call_state = ttk.Label(callf, text="Bereit")
        self.lbl_call_state.grid(row=1, column=0, columnspan=5, sticky="w", padx=4, pady=4)

        self.var_record = tk.BooleanVar(value=False)
        ttk.Checkbutton(callf, text="Aufnahme (Diagnose): empfangenes Audio als WAV mitschneiden",
                         variable=self.var_record, command=self._on_record_toggled
                         ).grid(row=2, column=0, columnspan=4, sticky="w", padx=4, pady=2)
        ttk.Button(callf, text="Aufnahmen oeffnen", command=self._on_open_recordings_dir
                   ).grid(row=2, column=4, columnspan=2, sticky="w", padx=4, pady=2)

        self.incoming_frame = ttk.LabelFrame(tab_door, text="Eingehender Anruf")
        self.lbl_caller = ttk.Label(self.incoming_frame, text="")
        self.lbl_caller.pack(side="left", padx=8, pady=6)
        ttk.Button(self.incoming_frame, text="Annehmen", command=self._on_answer).pack(side="left", padx=4)
        ttk.Button(self.incoming_frame, text="Ablehnen", command=self._on_reject).pack(side="left", padx=4)

        videof = ttk.LabelFrame(tab_door, text="Video")
        videof.pack(fill="both", expand=True, padx=8, pady=6)
        self.video_label = ttk.Label(videof, text="(kein Bild)", anchor="center")
        self.video_label.pack(fill="both", expand=True, padx=4, pady=4)
        self.btn_video = ttk.Button(videof, text="Bild anzeigen", command=self._on_video_toggle)
        self.btn_video.pack(pady=4)

        fritzf = ttk.LabelFrame(tab_fritz, text="Fritzbox-Bridge")
        fritzf.pack(fill="x", padx=8, pady=6)

        self.var_fritz_server = tk.StringVar(value="")
        self.var_fritz_user = tk.StringVar(value="")
        self.var_fritz_pass = tk.StringVar(value="")
        # Hinweis: NICHT "**620" als Standard verwenden -- das ist die Nummer,
        # unter der unsere eigene App als Tuersprechanlage registriert ist.
        # Ein Testanruf dorthin waere ein Selbstanruf und haengt sich in einer
        # Dauerschleife auf (401/CANCEL/487).
        self.var_fritz_target = tk.StringVar(value="**610")

        self._labeled_entry(fritzf, "Fritzbox-IP", self.var_fritz_server, 0)
        self._labeled_entry(fritzf, "Benutzername", self.var_fritz_user, 1)
        self._labeled_entry(fritzf, "Passwort", self.var_fritz_pass, 2, show="*")
        self._labeled_entry(fritzf, "Ziel (Fritzfon-Nr.)", self.var_fritz_target, 3)

        frow = ttk.Frame(fritzf)
        frow.grid(row=4, column=0, columnspan=2, sticky="we", pady=4)
        self.btn_fritz_connect = ttk.Button(frow, text="Verbinden", command=self._on_fritz_connect)
        self.btn_fritz_connect.pack(side="left", padx=4)
        self.lbl_fritz_status = ttk.Label(frow, text="Nicht verbunden", foreground="red")
        self.lbl_fritz_status.pack(side="left", padx=8)

        trow = ttk.Frame(fritzf)
        trow.grid(row=5, column=0, columnspan=2, sticky="we", pady=4)
        self.btn_fritz_call = ttk.Button(trow, text="Testanruf", command=self._on_fritz_call, state="disabled")
        self.btn_fritz_call.pack(side="left", padx=4)
        self.btn_fritz_hangup = ttk.Button(trow, text="Auflegen", command=self._on_fritz_hangup, state="disabled")
        self.btn_fritz_hangup.pack(side="left", padx=4)
        self.lbl_fritz_call_state = ttk.Label(trow, text="")
        self.lbl_fritz_call_state.pack(side="left", padx=8)

        self.var_auto_bridge = tk.BooleanVar(value=False)
        ttk.Checkbutton(fritzf, text="Automatische Weiterleitung: Tuer -> Fritzbox",
                         variable=self.var_auto_bridge, command=self._on_auto_bridge_toggled
                         ).grid(row=6, column=0, columnspan=2, sticky="w", padx=4, pady=4)

        botf = ttk.LabelFrame(tab_bot, text="KI-Sprachbot -- Allgemein (OpenAI Realtime)")
        botf.pack(fill="x", padx=8, pady=6)

        self.var_bot_key = tk.StringVar(value=os.environ.get("OPENAI_API_KEY", ""))
        self.var_bot_model = tk.StringVar(value="gpt-realtime")
        self.var_bot_dir = tk.StringVar(
            value=os.path.join(os.environ.get("APPDATA", "."), "GoliathIntercom", "Nachrichten"))

        self._labeled_entry(botf, "API-Key", self.var_bot_key, 0, show="*")
        trow2 = ttk.Frame(botf)
        trow2.grid(row=0, column=2, padx=4, pady=2, sticky="w")
        ttk.Button(trow2, text="Verbindung testen", command=self._on_test_openai_key).pack(side="left")
        self.lbl_bot_status = ttk.Label(trow2, text="Nicht geprueft")
        self.lbl_bot_status.pack(side="left", padx=8)
        ttk.Label(botf, text="Realtime-Modell:").grid(row=1, column=0, sticky="w", padx=4)
        self.cmb_bot_model = ttk.Combobox(botf, textvariable=self.var_bot_model, width=42,
                                          values=["gpt-realtime"])
        self.cmb_bot_model.grid(row=1, column=1, padx=4, pady=2)
        ttk.Button(botf, text="Modelle aktualisieren", command=self._on_refresh_models).grid(row=1, column=2, padx=4)
        self._labeled_entry(botf, "Nachrichten-Ordner", self.var_bot_dir, 2)
        brow = ttk.Frame(botf)
        brow.grid(row=3, column=0, columnspan=2, sticky="we", pady=4)
        ttk.Button(brow, text="Ordner oeffnen", command=self._on_open_messages_dir).pack(side="left", padx=4)
        self.lbl_model_status = ttk.Label(brow, text="Modelle noch nicht von API geladen", wraplength=440)
        self.lbl_model_status.pack(side="left", padx=4)

        bot_notebook = ttk.Notebook(tab_bot)
        bot_notebook.pack(fill="both", expand=True, padx=8, pady=6)
        tab_bot_door = ttk.Frame(bot_notebook)
        tab_bot_fritz = ttk.Frame(bot_notebook)
        bot_notebook.add(tab_bot_door, text="Bot: Tuerklingel")
        bot_notebook.add(tab_bot_fritz, text="Bot: Fritzbox (**620)")
        tab_vision = ttk.Frame(bot_notebook)
        bot_notebook.add(tab_vision, text="Kameraanalyse")
        self.var_vision_enabled = tk.BooleanVar(value=False)
        self.var_vision_save_images = tk.BooleanVar(value=False)
        self.var_vision_model = tk.StringVar(value="gpt-4.1-mini")
        self.var_vision_retention = tk.StringVar(value="7")
        ttk.Checkbutton(tab_vision, text="Klingelbild mit OpenAI analysieren", variable=self.var_vision_enabled).grid(row=0, column=0, columnspan=2, sticky="w", padx=4, pady=4)
        ttk.Label(tab_vision, text="Aktivierung uebertraegt ein Kamerabild an OpenAI.\nKeine Gesichtserkennung oder Identitaetsbestimmung.", wraplength=500).grid(row=1, column=0, columnspan=2, sticky="w", padx=4, pady=4)
        ttk.Checkbutton(tab_vision, text="Kamerabilder auch lokal speichern", variable=self.var_vision_save_images).grid(row=2, column=0, columnspan=2, sticky="w", padx=4, pady=4)
        ttk.Label(tab_vision, text="Bildanalyse-Modell:").grid(row=3, column=0, sticky="w", padx=4)
        self.cmb_vision_model = ttk.Combobox(tab_vision, textvariable=self.var_vision_model, width=42,
                                             values=["gpt-4.1-mini"])
        self.cmb_vision_model.grid(row=3, column=1, padx=4, pady=2)
        ttk.Button(tab_vision, text="Modelle aktualisieren", command=self._on_refresh_models).grid(row=7, column=0, padx=4, pady=4)
        ttk.Button(tab_vision, text="Bildmodell testen (API-Kosten)", command=lambda: self._on_test_model('vision')).grid(row=7, column=1, padx=4, pady=4)
        self.lbl_vision_test = ttk.Label(tab_vision, text="Test nutzt nur ein synthetisches Bild", wraplength=500)
        self.lbl_vision_test.grid(row=8, column=0, columnspan=2, sticky="w", padx=4)
        self._labeled_entry(tab_vision, "Bild/Protokoll loeschen nach Tagen (1-365)", self.var_vision_retention, 4)
        ttk.Button(tab_vision, text="Klingelprotokoll oeffnen", command=self._open_door_events).grid(row=5, column=0, sticky="w", padx=4, pady=4)
        self.lbl_vision_status = ttk.Label(tab_vision, text="Noch kein Klingelbild analysiert", wraplength=500)
        self.lbl_vision_status.grid(row=6, column=0, columnspan=2, sticky="w", padx=4, pady=4)

        door_prompt_default = (
            "Du bist der digitale Assistent an der Haustuer der Familie. "
            "Begruesse Anrufer freundlich und kurz, frage nach dem Anliegen. "
            "Du darfst KEINE Auskunft ueber Abwesenheit, Tagesablauf oder Sicherheit "
            "des Haushalts geben. Wenn niemand erreichbar ist, biete an, eine "
            "Nachricht aufzunehmen (Name, Anliegen, Rueckruf-Nummer).")
        (self.var_bot_voice, self.var_bot_silence, self.var_bot_maxdur,
         self.txt_bot_prompt, self.var_bot_volume) = self._build_bot_config_tab(tab_bot_door, door_prompt_default)
        self.var_bot_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab_bot_door, text="Bot beantwortet Tuer-Anrufe",
                         variable=self.var_bot_enabled, command=self._on_bot_toggled
                         ).grid(row=9, column=0, columnspan=2, sticky="w", padx=4, pady=6)
        self.var_transfer_enabled, self.var_transfer_target, self.var_transfer_timeout = self._build_transfer_fields(tab_bot_door)
        self.var_bot_answer_delay = tk.StringVar(value="10")
        self._labeled_entry(tab_bot_door, "Bot-Annahme nach (0-40 s)", self.var_bot_answer_delay, 10)

        fritz_prompt_default = (
            "Du bist der digitale Assistent der Familie, erreichbar ueber die interne "
            "Rufnummer **620. Anrufer hier sind Familienmitglieder, keine Fremden an der "
            "Tuer. Sei hilfsbereit, beantworte Fragen zu Nachrichten und kannst offener "
            "sprechen als am Haustuer-Kanal.")
        (self.var_bot_voice_fritz, self.var_bot_silence_fritz, self.var_bot_maxdur_fritz,
         self.txt_bot_prompt_fritz, self.var_bot_volume_fritz) = self._build_bot_config_tab(tab_bot_fritz, fritz_prompt_default)
        self.var_bot_fritz_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab_bot_fritz, text="Bot beantwortet Fritzbox-Anrufe (**620, statt Tuer-Weiterleitung)",
                         variable=self.var_bot_fritz_enabled
                         ).grid(row=9, column=0, columnspan=2, sticky="w", padx=4, pady=6)
        self.var_transfer_enabled_fritz, self.var_transfer_target_fritz, self.var_transfer_timeout_fritz = self._build_transfer_fields(tab_bot_fritz)

        self._build_connection_config()

        logf = ttk.LabelFrame(self.root, text="Protokoll")
        logf.pack(fill="x", expand=False, padx=8, pady=(4, 8))
        self.txt_log = tk.Text(logf, height=6, state="disabled")
        self.txt_log.pack(fill="both", expand=True, padx=4, pady=4)

    def _build_connection_config(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Konfiguration")
        canvas = tk.Canvas(tab, highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        for title, fields in [
            ("SIP-Tuerstation", [("IP / Host", self.var_server, None), ("SIP-Port", self.var_port, None),
             ("SIP-Domaene", self.var_domain, None), ("Benutzer / Nebenstelle", self.var_user, None), ("Passwort", self.var_pass, "*")]),
            ("Fritzbox", [("IP / Host", self.var_fritz_server, None), ("SIP-Benutzer", self.var_fritz_user, None),
             ("SIP-Passwort", self.var_fritz_pass, "*"), ("Zielnummer", self.var_fritz_target, None)]),
        ]:
            frame = ttk.LabelFrame(content, text=title)
            frame.pack(fill="x", padx=8, pady=6)
            for row, (label, variable, mask) in enumerate(fields):
                self._labeled_entry(frame, label, variable, row, show=mask)
        camera = ttk.LabelFrame(content, text="Kamera-Zugang (RTSP)")
        camera.pack(fill="x", padx=8, pady=6)
        self.camera_fields = {key: tk.StringVar(value=default) for key, default in
                              [("host", ""), ("port", "554"), ("user", ""), ("password", ""), ("channel", "1"), ("subtype", "1")]}
        for row, (key, label) in enumerate([( "host", "IP / Host"), ("port", "RTSP-Port"),
                       ("user", "Benutzer"), ("password", "Passwort"), ("channel", "Kanal"), ("subtype", "Substream")]):
            self._labeled_entry(camera, label, self.camera_fields[key], row, show="*" if key == "password" else None)
        ttk.Button(camera, text="Kamera-Zugang uebernehmen", command=self._apply_camera_fields).grid(row=6, column=0, columnspan=2, pady=6)
        ttk.Label(content, text="Speichern mit 'Einstellungen speichern'. SIP-Aenderungen erst nach Neuverbindung.\nZugangsdaten werden lokal im Klartext gespeichert. config.json niemals veroeffentlichen.", wraplength=510).pack(padx=8, pady=8)

    def _read_camera_fields(self):
        try:
            parsed = urllib.parse.urlsplit(self.var_rtsp.get())
            values = {"host": parsed.hostname or "", "port": str(parsed.port or 554),
                      "user": urllib.parse.unquote(parsed.username or ""), "password": urllib.parse.unquote(parsed.password or "")}
            query = urllib.parse.parse_qs(parsed.query)
            values.update({key: query.get(key, ["1"])[0] for key in ("channel", "subtype")})
            for key, value in values.items():
                self.camera_fields[key].set(value)
        except ValueError:
            self._log("Kamera-URL ungueltig: bitte im Konfigurationsreiter korrigieren.")

    def _apply_camera_fields(self):
        values = {key: variable.get().strip() if key != "password" else variable.get() for key, variable in self.camera_fields.items()}
        host = values["host"]
        if not host or any(character in host for character in '/@?# '):
            self._log("Kamera: bitte gueltige IP / Host angeben.")
            return
        try:
            port = int(values["port"])
            if not 1 <= port <= 65535 or not values["channel"].isdigit() or values["subtype"] not in ("0", "1"):
                raise ValueError()
        except ValueError:
            self._log("Kamera: Port 1-65535, numerischer Kanal und Substream 0 oder 1 erforderlich.")
            return
        if ':' in host:
            host = '[' + host.strip('[]') + ']'
        auth = ''
        if values['user']:
            auth = urllib.parse.quote(values['user'], safe='') + ':' + urllib.parse.quote(values['password'], safe='') + '@'
        self.var_rtsp.set(f"rtsp://{auth}{host}:{port}/cam/realmonitor?channel={values['channel']}&subtype={values['subtype']}")
        self._log("Kamera-Zugang uebernommen; Einstellungen speichern und Video neu starten.")

    def _labeled_entry(self, parent, label, var, row, show=None):
        ttk.Label(parent, text=label + ":").grid(row=row, column=0, sticky="w", padx=4, pady=2)
        ttk.Entry(parent, textvariable=var, width=45, show=show).grid(row=row, column=1, padx=4, pady=2)

    def _build_bot_config_tab(self, parent, default_prompt):
        voice_var = tk.StringVar(value="alloy")
        silence_var = tk.StringVar(value="20")
        maxdur_var = tk.StringVar(value="180")

        ttk.Label(parent, text="Stimme:").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        voice_dropdown = ttk.Combobox(parent, textvariable=voice_var, state="readonly", width=42, values=VOICES)
        voice_dropdown.grid(row=0, column=1, padx=4, pady=2)
        self.voice_dropdowns.append(voice_dropdown)
        source = 'door' if len(self.voice_dropdowns) == 1 else 'fritz'
        ttk.Button(parent, text="Modell/Stimme testen", command=lambda: self._on_test_model(source)).grid(row=11, column=0, columnspan=2, pady=4)

        ttk.Label(parent, text="Ansprache / Skills (System-Prompt):").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=4, pady=(6, 0))
        prompt_text = tk.Text(parent, height=7, width=60, wrap="word")
        prompt_text.grid(row=2, column=0, columnspan=2, padx=4, pady=2, sticky="we")
        prompt_text.insert("1.0", default_prompt)

        self._labeled_entry(parent, "Stille-Timeout (s)", silence_var, 3)
        self._labeled_entry(parent, "Max. Gespraechsdauer (s)", maxdur_var, 4)

        volume_var = tk.StringVar(value="100")
        ttk.Label(parent, text="Bot-Lautstaerke (%):").grid(row=5, column=0, sticky="w", padx=4, pady=2)
        ttk.Spinbox(parent, from_=0, to=400, increment=25, textvariable=volume_var,
                    width=10).grid(row=5, column=1, sticky="w", padx=4, pady=2)
        return voice_var, silence_var, maxdur_var, prompt_text, volume_var

    # -- Konfiguration speichern/laden ---------------------------------------
    def _build_transfer_fields(self, parent):
        enabled = tk.BooleanVar(value=False)
        target = tk.StringVar(value="**610")
        timeout = tk.StringVar(value="30")
        ttk.Checkbutton(parent, text="Weiterleitung auf Wunsch erlauben (via Fritzbox)", variable=enabled).grid(row=6, column=0, columnspan=2, sticky="w", padx=4)
        self._labeled_entry(parent, "Freigegebene Zielnummer", target, 7)
        self._labeled_entry(parent, "Klingelzeit (5-120 s)", timeout, 8)
        return enabled, target, timeout

    def _collect_config(self):
        return {
            "server": self.var_server.get(), "port": self.var_port.get(),
            "user": self.var_user.get(), "pass": self.var_pass.get(),
            "domain": self.var_domain.get(), "rtsp": self.var_rtsp.get(),
            "mic": self.cmb_in.get(), "speaker": self.cmb_out.get(),
            "target": self.var_target.get(),
            "fritz_server": self.var_fritz_server.get(), "fritz_user": self.var_fritz_user.get(),
            "fritz_pass": self.var_fritz_pass.get(), "fritz_target": self.var_fritz_target.get(),
            "auto_bridge": self.var_auto_bridge.get(),
            "bot_key": self.var_bot_key.get(), "bot_model": self.var_bot_model.get(),
            "bot_dir": self.var_bot_dir.get(),
            "bot_voice": self.var_bot_voice.get(), "bot_silence": self.var_bot_silence.get(),
            "bot_maxdur": self.var_bot_maxdur.get(),
            "bot_volume": self.var_bot_volume.get(),
            "bot_answer_delay": self.var_bot_answer_delay.get(),
            "vision_enabled": self.var_vision_enabled.get(),
            "vision_save_images": self.var_vision_save_images.get(),
            "vision_model": self.var_vision_model.get(),
            "vision_retention": self.var_vision_retention.get(),
            "bot_prompt": self.txt_bot_prompt.get("1.0", "end").strip(),
            "bot_enabled": self.var_bot_enabled.get(),
            "bot_voice_fritz": self.var_bot_voice_fritz.get(),
            "bot_silence_fritz": self.var_bot_silence_fritz.get(),
            "bot_maxdur_fritz": self.var_bot_maxdur_fritz.get(),
            "bot_volume_fritz": self.var_bot_volume_fritz.get(),
            "bot_prompt_fritz": self.txt_bot_prompt_fritz.get("1.0", "end").strip(),
            "bot_fritz_enabled": self.var_bot_fritz_enabled.get(),
            "transfer_enabled": self.var_transfer_enabled.get(),
            "transfer_target": self.var_transfer_target.get(),
            "transfer_timeout": self.var_transfer_timeout.get(),
            "transfer_enabled_fritz": self.var_transfer_enabled_fritz.get(),
            "transfer_target_fritz": self.var_transfer_target_fritz.get(),
            "transfer_timeout_fritz": self.var_transfer_timeout_fritz.get(),
        }

    def _apply_config(self, cfg):
        pairs = [
            (self.var_server, "server"), (self.var_port, "port"), (self.var_user, "user"),
            (self.var_pass, "pass"), (self.var_domain, "domain"), (self.var_rtsp, "rtsp"),
            (self.var_target, "target"),
            (self.var_fritz_server, "fritz_server"), (self.var_fritz_user, "fritz_user"),
            (self.var_fritz_pass, "fritz_pass"), (self.var_fritz_target, "fritz_target"),
            (self.var_bot_key, "bot_key"), (self.var_bot_model, "bot_model"),
            (self.var_bot_dir, "bot_dir"),
            (self.var_bot_voice, "bot_voice"), (self.var_bot_silence, "bot_silence"),
            (self.var_bot_maxdur, "bot_maxdur"),
            (self.var_bot_volume, "bot_volume"),
            (self.var_bot_answer_delay, "bot_answer_delay"),
            (self.var_vision_model, "vision_model"),
            (self.var_vision_retention, "vision_retention"),
            (self.var_bot_volume_fritz, "bot_volume_fritz"),
            (self.var_transfer_target, "transfer_target"),
            (self.var_transfer_timeout, "transfer_timeout"),
            (self.var_transfer_target_fritz, "transfer_target_fritz"),
            (self.var_transfer_timeout_fritz, "transfer_timeout_fritz"),
            (self.var_bot_voice_fritz, "bot_voice_fritz"), (self.var_bot_silence_fritz, "bot_silence_fritz"),
            (self.var_bot_maxdur_fritz, "bot_maxdur_fritz"),
        ]
        for var, key in pairs:
            if key in cfg:
                var.set(cfg[key])
        if "rtsp" in cfg:
            self._read_camera_fields()
        for var, key in [(self.var_auto_bridge, "auto_bridge"), (self.var_bot_enabled, "bot_enabled"),
                          (self.var_bot_fritz_enabled, "bot_fritz_enabled"),
                          (self.var_transfer_enabled, "transfer_enabled"),
                          (self.var_transfer_enabled_fritz, "transfer_enabled_fritz"),
                          (self.var_vision_enabled, "vision_enabled"),
                          (self.var_vision_save_images, "vision_save_images")]:
            if key in cfg:
                var.set(bool(cfg[key]))
        if "mic" in cfg and cfg["mic"] in [n for n, _ in self.devices_in]:
            self.cmb_in.set(cfg["mic"])
        if "speaker" in cfg and cfg["speaker"] in [n for n, _ in self.devices_out]:
            self.cmb_out.set(cfg["speaker"])
        if "bot_prompt" in cfg:
            self.txt_bot_prompt.delete("1.0", "end")
            self.txt_bot_prompt.insert("1.0", cfg["bot_prompt"])
        if "bot_prompt_fritz" in cfg:
            self.txt_bot_prompt_fritz.delete("1.0", "end")
            self.txt_bot_prompt_fritz.insert("1.0", cfg["bot_prompt_fritz"])

    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            bad_dir = cfg.get("bot_dir", "")
            if "_internal" in bad_dir or "_MEI" in bad_dir:
                # Alter Fehler: Standardpfad wurde frueher relativ zu __file__
                # berechnet, was in der gepackten .exe in den internen
                # PyInstaller-Ordner zeigte. Auf den korrekten Pfad zuruecksetzen.
                del cfg["bot_dir"]
                self._log(f"[BOT] Fehlerhaften gespeicherten Nachrichten-Pfad "
                          f"korrigiert (war: {bad_dir})")
            self._apply_config(cfg)
            self._log(f"Einstellungen geladen aus {CONFIG_PATH}")
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as e:
            self._log(f"Konnte Einstellungen nicht laden: {e}")

    def _save_config(self):
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self._collect_config(), f, indent=2, ensure_ascii=False)
            self._log(f"Einstellungen gespeichert unter {CONFIG_PATH}")
        except OSError as e:
            self._log(f"Konnte Einstellungen nicht speichern: {e}")

    # -- Button-Handler ----------------------------------------------------
    def _on_connect(self):
        if self.sip is not None:
            return
        self.audio.input_device = self.devices_in[self.cmb_in.current()][1]
        self.audio.output_device = self.devices_out[self.cmb_out.current()][1]
        try:
            self.sip = SipCallClient(
                server_ip=self.var_server.get().strip(),
                server_port=int(self.var_port.get().strip()),
                user=self.var_user.get().strip(),
                password=self.var_pass.get().strip(),
                domain=self.var_domain.get().strip(),
                local_port=5070, rtp_port=17800,
                events=self.events, audio_engine=self.audio, leg="door")
        except Exception as e:
            self._log(f"Verbindung fehlgeschlagen: {e}")
            self.sip = None
            return
        self.sip_thread = threading.Thread(target=self.sip.run, daemon=True)
        self.sip_thread.start()
        self.btn_connect.config(state="disabled")
        self.btn_call.config(state="normal")

    def _on_fritz_connect(self):
        if self.sip_fritz is not None:
            return
        try:
            self.sip_fritz = SipCallClient(
                server_ip=self.var_fritz_server.get().strip(),
                server_port=5060,
                user=self.var_fritz_user.get().strip(),
                password=self.var_fritz_pass.get().strip(),
                domain=self.var_fritz_server.get().strip(),
                local_port=5071, rtp_port=17801,
                events=self.events, audio_engine=self.audio, leg="fritz")
        except Exception as e:
            self._log(f"Fritzbox-Verbindung fehlgeschlagen: {e}")
            self.sip_fritz = None
            return
        self.sip_fritz_thread = threading.Thread(target=self.sip_fritz.run, daemon=True)
        self.sip_fritz_thread.start()
        self.btn_fritz_connect.config(state="disabled")
        self.btn_fritz_call.config(state="normal")

    def _on_fritz_call(self):
        if self.sip_fritz:
            self.sip_fritz.audio = self.audio
            self.sip_fritz.cmd_call(self.var_fritz_target.get().strip())

    def _on_fritz_hangup(self):
        if self.sip_fritz:
            self.sip_fritz.cmd_hangup()

    def _on_call(self):
        if self.sip:
            self.sip.cmd_call(self.var_target.get().strip())

    def _on_hangup(self):
        if self.sip:
            self.sip.cmd_hangup()

    def _on_answer(self):
        if self.sip:
            self.sip.cmd_answer()
        self.incoming_frame.pack_forget()

    def _on_reject(self):
        if self.sip:
            self.sip.cmd_reject()
        self.incoming_frame.pack_forget()

    def _on_mute_toggle(self):
        self.audio.muted = not self.audio.muted
        self.btn_mute.config(text="Stumm (an)" if self.audio.muted else "Stumm")

    def _on_tone_toggle(self):
        self.audio.test_tone = not self.audio.test_tone
        if self.audio.test_tone:
            self.audio.test_tone_pos = 0
        self.btn_tone.config(text="Testton (an)" if self.audio.test_tone else "Testton")

    def _on_video_toggle(self):
        if self.video.running:
            self.video.stop()
            self.btn_video.config(text="Bild anzeigen")
            self.video_label.config(image="", text="(kein Bild)")
        else:
            self.video.start(self.var_rtsp.get().strip())
            self.btn_video.config(text="Bild ausblenden")

    def _on_close(self):
        try:
            self.door_bot_pending = None
            self._finish_bot_transfer()
            self._save_config()
            self.video.stop()
            self.audio.stop()
            if self.sip:
                self.sip.cmd_stop()
            if self.sip_fritz:
                self.sip_fritz.cmd_stop()
            if self.admin_server:
                self.admin_server.stop()
        finally:
            self.root.after(300, self.root.destroy)

    def _on_auto_bridge_toggled(self):
        if self.var_auto_bridge.get():
            self.var_bot_enabled.set(False)

    def _on_bot_toggled(self):
        if self.var_bot_enabled.get():
            self.var_auto_bridge.set(False)

    def _on_open_messages_dir(self):
        path = self.var_bot_dir.get().strip()
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)
        except OSError as e:
            self._log(f"[BOT] Konnte Ordner nicht oeffnen: {e}")

    def _on_record_toggled(self):
        self.audio.record_enabled = self.var_record.get()

    def _on_open_recordings_dir(self):
        path = self.audio.record_dir
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)
        except OSError as e:
            self._log(f"[AUDIO] Konnte Aufnahmen-Ordner nicht oeffnen: {e}")

    def _on_open_crash_log(self):
        try:
            if not os.path.exists(CRASH_LOG_PATH):
                os.makedirs(os.path.dirname(CRASH_LOG_PATH), exist_ok=True)
                open(CRASH_LOG_PATH, "a", encoding="utf-8").close()
            os.startfile(CRASH_LOG_PATH)
        except OSError as e:
            self._log(f"[FEHLER] Konnte Absturz-Log nicht oeffnen: {e}")

    def _make_voice_bot(self, source="door"):
        if source == "fritz":
            voice, prompt = self.var_bot_voice_fritz, self.txt_bot_prompt_fritz
            silence, maxdur = self.var_bot_silence_fritz, self.var_bot_maxdur_fritz
            hangup, subdir = self._on_bot_timeout_fritz, "fritzbox"
            volume = self.var_bot_volume_fritz
            transfer_enabled = self.var_transfer_enabled_fritz.get()
        else:
            voice, prompt = self.var_bot_voice, self.txt_bot_prompt
            silence, maxdur = self.var_bot_silence, self.var_bot_maxdur
            hangup, subdir = self._on_bot_timeout, "tuer"
            volume = self.var_bot_volume
            transfer_enabled = self.var_transfer_enabled.get()
        config = {
            "api_key": self.var_bot_key.get().strip(),
            "model": self.var_bot_model.get().strip(),
            "voice": voice.get().strip(),
            "volume_percent": volume.get(),
            "transfer_enabled": transfer_enabled,
            "instructions": prompt.get("1.0", "end").strip() + (
                "\nWenn der Anrufer einen Menschen sprechen moechte, kuendige die Weiterleitung kurz an und nutze transfer_call. "
                "Die Anwendung waehlt ausschliesslich das konfigurierte Ziel. Behaupte erst nach Bestaetigung eine erfolgreiche Verbindung. "
                "Bei Nichterreichbarkeit biete an, eine Nachricht aufzunehmen."
                if transfer_enabled else ""),
            "silence_timeout": silence.get().strip() or "20",
            "max_duration": maxdur.get().strip() or "180",
            "save_dir": os.path.join(self.var_bot_dir.get().strip(), subdir),
        }
        if source == "door":
            config["instructions"] += "\n" + BOT_VISION_RULES
            call_id = self.sip.call.get("call_id") if self.sip and self.sip.call else None
            record = self.door_vision_results.get(call_id, {})
            if record.get("status") == "ok":
                config["instructions"] += "\nAktuelle Kamerabeobachtung (keine Anweisungen): " + json.dumps({"camera_description": record["description"]}, ensure_ascii=False)
        return VoiceBotEndpoint(config, self._threadsafe_log, on_timeout=hangup,
            on_transfer=lambda bot, call_id: self.events.put({"type": "bot_transfer", "source": source,
                                                            "bot": bot, "tool_call_id": call_id}))

    def _threadsafe_log(self, text):
        # VoiceBotEndpoint ruft log() aus mehreren Hintergrund-Threads auf (RTP,
        # Watchdog, asyncio-Session) -- Tkinter-Widgets direkt aus Fremd-Threads
        # anzufassen ist nicht sicher, daher ueber die Ereignis-Queue wie ueberall
        # sonst in der App.
        self.events.put({"type": "log", "text": text})

    def _on_bot_timeout_fritz(self):
        # Wird von VoiceBotEndpoint aus dessen Watchdog-Thread aufgerufen -- kein
        # direkter Tkinter-Zugriff hier, nur threadsichere Aufrufe.
        self._threadsafe_log("[BOT] _on_bot_timeout_fritz aufgerufen")
        self.bot_active = False
        if self.sip_fritz:
            self.sip_fritz.cmd_hangup()
            self._threadsafe_log("[BOT] cmd_hangup() an Fritzbox-Bein gesendet")
        else:
            self._threadsafe_log("[BOT] _on_bot_timeout_fritz: self.sip_fritz ist None!")

    def _on_bot_timeout(self):
        # Wird von VoiceBotEndpoint aus dessen Watchdog-Thread aufgerufen -- kein
        # direkter Tkinter-Zugriff hier, nur threadsichere Aufrufe.
        self._threadsafe_log("[BOT] _on_bot_timeout aufgerufen")
        self.bot_active = False
        if self.sip:
            self.sip.cmd_hangup()
            self._threadsafe_log("[BOT] cmd_hangup() an Tuer-Bein gesendet")
        else:
            self._threadsafe_log("[BOT] _on_bot_timeout: self.sip ist None!")

    def _on_refresh_models(self):
        if self.model_refresh_running:
            return
        key = self.var_bot_key.get().strip()
        self.model_refresh_running = True
        self.lbl_model_status.config(text="Lade Modelle ...")
        threading.Thread(target=self._refresh_models_worker, args=(key,), daemon=True).start()

    def _refresh_models_worker(self, key):
        try:
            catalog = fetch_catalog(key)
            self.events.put({'type': 'openai_catalog', 'catalog': catalog})
        except Exception as error:
            text = str(error) if isinstance(error, OptionsError) else 'Modellabruf fehlgeschlagen'
            self.events.put({'type': 'openai_catalog', 'error': text})

    def _set_model_catalog(self, catalog):
        self.model_catalog = catalog
        # Keep saved/manual IDs even if not listed or no longer accessible.
        for combo, variable, category in [(self.cmb_bot_model, self.var_bot_model, 'realtime_models'),
                                           (self.cmb_vision_model, self.var_vision_model, 'vision_models')]:
            combo.configure(values=sorted(set(catalog.get(category, []) + [variable.get()])))

    def _on_test_model(self, kind):
        settings = self._model_test_settings(kind)
        if kind == 'vision':
            self.lbl_vision_test.config(text='Pruefe synthetisches Testbild ...')
        else:
            self.lbl_bot_status.config(text='Pruefe Modell/Stimme ...')
        threading.Thread(target=self._model_test_worker, args=(kind, settings), daemon=True).start()

    def _model_test_settings(self, kind):
        return {'key': self.var_bot_key.get().strip(),
                'model': self.var_vision_model.get().strip() if kind == 'vision' else self.var_bot_model.get().strip(),
                'voice': self.var_bot_voice_fritz.get() if kind == 'fritz' else self.var_bot_voice.get()}

    def _model_test_worker(self, kind, settings):
        try:
            text = test_vision(settings['key'], settings['model']) if kind == 'vision' else test_realtime(settings['key'], settings['model'], settings['voice'])
            ok = True
        except Exception as error:
            text = str(error) if isinstance(error, OptionsError) else 'Modelltest fehlgeschlagen'
            ok = False
        self.events.put({'type': 'openai_model_test', 'kind': kind, 'ok': ok, 'text': text})

    def _on_test_openai_key(self):
        self._on_refresh_models()

    # -- Logging / Ereignisverarbeitung -------------------------------------
    def _log(self, text):
        stamped = f"[{time.strftime('%H:%M:%S')}.{int((time.time() % 1) * 1000):03d}] {text}"
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", stamped + "\n")
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")
        self.log_lines.append(stamped)
        if len(self.log_lines) > 500:
            del self.log_lines[: len(self.log_lines) - 500]

    def _poll(self):
        # try/finally garantiert, dass sich _poll() IMMER neu einplant -- sonst
        # friert bei einem einzelnen unerwarteten Fehler die komplette Anzeige
        # (Protokoll, Status, alles) fuer den Rest der Laufzeit lautlos ein.
        try:
            try:
                while True:
                    ev = self.events.get_nowait()
                    self._handle_event(ev)
            except queue.Empty:
                pass

            if self.video.running:
                frame = self.video.get_frame()
                if frame is not None:
                    photo = ImageTk.PhotoImage(frame)
                    self.video_label.configure(image=photo, text="")
                    self.video_label.image = photo
                err = self.video.pop_error()
                if err:
                    self._log(f"[VIDEO] {err}")

            if self.call_start is not None:
                elapsed = int(time.time() - self.call_start)
                self.lbl_call_state.config(text=f"Verbunden {elapsed // 60:02d}:{elapsed % 60:02d}")
            self._check_bot_transfer()
            self._check_door_bot_answer()
            if time.monotonic() >= self.vision_cleanup_at:
                self.vision_cleanup_at = time.monotonic() + 3600
                days = self.var_vision_retention.get()
                threading.Thread(target=self._cleanup_door_events, args=(days,), daemon=True).start()
        except Exception as e:
            log_crash(f"Fehler in _poll():\n{traceback.format_exc()}")
            try:
                self._log(f"[FEHLER] Interner Anzeige-Fehler (siehe crash.log): {e}")
            except Exception:
                pass
        finally:
            self.root.after(100, self._poll)

    def _open_door_events(self):
        self.door_vision.directory.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.door_vision.directory))

    def _cleanup_door_events(self, days):
        try:
            count = self.door_vision.cleanup(days)
            if count:
                self._threadsafe_log(f"[KAMERA] {count} alte Bild-/Protokolldateien gemaess Loeschfrist entfernt (nicht wiederherstellbar)")
        except OSError:
            self._threadsafe_log("[KAMERA] Loeschfrist konnte nicht angewendet werden")

    def _start_door_vision(self, ev):
        if not self.var_vision_enabled.get() or not re.search(r"sip:8001@", ev.get("caller", "")):
            return
        call_id = ev.get("call_id")
        if not call_id:
            return
        settings = {"rtsp": self.var_rtsp.get().strip(), "key": self.var_bot_key.get().strip(),
                    "model": self.var_vision_model.get().strip() or "gpt-4.1-mini",
                    "save_images": self.var_vision_save_images.get(), "retention_days": self.var_vision_retention.get()}
        self.lbl_vision_status.config(text="Klingelbild wird aufgenommen und analysiert ...")
        self._log("[KAMERA] Aktueller Haustuer-Schnappschuss und Bildanalyse gestartet")
        threading.Thread(target=self._door_vision_worker, args=(settings, call_id), daemon=True).start()

    def _door_vision_worker(self, settings, call_id):
        try:
            record = self.door_vision.process(settings, call_id)
        except Exception:
            record = {"call_id": call_id, "status": "error", "error": "Klingelbild konnte nicht verarbeitet werden"}
        self.events.put({"type": "door_vision_result", "record": record})

    def _handle_door_vision_result(self, record):
        if record.get("purged_files"):
            self._log(f"[KAMERA] {record['purged_files']} alte Bild-/Protokolldateien gemaess Loeschfrist entfernt (nicht wiederherstellbar)")
        call_id = record["call_id"]
        self.door_vision_results[call_id] = record
        while len(self.door_vision_results) > 30:
            self.door_vision_results.pop(next(iter(self.door_vision_results)))
        if record.get("status") == "ok":
            self.lbl_vision_status.config(text=record["description"])
            self._log("[KAMERA] " + record["description"])
            if self.sip and self.sip.call and self.sip.call.get("call_id") == call_id:
                self.sip.cmd_camera_context(call_id, record["description"])
        else:
            self.lbl_vision_status.config(text=record.get("error", "Bildanalyse nicht verfuegbar"))
            self._log("[KAMERA] " + record.get("error", "Bildanalyse nicht verfuegbar") + "; Bot bleibt bei neutraler Ansprache")
        if record.get("log_error"):
            self._log("[KAMERA] " + record["log_error"])

    def _schedule_door_bot_answer(self, ev):
        try:
            delay = float(self.var_bot_answer_delay.get())
            if not 0 <= delay <= 40:
                raise ValueError()
        except (ValueError, TypeError):
            delay = 10
            self._log("[BOT] Ungueltige Annahme-Wartezeit; verwende 10 Sekunden (erlaubt: 0-40)")
        client = self.sip
        call_id = ev.get("call_id")
        if not client or not call_id:
            return
        self.door_bot_pending = {"client": client, "call_id": call_id,
                                 "deadline": time.monotonic() + delay}
        self.btn_hangup.config(state="normal")
        self._log(f"[BOT] Tueranruf: Annahme nach {delay:g}s geplant; Innenstationen koennen zuerst annehmen")
        self._check_door_bot_answer()

    def _check_door_bot_answer(self):
        pending = self.door_bot_pending
        if not pending:
            return
        client = pending["client"]
        call = client.call
        if (client is not self.sip or not self.var_bot_enabled.get() or not call
                or call.get("call_id") != pending["call_id"] or call.get("state") != "ringing_in"):
            self.door_bot_pending = None
            self._log("[BOT] Geplante Tuer-Annahme verworfen: Anruf nicht mehr offen oder Bot deaktiviert")
            return
        remaining = pending["deadline"] - time.monotonic()
        if remaining > 0:
            self.lbl_call_state.config(text=f"Klingelt -- Bot uebernimmt in {int(remaining) + 1}s")
            return
        self.door_bot_pending = None
        self.bot_active = True
        client.cmd_answer(expected_call_id=pending["call_id"], audio_engine=self._make_voice_bot())
        self._log("[BOT] Wartezeit abgelaufen -- nehme den offenen Tueranruf an")

    def _start_bot_transfer(self, ev):
        bot, source = ev["bot"], ev["source"]
        origin = self.sip_fritz if source == "fritz" else self.sip
        if not origin or origin.audio is not bot or not bot.running:
            return
        if self.bot_transfer:
            bot.transfer_failed(ev["tool_call_id"], "Eine Weiterleitung laeuft bereits")
            return
        suffix = "_fritz" if source == "fritz" else ""
        enabled = getattr(self, "var_transfer_enabled" + suffix).get()
        target = getattr(self, "var_transfer_target" + suffix).get().strip()
        try:
            seconds = float(getattr(self, "var_transfer_timeout" + suffix).get())
            if not 5 <= seconds <= 120:
                raise ValueError()
        except (ValueError, TypeError):
            bot.transfer_failed(ev["tool_call_id"], "Ungueltige Klingelzeit; erlaubt sind 5 bis 120 Sekunden")
            return
        if not enabled or not re.fullmatch(r"[+*0-9#]{1,32}", target) or target in ("**620", "620"):
            bot.transfer_failed(ev["tool_call_id"], "Keine gueltige freigegebene Zielnummer (kein Selbstanruf auf **620)")
            return
        self.transfer_serial += 1
        self.bot_transfer = {"bot": bot, "source": source, "origin": origin,
            "tool_call_id": ev["tool_call_id"], "target": target, "seconds": seconds,
            "leg": f"transfer-{self.transfer_serial}", "state": "announcement",
            "deadline": time.monotonic() + 15, "peer": None, "endpoint": None}
        self._log(f"[TRANSFER] Gewuenschte Verbindung zu {target}; Ansage wird abgewartet")

    def _check_bot_transfer(self):
        t = self.bot_transfer
        if not t:
            return
        bot = t["bot"]
        if not bot.running or not t["origin"].call:
            self._finish_bot_transfer()
            return
        if t["state"] == "announcement":
            with bot.outbound_lock:
                ready = not bot.response_in_progress and len(bot.outbound_buf) < SAMPLES_PER_PACKET
            if not ready and time.monotonic() < t["deadline"]:
                return
            endpoint = None
            try:
                endpoint = TransferEndpoint()
                peer = SipCallClient(self.var_fritz_server.get().strip(), 5060,
                    self.var_fritz_user.get().strip(), self.var_fritz_pass.get(),
                    self.var_fritz_server.get().strip(), 0, endpoint.port, self.events, endpoint,
                    leg=t["leg"], register_on_run=False)
                peer.call_timeout = t["seconds"]
                t.update(peer=peer, endpoint=endpoint, state="ringing",
                         deadline=time.monotonic() + t["seconds"])
                threading.Thread(target=peer.run, daemon=True).start()
                peer.cmd_call(t["target"])
                self._log(f"[TRANSFER] Ziel {t['target']} klingelt, maximal {t['seconds']:.0f}s")
            except Exception as e:
                if endpoint:
                    endpoint.stop()
                self._transfer_failed(str(e))
        elif t["state"] == "ringing" and time.monotonic() >= t["deadline"]:
            self._transfer_failed("Niemand hat innerhalb der Klingelzeit angenommen")

    def _transfer_failed(self, reason):
        t = self.bot_transfer
        if not t:
            return
        self._log(f"[TRANSFER] Nicht durchgestellt: {reason}; Bot uebernimmt wieder")
        self._finish_bot_transfer()
        if t["bot"].running:
            t["bot"].transfer_failed(t["tool_call_id"], reason)

    def _finish_bot_transfer(self):
        t = self.bot_transfer
        self.bot_transfer = None
        if not t:
            return
        peer = t.get("peer")
        if peer:
            peer.cmd_hangup()
            # CANCEL/487/ACK noch abwickeln, bevor der eigene SIP-Socket endet.
            timer = threading.Timer(11, peer.cmd_stop)
            timer.daemon = True
            timer.start()
        if t.get("endpoint"):
            t["endpoint"].stop()

    def _handle_transfer_event(self, ev):
        t = self.bot_transfer
        if not t or ev.get("leg") != t["leg"]:
            return
        kind = ev.get("type")
        if kind == "incoming_call":
            t["peer"].cmd_reject()
        elif kind == "call_connected" and t["state"] == "ringing":
            t["state"] = "connected"
            t["bot"].connect_transfer(t["endpoint"].info)
        elif kind in ("call_failed", "call_ended", "stopped"):
            if t["state"] == "connected":
                t["origin"].cmd_hangup()
                self._finish_bot_transfer()
            else:
                self._transfer_failed(ev.get("reason", "Ziel nicht erreichbar"))

    def _trigger_auto_bridge(self, caller):
        # Erst die Fritzbox anrufen und ABWARTEN bis sie verbunden ist, bevor wir bei
        # der Tuer annehmen -- sonst hoert die Tuerseite Stille waehrend am anderen
        # Ende noch die Digest-Auth-Runde laeuft.
        if not self.sip_fritz:
            self._log("[BRIDGE] Automatische Weiterleitung nicht moeglich: nicht mit Fritzbox verbunden!")
            self.sip.cmd_reject()
            return
        self.auto_bridge_active = True
        self.auto_bridge_pending = True
        self.sip_fritz.audio = self.fritz_relay
        self.sip_fritz.cmd_call(self.var_fritz_target.get().strip())
        self._log(f"[BRIDGE] Rufe zuerst Fritzbox an, dann Tuer annehmen (Anrufer: {caller})")

    def _trigger_reverse_bridge(self, caller):
        # Symmetrisch: erst die Tuerstation anrufen und verbinden lassen, bevor wir
        # den Fritzfon-Anruf annehmen.
        if not self.sip:
            self._log("[BRIDGE] Ruecklaeufige Weiterleitung nicht moeglich: nicht mit Tuerstation verbunden!")
            self.sip_fritz.cmd_reject()
            return
        self.auto_bridge_active = True
        self.reverse_bridge_pending = True
        self.sip.audio = self.door_relay
        self.sip.cmd_call("8001")
        self._log(f"[BRIDGE] Rufe zuerst Tuerstation an, dann Fritzfon-Anruf annehmen ({caller})")

    def _end_auto_bridge(self):
        self.auto_bridge_pending = False
        self.reverse_bridge_pending = False
        if not self.auto_bridge_active:
            return
        self.auto_bridge_active = False
        if self.sip:
            self.sip.audio = self.audio
            self.sip.cmd_hangup()
        if self.sip_fritz:
            self.sip_fritz.audio = self.audio
            self.sip_fritz.cmd_hangup()

    def _handle_event(self, ev):
        kind = ev.get("type")
        leg = ev.get("leg", "door")
        if kind == "log":
            self._log(ev["text"])
            return
        if kind == "bot_transfer":
            self._start_bot_transfer(ev)
            return
        if kind == "door_vision_result":
            self._handle_door_vision_result(ev["record"])
            return
        if str(leg).startswith("transfer-"):
            self._handle_transfer_event(ev)
            return
        if kind in ("call_ended", "call_failed") and self.bot_transfer and self.bot_transfer["source"] == leg:
            self._finish_bot_transfer()
        if kind == 'ui_callback':
            ev['callback']()
            return
        if kind == 'openai_catalog':
            self.model_refresh_running = False
            if ev.get('error'):
                self.lbl_model_status.config(text=ev['error'] + '; bisherige Auswahl bleibt erhalten')
                self._log('[OPENAI] ' + ev['error'])
            else:
                self._set_model_catalog(ev['catalog'])
                count = len(ev['catalog']['all_models'])
                self.lbl_model_status.config(text=f'{count} Modelle geladen; Kandidaten separat testen')
                self.lbl_bot_status.config(text='API erreichbar, Modelle geladen', foreground='green')
                self._log(f'[OPENAI] {count} Modelle geladen (keine Kompatibilitaetsgarantie)')
            return
        if kind == 'openai_model_test':
            label = self.lbl_vision_test if ev['kind'] == 'vision' else self.lbl_bot_status
            label.config(text=ev['text'], foreground='green' if ev['ok'] else 'red')
            self._log('[OPENAI] ' + ev['text'])
            return
        if kind == "openai_test_result":
            ok = ev.get("ok")
            self.lbl_bot_status.config(text=ev.get("text", ""), foreground="green" if ok else "red")
            self._log(f"[BOT] Verbindungstest: {ev.get('text', '')}")
            return

        if leg == "fritz":
            if kind == "registered":
                self.lbl_fritz_status.config(text="Registriert", foreground="green")
            elif kind == "reg_failed":
                self.lbl_fritz_status.config(text=f"Fehler: {ev.get('reason')}", foreground="red")
            elif kind == "incoming_call":
                self.btn_fritz_hangup.config(state="normal")
                if self.var_bot_fritz_enabled.get():
                    self.lbl_fritz_call_state.config(text=f"Anruf von {ev.get('caller')} -- Bot antwortet ...")
                    self.bot_active = True
                    self.sip_fritz.audio = self._make_voice_bot(source="fritz")
                    self.sip_fritz.cmd_answer()
                else:
                    self.lbl_fritz_call_state.config(
                        text=f"Anruf von {ev.get('caller')} -- verbinde zur Tuerstation ...")
                    self._trigger_reverse_bridge(ev.get("caller"))
            elif kind == "calling":
                self.lbl_fritz_call_state.config(text=f"Rufe {ev.get('target')} an ...")
                self.btn_fritz_hangup.config(state="normal")
            elif kind == "ringing_out":
                self.lbl_fritz_call_state.config(text="Klingelt ...")
            elif kind == "call_connected":
                self.lbl_fritz_call_state.config(text="Verbunden")
                self.btn_fritz_hangup.config(state="normal")
                if self.auto_bridge_pending:
                    self.auto_bridge_pending = False
                    self.sip.audio = self.door_relay
                    self.sip.cmd_answer()
                    self._log("[BRIDGE] Fritzbox verbunden, nehme jetzt auch den Tueranruf an")
            elif kind == "call_failed":
                self.lbl_fritz_call_state.config(text=f"Fehlgeschlagen: {ev.get('reason', '')}")
                self.btn_fritz_hangup.config(state="disabled")
                if self.auto_bridge_pending and self.sip:
                    self.sip.cmd_reject()
                self._end_auto_bridge()
                self.bot_active = False
                self.sip_fritz.audio = self.audio
            elif kind == "call_ended":
                self.lbl_fritz_call_state.config(text=f"Beendet ({ev.get('reason', '')})")
                self.btn_fritz_hangup.config(state="disabled")
                self._end_auto_bridge()
                self.bot_active = False
                self.sip_fritz.audio = self.audio
            return

        # leg == "door"
        if kind == "registered":
            self.lbl_status.config(text="Registriert", foreground="green")
        elif kind == "reg_failed":
            self.lbl_status.config(text=f"Fehler: {ev.get('reason')}", foreground="red")
        elif kind == "incoming_call":
            self._start_door_vision(ev)
            if not self.video.running:
                self.video.start(self.var_rtsp.get().strip())
                self.btn_video.config(text="Bild ausblenden")
            if self.var_bot_enabled.get():
                self._schedule_door_bot_answer(ev)
            elif self.var_auto_bridge.get():
                self.lbl_call_state.config(text=f"Anruf von {ev.get('caller')} -- wird an Fritzbox weitergeleitet ...")
                self.btn_hangup.config(state="normal")
                self._trigger_auto_bridge(ev.get("caller"))
            else:
                self.lbl_caller.config(text=f"Anruf von {ev.get('caller')}")
                self.incoming_frame.pack(fill="x", padx=8, pady=6)
                self.lbl_call_state.config(text="Eingehender Anruf ...")
                self.btn_hangup.config(state="normal")
        elif kind == "calling":
            self.lbl_call_state.config(text=f"Rufe {ev.get('target')} an ...")
            self.btn_call.config(state="disabled")
            self.btn_hangup.config(state="normal")
        elif kind == "ringing_out":
            self.lbl_call_state.config(text="Klingelt ...")
        elif kind == "call_connected":
            self.incoming_frame.pack_forget()
            self.call_start = time.time()
            self.btn_hangup.config(state="normal")
            self.btn_call.config(state="disabled")
            if not self.auto_bridge_active and not self.bot_active:
                self.btn_mute.config(state="normal")
                self.btn_tone.config(state="normal")
            if self.reverse_bridge_pending:
                self.reverse_bridge_pending = False
                self.sip_fritz.audio = self.fritz_relay
                self.sip_fritz.cmd_answer()
                self._log("[BRIDGE] Tuerstation verbunden, nehme jetzt auch den Fritzfon-Anruf an")
        elif kind == "call_failed":
            self.lbl_call_state.config(text=f"Fehlgeschlagen: {ev.get('reason', '')}")
            self.btn_call.config(state="normal")
            self.btn_hangup.config(state="disabled")
            if self.reverse_bridge_pending and self.sip_fritz:
                self.sip_fritz.cmd_reject()
            self._end_auto_bridge()
            self.bot_active = False
            self.sip.audio = self.audio
        elif kind == "call_ended":
            self.incoming_frame.pack_forget()
            self.call_start = None
            self.lbl_call_state.config(text=f"Beendet ({ev.get('reason', '')})")
            self.btn_call.config(state="normal")
            self.btn_hangup.config(state="disabled")
            self.btn_mute.config(state="disabled", text="Stumm")
            self.btn_tone.config(state="disabled", text="Testton")
            self.bot_active = False
            self.sip.audio = self.audio
            self.audio.muted = False
            self.audio.test_tone = False
            self._end_auto_bridge()


def main():
    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    except Exception:
        log_crash("FATAL beim Start:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
