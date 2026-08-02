#!/usr/bin/env python3
"""FT-710 Tune Assist + POTA Spot Cluster (PC companion app).

Two interchangeable rig backends, selected in Settings:
  - FT-710 direct CAT: connects to a Yaesu FT-710 over a normal
    serial/CAT cable (the PC is a real USB host, so - unlike the
    USB-C-host Cardputer variant of this project - no external power
    injection is needed here; the radio just enumerates as a COM/tty
    port).
  - Hamlib rigctld: connects over TCP to a `rigctld` daemon (part of
    Hamlib, started separately by the user, e.g.
    `rigctld -m <model> -r COM5`). Since rigctld itself abstracts the
    CAT/CI-V/etc. protocol differences between radios, this backend
    works with any of the ~300 rigs Hamlib supports, not just the
    FT-710.

Both features below work with either backend:
  - POTA spot list (polled from api.pota.app): double-click a spot to
    QSY the radio to its frequency and mode.
  - Tune button (press and hold): moves TUNE_OFFSET_HZ off the current
    frequency, switches to CW at the configured tune power and keys a
    steady carrier; releasing un-keys and restores frequency, mode and
    power.

CAT reference (FT-710 direct backend): Yaesu "New CAT" command set,
verified against Hamlib's newcat backend (which lists the FT-710 as a
supported rig):
  FA%0*d;  set/query VFO-A frequency in Hz, zero padded to a width the
           radio itself reports back (auto-detected, 8 or 9 digits)
  PC%03d;  set RF power in watts, zero padded to 3 digits
  MD0%c;   set mode on the main VFO ('3' = CW)
  TX1; / TX0;  CAT PTT on/off

rigctld reference (Hamlib backend): extended-response protocol
('+'-prefixed commands, each reply terminated by an "RPRT <code>"
line), see the rigctld(8) man page. Power there is Hamlib's normalized
RFPOWER level (0.0-1.0 fraction of the rig's max power) - Hamlib has
no backend-independent absolute-watt setter, so this backend's tune
power is a level, not watts.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk

import requests
import serial
import serial.tools.list_ports

POTA_SPOTS_URL = "https://api.pota.app/spot/activator"
POTA_POLL_SECONDS_DEFAULT = 60
WORKED_TODAY_REFRESH_SECONDS = 60
QRZ_LOGBOOK_API_URL = "https://logbook.qrz.com/api"


def app_dir() -> Path:
    """Directory the script/exe lives in - config and logs live next to it."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = app_dir() / "pota_tune_assist_config.json"
LOG_DIR = app_dir() / "logs"
OUTDOOR_LIST_PATH = app_dir() / "draussenfunker.txt"
OUTDOOR_LIST_URL = "https://calls.draussenfunker.de/df-polo-notes.txt"

ADIF_HEADER = (
    "POTA Tune Assist ADIF Log\n"
    "<ADIF_VER:5>3.1.4\n"
    "<PROGRAMID:15>POTA-TuneAssist\n"
    "<EOH>\n"
)


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(config: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except OSError:
        pass


def parse_outdoor_calls(text: str) -> set[str]:
    """Parses a Ham2K PoLo callsign-notes-style watchlist: one entry per
    line, first whitespace-separated token is the callsign, everything
    after it (emoji, name, tags, ...) is ignored. '#' starts a comment,
    blank lines are skipped."""
    calls: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        call = line.split()[0].strip().upper()
        if call:
            calls.add(call)
    return calls


def load_outdoor_calls() -> set[str]:
    """Downloads the Draussenfunker watchlist fresh from OUTDOOR_LIST_URL
    on every start and caches the raw response in OUTDOOR_LIST_PATH, so a
    start without internet (e.g. out in a park) still has the last known
    list instead of an empty one."""
    try:
        resp = requests.get(OUTDOOR_LIST_URL, timeout=10)
        resp.raise_for_status()
        text = resp.text
    except (requests.RequestException, OSError):
        text = None

    if text is not None:
        try:
            with open(OUTDOOR_LIST_PATH, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
        return parse_outdoor_calls(text)

    try:
        with open(OUTDOOR_LIST_PATH, "r", encoding="utf-8") as f:
            return parse_outdoor_calls(f.read())
    except OSError:
        return set()


def _adif_field(name: str, value: str) -> str:
    return f"<{name}:{len(value)}>{value}"


def format_adif_record(fields: dict[str, str]) -> str:
    parts = [_adif_field(name, value) for name, value in fields.items() if value]
    parts.append("<EOR>")
    return " ".join(parts) + "\n"


def daily_adif_path(qso_date: str) -> Path:
    """One ADIF file per QSO day - a new day never gets appended to a
    previous day's file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"pota_tune_assist_log_{qso_date}.adi"


def append_adif_record(qso_date: str, record: str) -> Path:
    path = daily_adif_path(qso_date)
    is_new = not path.exists()
    with open(path, "a", encoding="utf-8") as f:
        if is_new:
            f.write(ADIF_HEADER)
        f.write(record)
    return path


_ADIF_TAG_RE = re.compile(r"<(\w+)(?::(\d+)(?::\w+)?)?>")


def parse_adif_records(text: str) -> list[dict[str, str]]:
    """Minimal reader for our own ADIF output: <FIELD:LEN>value ... <EOR>.
    Good enough to re-read today's log for dupe/new-band checking, not a
    general-purpose ADIF parser."""
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for m in _ADIF_TAG_RE.finditer(text):
        name = m.group(1).upper()
        if name == "EOR":
            if current:
                records.append(current)
            current = {}
            continue
        length_str = m.group(2)
        if length_str is None:
            continue
        length = int(length_str)
        current[name] = text[m.end():m.end() + length]
    return records


def load_worked_today() -> dict[str, list[dict[str, str]]]:
    """Call -> list of today's logged QSO records (BAND/MODE/SIG_INFO),
    keyed by uppercased callsign, for the new-band/mode/park check."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = daily_adif_path(today)
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    index: dict[str, list[dict[str, str]]] = {}
    for rec in parse_adif_records(text):
        call = rec.get("CALL", "").strip().upper()
        if call:
            index.setdefault(call, []).append(rec)
    return index


def worked_today_badge(spot: "Spot", worked_index: dict[str, list[dict[str, str]]]) -> str:
    """"" (nothing worked today), "DUPE" (exact band+mode+park already
    logged today), or "New <Band/Mode/Park> (mode freq park)" for a spot
    whose band, mode, or park hasn't been logged yet today for this call
    - only meaningful once the call has at least one QSO logged today."""
    call = (spot.activator or "").strip().upper()
    records = worked_index.get(call)
    if not records:
        return ""

    band = band_for_khz(spot.frequency_khz)
    mode = (spot.mode or "").strip().upper()
    park = spot.reference

    if any(
        r.get("BAND", "").strip().lower() == band
        and r.get("MODE", "").strip().upper() == mode
        and r.get("SIG_INFO", "").strip() == park
        for r in records
    ):
        return "DUPE"

    bands_worked = {r.get("BAND", "").strip().lower() for r in records}
    modes_worked = {r.get("MODE", "").strip().upper() for r in records}
    parks_worked = {r.get("SIG_INFO", "").strip() for r in records}

    new_dims = []
    if band not in bands_worked:
        new_dims.append("Band")
    if mode not in modes_worked:
        new_dims.append("Mode")
    if park not in parks_worked:
        new_dims.append("Park")
    if not new_dims:
        return ""
    return f"New {'/'.join(new_dims)} ({mode} {spot.frequency_khz:.1f} {park})"
    return path


def upload_to_qrz(api_key: str, adif_record: str) -> tuple[bool, str]:
    """POST a single ADIF record to the QRZ Logbook API. Returns
    (success, logid_or_reason)."""
    resp = requests.post(
        QRZ_LOGBOOK_API_URL,
        data={"KEY": api_key, "ACTION": "INSERT", "ADIF": adif_record},
        timeout=10,
    )
    resp.raise_for_status()
    parsed = dict(urllib.parse.parse_qsl(resp.text.strip()))
    if parsed.get("RESULT", "").upper() == "OK":
        return True, parsed.get("LOGID", "")
    return False, parsed.get("REASON", resp.text)

CAT_BAUD_DEFAULT = 38400
CAT_CMD_DELAY = 0.05
CAT_REPLY_TIMEOUT = 0.3

RIGCTLD_HOST_DEFAULT = "localhost"
RIGCTLD_PORT_DEFAULT = 4532
RIGCTLD_TIMEOUT = 1.0
RIGCTLD_LAUNCH_TIMEOUT = 5.0

TUNE_POWER_WATTS_DEFAULT = 5
RIGCTLD_TUNE_LEVEL_DEFAULT = 0.05
TUNE_OFFSET_HZ_DEFAULT = 5000
MAX_TUNE_SECONDS = 10

CW_MODE_NAME = "CW"

# TUNE moves off-frequency by roughly one channel width of whatever mode
# was active before tuning, so the test tone lands right next to the
# operating frequency instead of an arbitrary fixed distance away.
# Approximate standard filter/occupied bandwidths per mode, in Hz.
MODE_BANDWIDTH_HZ: dict[str, int] = {
    "CW": 500, "CWR": 500,
    "LSB": 2400, "USB": 2400,
    "RTTY": 500, "RTTYR": 500,
    "PKTLSB": 2400, "PKTUSB": 2400,
    "AM": 6000,
    "FM": 12500, "PKTFM": 12500,
    "C4FM": 12500,
}

BACKEND_DISPLAY_TO_KEY = {
    "FT-710 (CAT direkt)": "ft710",
    "Hamlib rigctld (Netzwerk)": "rigctld",
}
BACKEND_KEY_TO_DISPLAY = {v: k for k, v in BACKEND_DISPLAY_TO_KEY.items()}

# FT-710 "New CAT" MD mode codes <-> Hamlib-style canonical mode names.
# Canonical names are what both backends expose via get_mode()/set_mode(),
# so QSY/tune logic never needs to know which backend is active.
CHAR_TO_CANONICAL = {
    "1": "LSB", "2": "USB", "3": "CW", "4": "FM", "5": "AM",
    "6": "RTTYR", "7": "CWR", "8": "PKTLSB", "9": "RTTY",
    "A": "PKTFM", "B": "FM", "C": "PKTUSB", "D": "AM",
    "E": "C4FM", "F": "PKTFM",
}
CANONICAL_TO_CHAR = {
    "LSB": "1", "USB": "2", "CW": "3", "FM": "4", "AM": "5",
    "RTTYR": "6", "CWR": "7", "PKTLSB": "8", "RTTY": "9",
    "PKTFM": "A", "PKTUSB": "C", "C4FM": "E",
}

BAND_RANGES_KHZ = [
    (1800, 2000, "160m"), (3500, 4000, "80m"), (5330, 5410, "60m"),
    (7000, 7300, "40m"), (10100, 10150, "30m"), (14000, 14350, "20m"),
    (18068, 18168, "17m"), (21000, 21450, "15m"), (24890, 24990, "12m"),
    (28000, 29700, "10m"), (50000, 54000, "6m"),
]


def band_for_khz(khz: float) -> str:
    for lo, hi, name in BAND_RANGES_KHZ:
        if lo <= khz <= hi:
            return name
    return "?"


def resolve_mode_name(pota_mode: str, freq_hz: int) -> str:
    m = (pota_mode or "").strip().upper()
    if m == "USB":
        return "USB"
    if m == "LSB":
        return "LSB"
    if m in ("SSB", "PHONE"):
        return "LSB" if freq_hz < 10_000_000 else "USB"
    if m == "CW":
        return "CW"
    if m == "AM":
        return "AM"
    if m == "FM":
        return "FM"
    if m == "RTTY":
        return "RTTY"
    # FT8, FT4, JS8, PSK, MFSK, and anything else unrecognized: DATA-USB
    return "PKTUSB"


class CatError(Exception):
    pass


class Ft710Cat:
    def __init__(self):
        self._ser: serial.Serial | None = None
        self.freq_width = 8
        self.trace: list[str] = []

    @property
    def connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def connect(self, port: str, baud: int) -> None:
        self._ser = serial.Serial(port, baudrate=baud, timeout=0.05)
        time.sleep(0.2)
        self.freq_width = self._detect_freq_width()

    def disconnect(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except serial.SerialException:
                pass
        self._ser = None

    def _trace_add(self, entry: str) -> None:
        self.trace.append(entry)
        del self.trace[:-60]

    def _write(self, text: str) -> None:
        assert self._ser is not None
        self._ser.write(text.encode("ascii"))
        self._trace_add(f"TX {text!r}")
        time.sleep(CAT_CMD_DELAY)

    def _transact(self, cmd: str, timeout: float = CAT_REPLY_TIMEOUT) -> str:
        if not self.connected:
            raise CatError("Funkgerät nicht verbunden")
        assert self._ser is not None
        stray = self._ser.in_waiting
        self._ser.reset_input_buffer()
        self._ser.write(cmd.encode("ascii"))
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self._ser.read(64)
            if chunk:
                buf.extend(chunk)
                if buf.endswith(b";"):
                    reply = buf.decode("ascii", errors="replace")
                    stray_note = f" [{stray} Byte vor der Anfrage verworfen]" if stray else ""
                    self._trace_add(f"TX {cmd!r} -> RX {reply!r}{stray_note}")
                    return reply
        self._trace_add(f"TX {cmd!r} -> TIMEOUT (bisher empfangen: {bytes(buf)!r})")
        raise CatError(f"Zeitüberschreitung bei Antwort auf {cmd!r}")

    def _detect_freq_width(self) -> int:
        reply = self._transact("FA;")
        digits = reply[2:-1]
        if not digits.isdigit():
            raise CatError(f"Unerwartete FA-Antwort: {reply!r}")
        return len(digits)

    def get_freq_hz(self) -> int:
        reply = self._transact("FA;")
        digits = reply[2:-1]
        if not digits.isdigit():
            raise CatError(f"Unerwartete FA-Antwort: {reply!r}")
        return int(digits)

    def set_freq_hz(self, hz: int) -> None:
        self._write(f"FA{hz:0{self.freq_width}d};")

    def get_mode(self) -> str:
        reply = self._transact("MD0;")
        if not reply.startswith("MD0") or len(reply) < 5:
            raise CatError(f"Unerwartete MD-Antwort: {reply!r}")
        char = reply[3]
        return CHAR_TO_CANONICAL.get(char, char)

    def set_mode(self, mode_name: str) -> None:
        char = CANONICAL_TO_CHAR.get(mode_name.upper())
        if char is None:
            raise CatError(f"Mode {mode_name!r} nicht auf FT-710-CAT-Code abbildbar")
        self._write(f"MD0{char};")

    def get_power(self) -> int:
        reply = self._transact("PC;")
        if not reply.startswith("PC") or len(reply) < 6:
            raise CatError(f"Unerwartete PC-Antwort: {reply!r}")
        return int(reply[2:5])

    def set_power(self, watts: float) -> None:
        self._write(f"PC{int(round(watts)):03d};")

    def key_down(self) -> None:
        self._write("TX1;")

    def key_up(self) -> None:
        self._write("TX0;")


class RigctldClient:
    """Talks to Hamlib's rigctld daemon over TCP using its extended-
    response protocol ('+'-prefixed commands, each reply terminated by
    an "RPRT <code>" line). Since rigctld itself translates generic
    commands into whatever its currently loaded rig backend needs, this
    class works unmodified for any Hamlib-supported radio - the user
    just points rigctld at the right -m <model> and -r <port>.

    Power here is Hamlib's normalized RFPOWER level (0.0-1.0), not
    absolute watts - Hamlib has no backend-independent absolute-watt
    setter.
    """

    def __init__(self):
        self._sock: socket.socket | None = None
        self._buf: bytes = b""

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self, host: str, port: int) -> None:
        self._sock = socket.create_connection((host, port), timeout=RIGCTLD_TIMEOUT)
        self._sock.settimeout(RIGCTLD_TIMEOUT)
        self._buf = b""

    def disconnect(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buf = b""

    def _readline(self) -> str:
        assert self._sock is not None
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise CatError("rigctld hat die Verbindung geschlossen")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line.decode("ascii", errors="replace").strip()

    def _transact(self, cmd: str) -> list[str]:
        if not self.connected:
            raise CatError("rigctld nicht verbunden")
        assert self._sock is not None
        try:
            self._sock.sendall(f"+{cmd}\n".encode("ascii"))
            lines: list[str] = []
            while True:
                line = self._readline()
                lines.append(line)
                if line.startswith("RPRT "):
                    break
        except (OSError, socket.timeout) as exc:
            raise CatError(f"rigctld-Fehler bei {cmd!r}: {exc}") from exc
        rprt = int(lines[-1].split()[1])
        if rprt != 0:
            raise CatError(f"rigctld meldet Fehler {rprt} bei {cmd!r}")
        return lines

    def get_freq_hz(self) -> int:
        for line in self._transact("f"):
            if line.startswith("Frequency:"):
                return int(line.split(":", 1)[1].strip())
        raise CatError("Keine Frequenz in rigctld-Antwort")

    def set_freq_hz(self, hz: int) -> None:
        self._transact(f"F {hz}")

    def get_mode(self) -> str:
        for line in self._transact("m"):
            if line.startswith("Mode:"):
                return line.split(":", 1)[1].strip()
        raise CatError("Kein Mode in rigctld-Antwort")

    def set_mode(self, mode_name: str) -> None:
        self._transact(f"M {mode_name.upper()} 0")

    def get_power(self) -> float:
        for line in self._transact("l RFPOWER"):
            if line.startswith("Level Value:"):
                return float(line.split(":", 1)[1].strip())
        raise CatError("Kein Level in rigctld-Antwort")

    def set_power(self, level: float) -> None:
        self._transact(f"L RFPOWER {level}")

    def key_down(self) -> None:
        self._transact("T 1")

    def key_up(self) -> None:
        self._transact("T 0")


def find_rigctld_executable() -> str | None:
    """Locates rigctld without requiring the user to add Hamlib to PATH
    themselves - checks PATH first, then the default Windows install
    location (Hamlib's own installer doesn't add itself to PATH)."""
    exe = shutil.which("rigctld")
    if exe:
        return exe
    if sys.platform == "win32":
        for envvar in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            base = os.environ.get(envvar)
            if not base:
                continue
            candidate = Path(base) / "Hamlib" / "bin" / "rigctld.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def parse_rig_list(text: str) -> list[tuple[int, str, str]]:
    """Parses `rigctld -l` output (a "Rig# Mfg Model Vers. Status" table,
    columns separated by 2+ spaces) into (model_id, mfg, model) tuples."""
    models: list[tuple[int, str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not line[0].isdigit():
            continue
        parts = re.split(r"\s{2,}", line)
        if len(parts) < 3:
            continue
        try:
            model_id = int(parts[0])
        except ValueError:
            continue
        models.append((model_id, parts[1].strip(), parts[2].strip()))
    return models


def list_rig_models(rigctld_exe: str) -> list[tuple[int, str, str]]:
    result = subprocess.run(
        [rigctld_exe, "-l"], capture_output=True, text=True, timeout=10, check=False,
    )
    return parse_rig_list(result.stdout)


class RigctldProcess:
    """Launches and owns a local rigctld subprocess for a chosen rig
    model + serial port, so the user never has to open a terminal or type
    a rigctld command line themselves."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, rigctld_exe: str, model_id: int, serial_port: str, baud: int,
              listen_host: str, listen_port: int) -> None:
        if self.running:
            self.stop()
        args = [
            rigctld_exe, "-m", str(model_id), "-r", serial_port,
            "-s", str(baud), "-t", str(listen_port),
        ]
        popen_kwargs: dict = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self._proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **popen_kwargs,
            )
        except OSError as exc:
            raise CatError(f"rigctld konnte nicht gestartet werden: {exc}") from exc

        deadline = time.monotonic() + RIGCTLD_LAUNCH_TIMEOUT
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                out = self._proc.stdout.read().decode(errors="replace") if self._proc.stdout else ""
                self._proc = None
                raise CatError(f"rigctld hat sich sofort beendet: {out.strip()[-300:] or 'unbekannter Fehler'}")
            try:
                with socket.create_connection((listen_host, listen_port), timeout=0.3):
                    return
            except OSError:
                time.sleep(0.15)
        self.stop()
        raise CatError(f"rigctld ist nicht innerhalb von {RIGCTLD_LAUNCH_TIMEOUT:g}s bereit geworden (Timeout).")

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None


def set_verified(get_fn, set_fn, target, attempts: int = 3, retry_delay: float = 0.15,
                  resend_delay: float = 0.05, tolerance: float = 0) -> bool:
    """Write-then-verify: a single blind CAT write is occasionally dropped
    or short-written by the radio (especially right after a preceding
    command, e.g. FA; sent just after TX0;), landing a few hundred Hz off
    until the same command is sent again by hand. Sending the command
    twice back-to-back before every read-back (field-confirmed fix - a
    lone write is the unreliable case, a repeated one lands correctly)
    plus retrying the whole pair on mismatch, instead of trusting one
    write, fixes that without the user needing to notice and retry.
    Returns whether the target was confirmed."""
    for _ in range(attempts):
        set_fn(target)
        time.sleep(resend_delay)
        set_fn(target)
        time.sleep(retry_delay)
        current = get_fn()
        if isinstance(target, (int, float)) and not isinstance(target, bool):
            if abs(current - target) <= tolerance:
                return True
        elif current == target:
            return True
    return False


class TuneController:
    """Mirrors the Cardputer firmware's tune logic: read back frequency,
    mode and power before transmitting, restore all three afterwards -
    never assume, always verify first. Works against either backend
    (Ft710Cat or RigctldClient) since both expose the same
    get/set_freq_hz, get/set_mode and get/set_power methods."""

    def __init__(self, cat):
        self.cat = cat
        self.active = False
        self.start_time = 0.0
        self.saved_freq: int | None = None
        self.saved_mode: str | None = None
        self.saved_power: float | None = None
        self.last_offset_hz = 0

    def start(self, tune_power: float, offset_sign: int, fallback_offset_hz: int) -> None:
        if self.active:
            return
        if not self.cat.connected:
            raise CatError("Funkgerät nicht verbunden")

        freq = self.cat.get_freq_hz()
        mode = self.cat.get_mode()
        power = self.cat.get_power()

        self.saved_freq = freq
        self.saved_mode = mode
        self.saved_power = power

        bandwidth = MODE_BANDWIDTH_HZ.get(mode.upper(), abs(fallback_offset_hz))
        offset_hz = (1 if offset_sign >= 0 else -1) * bandwidth
        self.last_offset_hz = offset_hz

        # Mode before frequency: on this radio, switching mode (e.g. into
        # CW) shifts the actual VFO frequency by the CW pitch/offset - if
        # frequency were set first, that later mode switch silently drags
        # it off-target again. Setting frequency last guarantees nothing
        # after it can move it.
        set_verified(self.cat.get_mode, self.cat.set_mode, CW_MODE_NAME)
        set_verified(self.cat.get_freq_hz, self.cat.set_freq_hz, freq + offset_hz)
        self.cat.set_power(tune_power)
        self.cat.key_down()

        self.active = True
        self.start_time = time.monotonic()

    def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        self.cat.key_up()
        if self.saved_mode is not None:
            set_verified(self.cat.get_mode, self.cat.set_mode, self.saved_mode)
        if self.saved_freq is not None:
            set_verified(self.cat.get_freq_hz, self.cat.set_freq_hz, self.saved_freq)
        if self.saved_power is not None:
            self.cat.set_power(self.saved_power)

    def elapsed(self) -> float:
        return time.monotonic() - self.start_time if self.active else 0.0


@dataclass
class Spot:
    spot_id: int
    activator: str
    frequency_khz: float
    mode: str
    reference: str
    park_name: str
    spot_time: str
    spotter: str
    comments: str
    location_desc: str
    invalid: bool

    @property
    def freq_hz(self) -> int:
        return int(round(self.frequency_khz * 1000))


def fetch_pota_spots() -> list[Spot]:
    resp = requests.get(POTA_SPOTS_URL, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    spots: list[Spot] = []
    for item in data:
        try:
            freq_khz = float(item.get("frequency", ""))
        except (TypeError, ValueError):
            continue
        spots.append(Spot(
            spot_id=item.get("spotId", 0),
            activator=item.get("activator", ""),
            frequency_khz=freq_khz,
            mode=(item.get("mode") or "").upper(),
            reference=item.get("reference", ""),
            park_name=item.get("name") or item.get("parkName", ""),
            spot_time=item.get("spotTime", ""),
            spotter=item.get("spotter", ""),
            comments=item.get("comments", ""),
            location_desc=item.get("locationDesc", ""),
            invalid=bool(item.get("invalid")),
        ))
    spots.sort(key=lambda s: s.frequency_khz)
    return spots


def format_spot_time(iso_time: str) -> str:
    try:
        dt = datetime.strptime(iso_time, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.strftime("%H:%M")
    except ValueError:
        return iso_time


def format_age(iso_time: str) -> str:
    try:
        dt = datetime.strptime(iso_time, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return "?"
    seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def strike(text: str) -> str:
    """Fake strikethrough for ttk.Treeview cells (no rich text support)."""
    return "̶".join(text) + "̶" if text else text


MODE_CATEGORY = {
    "CW": "cw",
    "SSB": "ssb", "USB": "ssb", "LSB": "ssb", "PHONE": "ssb",
    "AM": "ssb", "FM": "ssb",
    "FT8": "digital", "FT4": "digital", "JS8": "digital",
    "PSK": "digital", "PSK31": "digital", "RTTY": "digital",
    "MFSK": "digital", "DATA": "digital",
}


def mode_category(mode: str) -> str:
    return MODE_CATEGORY.get((mode or "").upper(), "digital")


BAND_FILTER_OPTIONS = ["Alle Bänder"] + [b[2] for b in BAND_RANGES_KHZ]
MODE_FILTER_OPTIONS = ["Alle Modes", "CW", "SSB", "Digital"]
COUNTRY_FILTER_ALL = "Alle Länder"


def country_for_location(location_desc: str) -> str:
    """POTA's locationDesc is "<entity-prefix>-<subdivision>" (e.g. "US-CA"),
    or just the prefix for countries without one. The prefix alone is the
    DXCC-style code hunters already recognize, so that's what we filter by."""
    if not location_desc:
        return "?"
    return location_desc.split("-", 1)[0]


# ISO 3166-1 alpha-2 country code -> continent, for the country picker's
# continent quick-select buttons. Best-effort/not exhaustive - a code POTA
# reports that isn't in here just won't be affected by a continent button,
# it stays individually selectable in the checklist either way.
ISO2_TO_CONTINENT: dict[str, str] = {
    **dict.fromkeys([
        "AD", "AL", "AT", "AX", "BA", "BE", "BG", "BY", "CH", "CY", "CZ", "DE",
        "DK", "EE", "ES", "FI", "FO", "FR", "GB", "GG", "GI", "GR", "HR", "HU",
        "IE", "IM", "IS", "IT", "JE", "LI", "LT", "LU", "LV", "MC", "MD", "ME",
        "MK", "MT", "NL", "NO", "PL", "PT", "RO", "RS", "RU", "SE", "SI", "SJ",
        "SK", "SM", "UA", "VA", "XK",
    ], "EU"),
    **dict.fromkeys([
        "AG", "AI", "AW", "BB", "BL", "BM", "BQ", "BS", "BZ", "CA", "CR", "CU",
        "CW", "DM", "DO", "GD", "GL", "GP", "GT", "HN", "HT", "JM", "KN", "KY",
        "LC", "MF", "MQ", "MS", "MX", "NI", "PA", "PM", "PR", "SV", "SX", "TC",
        "TT", "US", "VC", "VG", "VI",
    ], "NA"),
    **dict.fromkeys([
        "AR", "BO", "BR", "CL", "CO", "EC", "FK", "GF", "GY", "PE", "PY", "SR",
        "UY", "VE",
    ], "SA"),
    **dict.fromkeys([
        "AE", "AF", "AM", "AZ", "BD", "BH", "BN", "BT", "CC", "CN", "CX", "GE",
        "HK", "ID", "IL", "IN", "IO", "IQ", "IR", "JO", "JP", "KG", "KH", "KP",
        "KR", "KW", "KZ", "LA", "LB", "LK", "MM", "MN", "MO", "MV", "MY", "NP",
        "OM", "PH", "PK", "PS", "QA", "SA", "SG", "SY", "TH", "TJ", "TL", "TM",
        "TR", "TW", "UZ", "VN", "YE",
    ], "AS"),
    **dict.fromkeys([
        "AO", "BF", "BI", "BJ", "BW", "CD", "CF", "CG", "CI", "CM", "CV", "DJ",
        "DZ", "EG", "EH", "ER", "ET", "GA", "GH", "GM", "GN", "GQ", "GW", "KE",
        "KM", "LR", "LS", "LY", "MA", "MG", "ML", "MR", "MU", "MW", "MZ", "NA",
        "NE", "NG", "RE", "RW", "SC", "SD", "SH", "SL", "SN", "SO", "SS", "ST",
        "SZ", "TD", "TG", "TN", "TZ", "UG", "YT", "ZA", "ZM", "ZW",
    ], "AF"),
    **dict.fromkeys([
        "AS", "AU", "CK", "FJ", "FM", "GU", "KI", "MH", "MP", "NC", "NF", "NR",
        "NU", "NZ", "PF", "PG", "PW", "SB", "TO", "TV", "VU", "WF", "WS",
    ], "OC"),
}

CONTINENT_LABELS = ["EU", "NA", "SA", "AS", "AF", "OC"]

# -- dark military/olive theme palette ---------------------------------
COL_BG = "#0c130c"
COL_PANEL = "#182619"
COL_PANEL_ALT = "#131f14"
COL_ROW_EVEN = "#141f14"
COL_ROW_ODD = "#1a2a19"
COL_BORDER = "#33502c"
COL_ACCENT = "#7fae42"
COL_ACCENT_DIM = "#5c8032"
COL_TEXT = "#e8ecdd"
COL_MUTED = "#8a9a7c"
COL_GREEN = "#4caf50"
COL_RED = "#a1372c"
COL_AMBER = "#b58c2b"
COL_FAVORITE_BG = "#4a3a0d"
COL_OUTDOOR_BG = "#0d3a4a"


def _chip_button(parent, text, command=None, **kw):
    return tk.Button(
        parent, text=text, command=command,
        bg=COL_PANEL_ALT, fg=COL_ACCENT, activebackground=COL_BORDER,
        activeforeground=COL_ACCENT, relief="flat", bd=0,
        padx=12, pady=5, font=("Segoe UI", 9, "bold"),
        cursor="hand2", **kw,
    )


def _badge(parent, textvariable=None, text=None, fg=COL_TEXT, bg=COL_PANEL_ALT):
    return tk.Label(
        parent, textvariable=textvariable, text=text, fg=fg, bg=bg,
        padx=10, pady=4, font=("Segoe UI", 9, "bold"),
    )


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("POTA Tune Assist")
        self.geometry("1320x680")
        self.minsize(1180, 560)
        self.configure(bg=COL_BG)

        self._init_style()

        self.cat = Ft710Cat()
        self.tune = TuneController(self.cat)
        self.spots: list[Spot] = []
        self.skipped_ids: set[int] = set()
        self.logged_spot_ids: set[int] = set()
        self.worked_today_index: dict[str, list[dict[str, str]]] = {}
        self.auto_refresh = True
        self.spot_result_queue: queue.Queue = queue.Queue()
        self.log_result_queue: queue.Queue = queue.Queue()
        self.outdoor_result_queue: queue.Queue = queue.Queue()
        self.stop_poll_event = threading.Event()

        self.backend_var = tk.StringVar(value="ft710")
        self.backend_display_var = tk.StringVar(value=BACKEND_KEY_TO_DISPLAY["ft710"])

        config = load_config()
        self.my_callsign_var = tk.StringVar(value=config.get("my_callsign", ""))
        self.my_grid_var = tk.StringVar(value=config.get("my_gridsquare", ""))
        self.qrz_api_key_var = tk.StringVar(value=config.get("qrz_api_key", ""))
        self.favorite_calls: set[str] = set(config.get("favorite_calls", []))
        self.outdoor_calls: set[str] = set()

        self.port_var = tk.StringVar()
        self.baud_var = tk.IntVar(value=CAT_BAUD_DEFAULT)
        self.host_var = tk.StringVar(value=RIGCTLD_HOST_DEFAULT)
        self.rig_port_var = tk.IntVar(value=RIGCTLD_PORT_DEFAULT)
        self.rigctld_process = RigctldProcess()
        self.rig_models: list[tuple[int, str, str]] = []
        self.rig_model_displays: list[str] = []
        self.rig_model_display_var = tk.StringVar(value=config.get("rig_model_display", ""))
        self.tune_power_var = tk.DoubleVar(value=float(TUNE_POWER_WATTS_DEFAULT))
        self.power_unit_var = tk.StringVar(value="Leistung (W)")
        self.offset_var = tk.IntVar(value=TUNE_OFFSET_HZ_DEFAULT)
        self.offset_sign_var = tk.StringVar(value="above")
        self.band_filter_var = tk.StringVar(value=BAND_FILTER_OPTIONS[0])
        self.mode_filter_var = tk.StringVar(value=MODE_FILTER_OPTIONS[0])
        self.selected_countries: set[str] = set(config.get("hunt_countries", []))
        self.known_countries: set[str] = set(self.selected_countries)
        self.country_filter_label_var = tk.StringVar(value=self._country_filter_label())
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._render_spots())
        self.clock_var = tk.StringVar(value="--:--:--z")
        self.cat_badge_var = tk.StringVar(value="CAT")
        self.count_badge_var = tk.StringVar(value="0 Spots")
        self.status_var = tk.StringVar(value="Bereit.")
        self.conn_status_var = tk.StringVar(value="Nicht verbunden")

        self._build_ui()
        self._refresh_ports()
        self._start_poll_thread()
        threading.Thread(target=self._load_outdoor_calls_async, daemon=True).start()
        self.after(200, self._tick)
        self._tick_clock()
        self._refresh_worked_today()
        self.after(WORKED_TODAY_REFRESH_SECONDS * 1000, self._tick_worked_today)

    # -- style / theme -----------------------------------------------------

    def _init_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("Treeview", background=COL_ROW_EVEN, fieldbackground=COL_ROW_EVEN,
                         foreground=COL_TEXT, borderwidth=0, rowheight=26,
                         font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background=COL_PANEL_ALT, foreground=COL_ACCENT,
                         borderwidth=0, font=("Segoe UI", 9, "bold"))
        style.map("Treeview.Heading", background=[("active", COL_PANEL_ALT)])
        style.map("Treeview", background=[("selected", COL_BORDER)],
                   foreground=[("selected", COL_TEXT)])
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

        style.configure("Dark.TCombobox", fieldbackground=COL_PANEL_ALT, background=COL_PANEL_ALT,
                         foreground=COL_ACCENT, arrowcolor=COL_ACCENT, borderwidth=0)
        style.configure("Dark.TEntry", fieldbackground=COL_PANEL_ALT, foreground=COL_TEXT,
                         insertcolor=COL_TEXT, borderwidth=0)
        style.configure("Dark.Vertical.TScrollbar", background=COL_PANEL_ALT,
                         troughcolor=COL_BG, arrowcolor=COL_ACCENT, borderwidth=0)

    # -- UI construction -----------------------------------------------------

    def _build_ui(self) -> None:
        header = tk.Frame(self, bg=COL_BG)
        header.pack(fill="x", padx=12, pady=(10, 4))

        title_row = tk.Frame(header, bg=COL_BG)
        title_row.pack(fill="x")
        tk.Label(title_row, text="POTA", fg=COL_ACCENT, bg=COL_BG,
                 font=("Segoe UI", 16, "bold")).pack(side="left")
        tk.Label(title_row, text="  Tune Assist · FT-710 / Hamlib", fg=COL_MUTED, bg=COL_BG,
                 font=("Segoe UI", 11)).pack(side="left", pady=(4, 0))

        tk.Label(title_row, textvariable=self.clock_var, fg=COL_ACCENT, bg=COL_BG,
                 font=("Consolas", 11, "bold")).pack(side="right", padx=(8, 0))
        self.cat_badge = _badge(title_row, textvariable=self.cat_badge_var, fg="white", bg=COL_RED)
        self.cat_badge.pack(side="right", padx=6)
        _badge(title_row, textvariable=self.count_badge_var, fg=COL_TEXT, bg=COL_PANEL_ALT).pack(side="right", padx=6)

        filter_row = tk.Frame(header, bg=COL_BG)
        filter_row.pack(fill="x", pady=(8, 0))

        tk.Label(filter_row, text="Band", fg=COL_MUTED, bg=COL_BG,
                 font=("Segoe UI", 8)).pack(side="left", padx=(0, 4))
        band_combo = ttk.Combobox(filter_row, textvariable=self.band_filter_var, style="Dark.TCombobox",
                                   values=BAND_FILTER_OPTIONS, width=10, state="readonly")
        band_combo.pack(side="left", padx=(0, 10))
        band_combo.bind("<<ComboboxSelected>>", lambda *_: self._render_spots())

        tk.Label(filter_row, text="Mode", fg=COL_MUTED, bg=COL_BG,
                 font=("Segoe UI", 8)).pack(side="left", padx=(0, 4))
        mode_combo = ttk.Combobox(filter_row, textvariable=self.mode_filter_var, style="Dark.TCombobox",
                                   values=MODE_FILTER_OPTIONS, width=10, state="readonly")
        mode_combo.pack(side="left", padx=(0, 10))
        mode_combo.bind("<<ComboboxSelected>>", lambda *_: self._render_spots())

        tk.Label(filter_row, text="Land", fg=COL_MUTED, bg=COL_BG,
                 font=("Segoe UI", 8)).pack(side="left", padx=(0, 4))
        _chip_button(
            filter_row, "", textvariable=self.country_filter_label_var, command=self._open_country_picker,
        ).pack(side="left", padx=(0, 10))

        tk.Label(filter_row, text="Suche", fg=COL_MUTED, bg=COL_BG,
                 font=("Segoe UI", 8)).pack(side="left", padx=(0, 4))
        filter_entry = ttk.Entry(filter_row, textvariable=self.filter_var, style="Dark.TEntry", width=14)
        filter_entry.pack(side="left", padx=(0, 10), ipady=3)

        _chip_button(filter_row, "Refresh", command=self._request_spot_refresh).pack(side="left", padx=4)
        self.scan_btn = _chip_button(filter_row, "Auto: An", command=self._toggle_auto_refresh)
        self.scan_btn.pack(side="left", padx=4)
        _chip_button(filter_row, "Settings", command=self._open_settings).pack(side="left", padx=4)
        _chip_button(filter_row, "Alle anzeigen", command=self._unskip_all).pack(side="left", padx=4)
        _chip_button(filter_row, "CAT-Log", command=self._open_cat_log).pack(side="left", padx=4)

        # -- table -----------------------------------------------------------
        table_frame = tk.Frame(self, bg=COL_BG)
        table_frame.pack(fill="both", expand=True, padx=12, pady=6)

        columns = ("fav", "outdoor", "qsy", "call", "worked", "freq", "mode", "ref", "name", "loc", "age", "skip", "log")
        headers = {
            "fav": "", "outdoor": "", "qsy": "", "call": "CALLSIGN", "worked": "HEUTE", "freq": "FREQ (KHZ)",
            "mode": "MODE", "ref": "REF", "name": "NAME", "loc": "LOC", "age": "AGE", "skip": "", "log": "",
        }
        widths = {
            "fav": 30, "outdoor": 30, "qsy": 60, "call": 90, "worked": 220, "freq": 90, "mode": 70,
            "ref": 90, "name": 260, "loc": 70, "age": 60, "skip": 60, "log": 70,
        }
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=18)
        for col in columns:
            self.tree.heading(col, text=headers[col])
            anchor = "center" if col in ("fav", "outdoor", "qsy", "skip", "mode", "age", "loc") else "w"
            self.tree.column(col, width=widths[col], anchor=anchor)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview,
                             style="Dark.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("cw_even", background=COL_ROW_EVEN, foreground=COL_ACCENT)
        self.tree.tag_configure("cw_odd", background=COL_ROW_ODD, foreground=COL_ACCENT)
        self.tree.tag_configure("ssb_even", background=COL_ROW_EVEN, foreground=COL_TEXT)
        self.tree.tag_configure("ssb_odd", background=COL_ROW_ODD, foreground=COL_TEXT)
        self.tree.tag_configure("digital_even", background=COL_ROW_EVEN, foreground=COL_AMBER)
        self.tree.tag_configure("digital_odd", background=COL_ROW_ODD, foreground=COL_AMBER)
        self.tree.tag_configure("invalid", background=COL_ROW_EVEN, foreground=COL_RED)
        self.tree.tag_configure("logged", background=COL_ROW_EVEN, foreground=COL_ACCENT_DIM)
        # Listed after the mode/invalid/logged tags on favorite/outdoor rows
        # so their background wins while the other tag's foreground (mode
        # color, red for invalid, ...) still shows through - only
        # background+font are set here, on purpose. "favorite" is applied
        # after "outdoor" in _render_spots() so a row that is both wins
        # the gold favorite look.
        self.tree.tag_configure("outdoor", background=COL_OUTDOOR_BG, font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("favorite", background=COL_FAVORITE_BG, font=("Segoe UI", 10, "bold"))

        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Button-1>", self._on_tree_click)

        # -- tune bar ---------------------------------------------------------
        tune_bar = tk.Frame(self, bg=COL_PANEL)
        tune_bar.pack(fill="x", padx=12, pady=(0, 6))

        tk.Label(tune_bar, textvariable=self.power_unit_var, fg=COL_MUTED, bg=COL_PANEL,
                 font=("Segoe UI", 8)).pack(side="left", padx=(10, 4), pady=8)
        ttk.Entry(tune_bar, textvariable=self.tune_power_var, style="Dark.TEntry", width=6).pack(side="left")

        tk.Label(tune_bar, text="Versatz Fallback (Hz)", fg=COL_MUTED, bg=COL_PANEL,
                 font=("Segoe UI", 8)).pack(side="left", padx=(14, 4))
        ttk.Entry(tune_bar, textvariable=self.offset_var, style="Dark.TEntry", width=7).pack(side="left")

        sign_frame = tk.Frame(tune_bar, bg=COL_PANEL)
        sign_frame.pack(side="left", padx=10)
        tk.Radiobutton(sign_frame, text="oberhalb", variable=self.offset_sign_var, value="above",
                        fg=COL_TEXT, bg=COL_PANEL, selectcolor=COL_PANEL_ALT,
                        activebackground=COL_PANEL, font=("Segoe UI", 9)).pack(side="left")
        tk.Radiobutton(sign_frame, text="unterhalb", variable=self.offset_sign_var, value="below",
                        fg=COL_TEXT, bg=COL_PANEL, selectcolor=COL_PANEL_ALT,
                        activebackground=COL_PANEL, font=("Segoe UI", 9)).pack(side="left", padx=(6, 0))

        self.tune_btn = tk.Button(
            tune_bar, text="TUNE (halten)", bg=COL_AMBER, fg="#1a1200",
            activebackground=COL_RED, relief="flat", bd=0,
            font=("Segoe UI", 11, "bold"), padx=24, pady=8, cursor="hand2",
        )
        self.tune_btn.pack(side="right", padx=10, pady=8)
        self.tune_btn.bind("<ButtonPress-1>", self._on_tune_press)
        self.tune_btn.bind("<ButtonRelease-1>", self._on_tune_release)

        # -- status bar -----------------------------------------------------
        status_bar = tk.Frame(self, bg=COL_PANEL_ALT)
        status_bar.pack(fill="x", side="bottom")
        tk.Label(status_bar, textvariable=self.status_var, fg=COL_MUTED, bg=COL_PANEL_ALT,
                 font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=10, pady=3)

    # -- settings dialog -----------------------------------------------------

    def _power_unit_label(self) -> str:
        return "Power Level (0-1)" if self.backend_var.get() == "rigctld" else "Leistung (W)"

    def _on_backend_display_change(self) -> None:
        key = BACKEND_DISPLAY_TO_KEY.get(self.backend_display_var.get(), "ft710")
        if key == self.backend_var.get():
            return
        self.backend_var.set(key)
        if key == "rigctld" and abs(self.tune_power_var.get() - TUNE_POWER_WATTS_DEFAULT) < 1e-9:
            self.tune_power_var.set(RIGCTLD_TUNE_LEVEL_DEFAULT)
        elif key == "ft710" and abs(self.tune_power_var.get() - RIGCTLD_TUNE_LEVEL_DEFAULT) < 1e-9:
            self.tune_power_var.set(float(TUNE_POWER_WATTS_DEFAULT))
        self.power_unit_var.set(self._power_unit_label())

    def _open_settings(self) -> None:
        dlg = tk.Toplevel(self, bg=COL_PANEL)
        dlg.title("Settings")
        dlg.geometry("420x600")
        dlg.configure(bg=COL_PANEL)
        dlg.transient(self)

        def row(parent, label):
            r = tk.Frame(parent, bg=COL_PANEL)
            r.pack(fill="x", padx=14, pady=6)
            tk.Label(r, text=label, fg=COL_MUTED, bg=COL_PANEL, width=14, anchor="w",
                     font=("Segoe UI", 9)).pack(side="left")
            return r

        tk.Label(dlg, text="Funkgerät", fg=COL_ACCENT, bg=COL_PANEL,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(10, 0))

        backend_row = row(dlg, "Backend")
        backend_combo = ttk.Combobox(
            backend_row, textvariable=self.backend_display_var, style="Dark.TCombobox",
            values=list(BACKEND_DISPLAY_TO_KEY.keys()), width=22, state="readonly",
        )
        backend_combo.pack(side="left")

        param_frame = tk.Frame(dlg, bg=COL_PANEL)
        param_frame.pack(fill="x")

        serial_frame = tk.Frame(param_frame, bg=COL_PANEL)
        port_row = row(serial_frame, "CAT-Port")
        self.port_combo = ttk.Combobox(port_row, textvariable=self.port_var, style="Dark.TCombobox", width=16)
        self.port_combo.pack(side="left")
        _chip_button(port_row, "↻", command=self._refresh_ports).pack(side="left", padx=4)
        self._refresh_ports()

        baud_row = row(serial_frame, "Baud")
        ttk.Entry(baud_row, textvariable=self.baud_var, style="Dark.TEntry", width=10).pack(side="left")
        serial_frame.pack(fill="x")

        rigctld_frame = tk.Frame(param_frame, bg=COL_PANEL)

        model_row = row(rigctld_frame, "Rig-Modell")
        model_combo = ttk.Combobox(
            model_row, textvariable=self.rig_model_display_var, style="Dark.TCombobox",
            width=26, values=self.rig_model_displays,
        )
        model_combo.pack(side="left")
        _chip_button(model_row, "↻", command=lambda: refresh_models()).pack(side="left", padx=4)

        def refresh_models() -> None:
            displays = self._load_rig_models()
            model_combo["values"] = displays

        def filter_models(_event=None) -> None:
            typed = self.rig_model_display_var.get().strip().lower()
            base = self.rig_model_displays
            model_combo["values"] = base if not typed else [d for d in base if typed in d.lower()]

        model_combo.bind("<KeyRelease>", filter_models)

        host_row = row(rigctld_frame, "Host")
        ttk.Entry(host_row, textvariable=self.host_var, style="Dark.TEntry", width=16).pack(side="left")

        rport_row = row(rigctld_frame, "Netzwerk-Port")
        ttk.Entry(rport_row, textvariable=self.rig_port_var, style="Dark.TEntry", width=10).pack(side="left")

        tk.Label(
            rigctld_frame,
            text="Host = 'localhost' (Standard): App startet rigctld selbst im\n"
                 "Hintergrund mit dem gewählten Rig-Modell + CAT-Port oben - kein\n"
                 "eigenes Terminal nötig, nur Hamlib muss installiert sein\n"
                 "(hamlib.github.io). Anderer Host = Verbindung zu einem bereits\n"
                 "andernorts laufenden rigctld, Rig-Modell wird dann ignoriert.",
            fg=COL_MUTED, bg=COL_PANEL, font=("Segoe UI", 8), justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 4))

        if not self.rig_model_displays:
            refresh_models()

        def sync_frames() -> None:
            if self.backend_var.get() == "rigctld":
                rigctld_frame.pack(fill="x")
            else:
                rigctld_frame.pack_forget()

        def on_backend_selected(_event=None) -> None:
            self._on_backend_display_change()
            sync_frames()

        backend_combo.bind("<<ComboboxSelected>>", on_backend_selected)
        sync_frames()

        self.connect_btn = _chip_button(
            dlg, "Trennen" if self.cat.connected else "Verbinden", command=self._toggle_connect,
        )
        self.connect_btn.pack(padx=14, pady=(10, 4), fill="x")

        tk.Label(dlg, textvariable=self.conn_status_var, fg=COL_MUTED, bg=COL_PANEL,
                 font=("Segoe UI", 8)).pack(padx=14, pady=(0, 10))

        ttk.Separator(dlg, orient="horizontal").pack(fill="x", padx=14, pady=(0, 6))

        tk.Label(dlg, text="Log / QRZ Logbook", fg=COL_ACCENT, bg=COL_PANEL,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(0, 0))

        call_row = row(dlg, "Eig. Rufzeichen")
        ttk.Entry(call_row, textvariable=self.my_callsign_var, style="Dark.TEntry", width=16).pack(side="left")

        grid_row = row(dlg, "Eig. Locator")
        ttk.Entry(grid_row, textvariable=self.my_grid_var, style="Dark.TEntry", width=16).pack(side="left")

        qrz_row = row(dlg, "QRZ API-Key")
        ttk.Entry(qrz_row, textvariable=self.qrz_api_key_var, style="Dark.TEntry", width=28, show="•").pack(side="left")

        tk.Label(
            dlg, text="QRZ XML/Logbook-API-Key aus dem QRZ-Logbook (Subscription\nManagement). Leer lassen, um nicht zu QRZ hochzuladen.",
            fg=COL_MUTED, bg=COL_PANEL, font=("Segoe UI", 7), justify="left", anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 8))

        _chip_button(dlg, "Speichern", command=self._save_log_settings).pack(padx=14, pady=(0, 10), fill="x")

    def _save_log_settings(self) -> None:
        save_config({
            "my_callsign": self.my_callsign_var.get().strip().upper(),
            "my_gridsquare": self.my_grid_var.get().strip().upper(),
            "qrz_api_key": self.qrz_api_key_var.get().strip(),
        })
        self._log("Log-/QRZ-Einstellungen gespeichert.")

    # -- rigctld model list / auto-launch --------------------------------------

    def _load_rig_models(self) -> list[str]:
        exe = find_rigctld_executable()
        if not exe:
            self._log("rigctld nicht gefunden - Hamlib installieren (hamlib.github.io) oder zum PATH hinzufügen.")
            return self.rig_model_displays
        try:
            self.rig_models = list_rig_models(exe)
        except (OSError, subprocess.SubprocessError) as exc:
            self._log(f"Rig-Modelle konnten nicht geladen werden: {exc}")
            return self.rig_model_displays
        self.rig_model_displays = [f"{mid} - {mfg} {model}" for mid, mfg, model in self.rig_models]
        if not self.rig_model_displays:
            self._log("'rigctld -l' lieferte keine Modell-Liste.")
        return self.rig_model_displays

    def _rig_model_id_from_display(self, display: str) -> int | None:
        if not display:
            return None
        head = display.split(" - ", 1)[0].strip()
        try:
            return int(head)
        except ValueError:
            return None

    def _save_rig_model(self, display: str, model_id: int) -> None:
        config = load_config()
        config["rig_model_display"] = display
        config["rig_model_id"] = model_id
        save_config(config)

    # -- CAT trace dialog -----------------------------------------------------

    def _open_cat_log(self) -> None:
        dlg = tk.Toplevel(self, bg=COL_PANEL)
        dlg.title("CAT-Log")
        dlg.geometry("640x420")
        dlg.configure(bg=COL_PANEL)
        dlg.transient(self)

        tk.Label(
            dlg, text="Rohe CAT-Befehle/-Antworten (nur FT-710-CAT-Backend) - "
            "zeigt exakt, was gesendet/empfangen wird.",
            fg=COL_MUTED, bg=COL_PANEL, font=("Segoe UI", 8), justify="left", wraplength=610,
        ).pack(anchor="w", padx=10, pady=(10, 4))

        text = tk.Text(
            dlg, bg=COL_PANEL_ALT, fg=COL_TEXT, insertbackground=COL_TEXT,
            font=("Consolas", 9), wrap="none", relief="flat", bd=0,
        )
        text.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        def refresh() -> None:
            trace = getattr(self.cat, "trace", None)
            text.configure(state="normal")
            text.delete("1.0", "end")
            if trace is None:
                text.insert(
                    "end",
                    "Aktuelles Backend zeichnet keine Rohdaten auf "
                    "(nur FT-710-CAT direkt, nicht rigctld).",
                )
            elif not trace:
                text.insert("end", "Noch keine CAT-Befehle aufgezeichnet.")
            else:
                text.insert("end", "\n".join(trace))
                text.see("end")
            text.configure(state="disabled")

        btn_row = tk.Frame(dlg, bg=COL_PANEL)
        btn_row.pack(fill="x", padx=10, pady=(0, 10))
        _chip_button(btn_row, "Aktualisieren", command=refresh).pack(side="left")
        _chip_button(
            btn_row, "Leeren",
            command=lambda: (getattr(self.cat, "trace", []).clear(), refresh()),
        ).pack(side="left", padx=6)

        refresh()

    # -- logging / status ----------------------------------------------------

    def _log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.status_var.set(f"[{ts}] {message}")

    # -- serial connection ----------------------------------------------------

    def _refresh_ports(self) -> None:
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if hasattr(self, "port_combo"):
            self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def _toggle_connect(self) -> None:
        if self.cat.connected:
            if self.tune.active:
                self.tune.stop()
            self.cat.disconnect()
            if self.rigctld_process.running:
                self.rigctld_process.stop()
                self._log("rigctld beendet.")
            self.conn_status_var.set("Nicht verbunden")
            self.connect_btn.configure(text="Verbinden")
            self.cat_badge.configure(bg=COL_RED)
            self._log("Getrennt.")
            return

        if self.backend_var.get() == "rigctld":
            host = self.host_var.get().strip() or RIGCTLD_HOST_DEFAULT
            managed = host.lower() in ("localhost", "127.0.0.1")
            rig_port = self.rig_port_var.get()

            if managed:
                display = self.rig_model_display_var.get().strip()
                model_id = self._rig_model_id_from_display(display)
                if model_id is None:
                    messagebox.showerror(
                        "Fehler", "Bitte ein Rig-Modell auswählen (ggf. erst über ↻ laden).",
                    )
                    return
                serial_port = self.port_var.get()
                if not serial_port:
                    messagebox.showerror("Fehler", "Bitte den CAT-Port des Funkgeräts auswählen.")
                    return
                exe = find_rigctld_executable()
                if not exe:
                    messagebox.showerror(
                        "rigctld nicht gefunden",
                        "Hamlib ist nicht installiert oder rigctld nicht im PATH.\n"
                        "Siehe https://hamlib.github.io/",
                    )
                    return
                try:
                    self.rigctld_process.start(
                        exe, model_id, serial_port, self.baud_var.get(), "127.0.0.1", rig_port,
                    )
                except CatError as exc:
                    messagebox.showerror("rigctld-Fehler", str(exc))
                    return
                self._log(f"rigctld gestartet (Modell {model_id}, {serial_port}, Port {rig_port}).")
                connect_host = "127.0.0.1"
            else:
                connect_host = host

            cat = RigctldClient()
            try:
                cat.connect(connect_host, rig_port)
            except (OSError, CatError) as exc:
                if managed:
                    self.rigctld_process.stop()
                messagebox.showerror("Verbindungsfehler", str(exc))
                return
            self.cat = cat
            self.tune.cat = cat
            self.conn_status_var.set(f"Verbunden (rigctld {connect_host}:{rig_port})")
            self._log(f"Verbunden mit rigctld auf {connect_host}:{rig_port}.")
            if managed:
                self._save_rig_model(self.rig_model_display_var.get().strip(), model_id)
        else:
            port = self.port_var.get()
            if not port:
                messagebox.showerror("Fehler", "Bitte einen Port auswählen.")
                return
            cat = Ft710Cat()
            try:
                cat.connect(port, self.baud_var.get())
            except (serial.SerialException, CatError) as exc:
                messagebox.showerror("Verbindungsfehler", str(exc))
                return
            self.cat = cat
            self.tune.cat = cat
            self.conn_status_var.set(f"Verbunden ({port}, Freq.-Breite {cat.freq_width})")
            self._log(f"Verbunden mit {port}.")

        self.connect_btn.configure(text="Trennen")
        self.cat_badge.configure(bg=COL_GREEN)

    # -- POTA polling -----------------------------------------------------------

    def _start_poll_thread(self) -> None:
        thread = threading.Thread(target=self._poll_loop, daemon=True)
        thread.start()

    def _poll_loop(self) -> None:
        while not self.stop_poll_event.is_set():
            if self.auto_refresh:
                self._fetch_once()
            self.stop_poll_event.wait(POTA_POLL_SECONDS_DEFAULT)

    def _toggle_auto_refresh(self) -> None:
        self.auto_refresh = not self.auto_refresh
        self.scan_btn.configure(
            text="Auto: An" if self.auto_refresh else "Auto: Aus",
            fg=COL_ACCENT if self.auto_refresh else COL_MUTED,
        )

    def _unskip_all(self) -> None:
        self.skipped_ids.clear()
        self._render_spots()

    def _request_spot_refresh(self) -> None:
        threading.Thread(target=self._fetch_once, daemon=True).start()

    def _fetch_once(self) -> None:
        try:
            spots = fetch_pota_spots()
            self.spot_result_queue.put(("ok", spots))
        except (requests.RequestException, ValueError) as exc:
            self.spot_result_queue.put(("error", str(exc)))

    def _load_outdoor_calls_async(self) -> None:
        self.outdoor_result_queue.put(load_outdoor_calls())

    # -- table rendering ----------------------------------------------------------

    def _spot_passes_filters(self, spot: Spot) -> bool:
        if spot.spot_id in self.skipped_ids:
            return False
        band = self.band_filter_var.get()
        if band != BAND_FILTER_OPTIONS[0] and band_for_khz(spot.frequency_khz) != band:
            return False
        mode_filter = self.mode_filter_var.get()
        if mode_filter != MODE_FILTER_OPTIONS[0] and mode_category(spot.mode) != mode_filter.lower():
            return False
        if self.selected_countries and country_for_location(spot.location_desc) not in self.selected_countries:
            return False
        needle = self.filter_var.get().strip().lower()
        if needle:
            haystack = f"{spot.activator} {spot.reference} {spot.park_name} {spot.comments} {spot.location_desc}".lower()
            if needle not in haystack:
                return False
        return True

    def _update_country_options(self) -> None:
        self.known_countries |= {country_for_location(s.location_desc) for s in self.spots} - {"?"}

    def _refresh_worked_today(self) -> None:
        self.worked_today_index = load_worked_today()
        self._render_spots()

    def _tick_worked_today(self) -> None:
        self._refresh_worked_today()
        self.after(WORKED_TODAY_REFRESH_SECONDS * 1000, self._tick_worked_today)

    def _country_filter_label(self) -> str:
        n = len(self.selected_countries)
        if n == 0:
            return COUNTRY_FILTER_ALL
        if n == 1:
            return next(iter(self.selected_countries))
        return f"{n} Länder"

    def _save_hunt_countries(self) -> None:
        config = load_config()
        config["hunt_countries"] = sorted(self.selected_countries)
        save_config(config)

    def _open_country_picker(self) -> None:
        dlg = tk.Toplevel(self, bg=COL_PANEL)
        dlg.title("Länder auswählen")
        dlg.geometry("300x460")
        dlg.configure(bg=COL_PANEL)
        dlg.transient(self)

        tk.Label(
            dlg, text="Nur Spots aus ausgewählten Ländern anzeigen.\nNichts ausgewählt = alle Länder.",
            fg=COL_MUTED, bg=COL_PANEL, font=("Segoe UI", 8), justify="left",
        ).pack(anchor="w", padx=12, pady=(10, 6))

        list_frame = tk.Frame(dlg, bg=COL_PANEL)
        list_frame.pack(fill="both", expand=True, padx=12)

        canvas = tk.Canvas(list_frame, bg=COL_PANEL, highlightthickness=0)
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview,
                             style="Dark.Vertical.TScrollbar")
        check_frame = tk.Frame(canvas, bg=COL_PANEL)
        check_frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=check_frame, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        country_vars: dict[str, tk.BooleanVar] = {}
        for country in sorted(self.known_countries):
            var = tk.BooleanVar(value=country in self.selected_countries)
            country_vars[country] = var
            tk.Checkbutton(
                check_frame, text=country, variable=var,
                fg=COL_TEXT, bg=COL_PANEL, selectcolor=COL_PANEL_ALT,
                activebackground=COL_PANEL, activeforeground=COL_TEXT,
                font=("Segoe UI", 9), anchor="w",
            ).pack(fill="x", padx=2, pady=1)

        if not country_vars:
            tk.Label(check_frame, text="(noch keine Länder in der aktuellen Spot-Liste)",
                     fg=COL_MUTED, bg=COL_PANEL, font=("Segoe UI", 8)).pack(padx=2, pady=4)

        def select_all() -> None:
            for var in country_vars.values():
                var.set(True)

        def select_none() -> None:
            for var in country_vars.values():
                var.set(False)

        def toggle_continent(continent: str) -> None:
            members = [c for c in country_vars if ISO2_TO_CONTINENT.get(c) == continent]
            if not members:
                return
            all_selected = all(country_vars[c].get() for c in members)
            for c in members:
                country_vars[c].set(not all_selected)

        def apply_and_close() -> None:
            self.selected_countries = {c for c, v in country_vars.items() if v.get()}
            self.country_filter_label_var.set(self._country_filter_label())
            self._save_hunt_countries()
            self._render_spots()
            dlg.destroy()

        top_btn_row = tk.Frame(dlg, bg=COL_PANEL)
        top_btn_row.pack(fill="x", padx=12, pady=(6, 0))
        _chip_button(top_btn_row, "Alle", command=select_all).pack(side="left")
        _chip_button(top_btn_row, "Keine", command=select_none).pack(side="left", padx=6)

        continent_row = tk.Frame(dlg, bg=COL_PANEL)
        continent_row.pack(fill="x", padx=12, pady=(6, 0))
        tk.Label(continent_row, text="Kontinent:", fg=COL_MUTED, bg=COL_PANEL,
                 font=("Segoe UI", 8)).pack(side="left", padx=(0, 4))
        for continent in CONTINENT_LABELS:
            _chip_button(
                continent_row, continent, command=lambda c=continent: toggle_continent(c),
            ).pack(side="left", padx=2)

        btn_row = tk.Frame(dlg, bg=COL_PANEL)
        btn_row.pack(fill="x", padx=12, pady=10)
        _chip_button(btn_row, "Abbrechen", command=dlg.destroy).pack(side="right")
        _chip_button(btn_row, "Übernehmen", command=apply_and_close).pack(side="right", padx=6)

    def _is_favorite(self, spot: Spot) -> bool:
        return (spot.activator or "").strip().upper() in self.favorite_calls

    def _is_outdoor(self, spot: Spot) -> bool:
        return (spot.activator or "").strip().upper() in self.outdoor_calls

    def _render_spots(self) -> None:
        self.tree.delete(*self.tree.get_children())
        visible = [s for s in self.spots if self._spot_passes_filters(s)]
        visible.sort(key=lambda s: (not self._is_favorite(s), not self._is_outdoor(s)))
        for i, spot in enumerate(visible):
            category = mode_category(spot.mode)
            parity = "even" if i % 2 == 0 else "odd"
            badge = worked_today_badge(spot, self.worked_today_index)
            already_logged = spot.spot_id in self.logged_spot_ids or badge == "DUPE"
            if spot.invalid:
                tag = "invalid"
            elif already_logged:
                tag = "logged"
            else:
                tag = f"{category}_{parity}"

            name = spot.park_name
            call = spot.activator
            freq_text = f"{spot.frequency_khz:.1f}"
            if spot.invalid or already_logged:
                name = strike(name)
                call = strike(call)
                freq_text = strike(freq_text)

            if badge == "DUPE":
                worked_text = "♻ Heute schon gearbeitet"
            elif badge:
                worked_text = f"🆕 {badge}"
            else:
                worked_text = ""

            is_fav = self._is_favorite(spot)
            is_outdoor = self._is_outdoor(spot)
            fav_icon = "⭐" if is_fav else "☆"
            outdoor_icon = "🏕" if is_outdoor else ""
            tags = (tag,)
            if is_outdoor:
                tags += ("outdoor",)
            if is_fav:
                tags += ("favorite",)

            self.tree.insert("", "end", iid=str(spot.spot_id), tags=tags, values=(
                fav_icon,
                outdoor_icon,
                "▶ QSY",
                call,
                worked_text,
                freq_text,
                spot.mode,
                spot.reference,
                name,
                spot.location_desc,
                format_age(spot.spot_time),
                "✕ Skip",
                "📝 Log",
            ))
        self.count_badge_var.set(f"{len(visible)} Spots")

    def _on_tree_click(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
        if not row_id:
            return
        if col_id == "#1":
            self._toggle_favorite(int(row_id))
        elif col_id == "#3":
            self._qsy_to_spot_id(int(row_id))
        elif col_id == "#12":
            self.skipped_ids.add(int(row_id))
            self._render_spots()
        elif col_id == "#13":
            self._open_log_dialog_for_spot_id(int(row_id))

    def _on_tree_double_click(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self._qsy_to_spot_id(int(row_id))

    def _toggle_favorite(self, spot_id: int) -> None:
        spot = next((s for s in self.spots if s.spot_id == spot_id), None)
        if spot is None:
            return
        call = (spot.activator or "").strip().upper()
        if not call:
            return
        if call in self.favorite_calls:
            self.favorite_calls.discard(call)
        else:
            self.favorite_calls.add(call)
        config = load_config()
        config["favorite_calls"] = sorted(self.favorite_calls)
        save_config(config)
        self._render_spots()

    def _qsy_to_spot_id(self, spot_id: int) -> None:
        spot = next((s for s in self.spots if s.spot_id == spot_id), None)
        if spot is None:
            return
        if self.tune.active:
            messagebox.showwarning("Sendung aktiv", "Bitte zuerst TUNE loslassen.")
            return
        if not self.cat.connected:
            messagebox.showerror("Fehler", "Funkgerät nicht verbunden.")
            return
        try:
            mode_name = resolve_mode_name(spot.mode, spot.freq_hz)
            # Mode before frequency: switching mode (e.g. into/out of CW)
            # shifts the actual VFO frequency by the CW pitch/offset on
            # this radio, so setting frequency last is what keeps it from
            # being dragged off-target by a mode change afterward.
            mode_ok = set_verified(self.cat.get_mode, self.cat.set_mode, mode_name)
            freq_ok = set_verified(self.cat.get_freq_hz, self.cat.set_freq_hz, spot.freq_hz)
            confirmed_freq = self.cat.get_freq_hz()
            confirmed_mode = self.cat.get_mode()
        except CatError as exc:
            messagebox.showerror("CAT-Fehler", str(exc))
            return
        if not freq_ok or not mode_ok:
            self._log(
                f"Warnung: QSY zu {spot.activator} konnte nicht sicher bestätigt werden "
                f"(Funkgerät antwortet nicht mit der gesetzten Frequenz/Mode)."
            )
        self._log(
            f"QSY zu {spot.activator}: Ziel {spot.freq_hz} Hz ({mode_name}) - "
            f"Funkgerät bestätigt {confirmed_freq} Hz ({confirmed_mode}) - {spot.reference}"
        )

    # -- log contact ------------------------------------------------------------

    def _open_log_dialog_for_spot_id(self, spot_id: int) -> None:
        spot = next((s for s in self.spots if s.spot_id == spot_id), None)
        if spot is None:
            return
        self._open_log_dialog(spot)

    def _open_log_dialog(self, spot: Spot) -> None:
        if not self.my_callsign_var.get().strip():
            messagebox.showwarning(
                "Eigenes Rufzeichen fehlt",
                "Bitte zuerst in den Settings unter 'Log / QRZ Logbook' das "
                "eigene Rufzeichen eintragen.",
            )
            return

        now = datetime.now(timezone.utc)
        default_rst = "599" if mode_category(spot.mode) in ("cw", "digital") else "59"

        dlg = tk.Toplevel(self, bg=COL_PANEL)
        dlg.title(f"Log Contact - {spot.activator}")
        dlg.geometry("360x460")
        dlg.configure(bg=COL_PANEL)
        dlg.transient(self)

        vars_: dict[str, tk.StringVar] = {
            "call": tk.StringVar(value=spot.activator),
            "date": tk.StringVar(value=now.strftime("%Y%m%d")),
            "time": tk.StringVar(value=now.strftime("%H%M")),
            "band": tk.StringVar(value=band_for_khz(spot.frequency_khz)),
            "freq": tk.StringVar(value=f"{spot.frequency_khz / 1000:.4f}"),
            "mode": tk.StringVar(value=(spot.mode or "").upper()),
            "rst_sent": tk.StringVar(value=default_rst),
            "rst_rcvd": tk.StringVar(value=default_rst),
            "name": tk.StringVar(value=""),
            "grid": tk.StringVar(value=""),
            "sig_info": tk.StringVar(value=spot.reference),
            "comment": tk.StringVar(value=spot.park_name),
        }

        def row(label, key, width=20):
            r = tk.Frame(dlg, bg=COL_PANEL)
            r.pack(fill="x", padx=14, pady=4)
            tk.Label(r, text=label, fg=COL_MUTED, bg=COL_PANEL, width=13, anchor="w",
                     font=("Segoe UI", 9)).pack(side="left")
            ttk.Entry(r, textvariable=vars_[key], style="Dark.TEntry", width=width).pack(side="left")

        tk.Label(dlg, text=f"POTA-Referenz: {spot.reference}", fg=COL_ACCENT, bg=COL_PANEL,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(10, 4))

        row("Rufzeichen", "call")
        row("Datum (UTC)", "date", width=10)
        row("Zeit (UTC)", "time", width=10)
        row("Band", "band", width=8)
        row("Freq (MHz)", "freq", width=10)
        row("Mode", "mode", width=10)
        row("RST gesendet", "rst_sent", width=6)
        row("RST empfangen", "rst_rcvd", width=6)
        row("Name", "name")
        row("Locator", "grid")
        row("SIG_INFO", "sig_info")
        row("Kommentar", "comment")

        status_var = tk.StringVar(value="")
        tk.Label(dlg, textvariable=status_var, fg=COL_MUTED, bg=COL_PANEL,
                 font=("Segoe UI", 8), wraplength=330, justify="left").pack(fill="x", padx=14, pady=(8, 0))

        btn_row = tk.Frame(dlg, bg=COL_PANEL)
        btn_row.pack(fill="x", padx=14, pady=12)
        _chip_button(btn_row, "Abbrechen", command=dlg.destroy).pack(side="right", padx=4)
        _chip_button(
            btn_row, "Log Contact",
            command=lambda: self._submit_log_contact(dlg, vars_, status_var, spot.spot_id),
        ).pack(side="right", padx=4)

    def _submit_log_contact(self, dlg: tk.Toplevel, vars_: dict, status_var: tk.StringVar, spot_id: int) -> None:
        call = vars_["call"].get().strip().upper()
        date = vars_["date"].get().strip()
        time_ = vars_["time"].get().strip()

        if not call:
            status_var.set("Rufzeichen fehlt.")
            return
        if len(date) != 8 or not date.isdigit():
            status_var.set("Datum muss im Format JJJJMMTT sein.")
            return
        if len(time_) not in (4, 6) or not time_.isdigit():
            status_var.set("Uhrzeit muss im Format SSMM oder SSMMSS sein.")
            return

        fields = {
            "CALL": call,
            "QSO_DATE": date,
            "TIME_ON": time_,
            "BAND": vars_["band"].get().strip().lower(),
            "MODE": vars_["mode"].get().strip().upper(),
            "FREQ": vars_["freq"].get().strip(),
            "RST_SENT": vars_["rst_sent"].get().strip(),
            "RST_RCVD": vars_["rst_rcvd"].get().strip(),
            "STATION_CALLSIGN": self.my_callsign_var.get().strip().upper(),
        }
        if self.my_grid_var.get().strip():
            fields["MY_GRIDSQUARE"] = self.my_grid_var.get().strip().upper()
        if vars_["grid"].get().strip():
            fields["GRIDSQUARE"] = vars_["grid"].get().strip().upper()
        if vars_["name"].get().strip():
            fields["NAME"] = vars_["name"].get().strip()
        if vars_["comment"].get().strip():
            fields["COMMENT"] = vars_["comment"].get().strip()
        if vars_["sig_info"].get().strip():
            fields["SIG"] = "POTA"
            fields["SIG_INFO"] = vars_["sig_info"].get().strip()

        record = format_adif_record(fields)
        try:
            path = append_adif_record(date, record)
        except OSError as exc:
            status_var.set(f"ADIF konnte nicht gespeichert werden: {exc}")
            return

        self._log(f"QSO mit {call} in {path.name} gespeichert.")

        self.logged_spot_ids.add(spot_id)
        self._refresh_worked_today()

        api_key = self.qrz_api_key_var.get().strip()
        if api_key:
            threading.Thread(
                target=self._upload_to_qrz_async, args=(api_key, record, call), daemon=True,
            ).start()

        dlg.destroy()

    def _upload_to_qrz_async(self, api_key: str, record: str, call: str) -> None:
        try:
            ok, info = upload_to_qrz(api_key, record)
        except requests.RequestException as exc:
            self.log_result_queue.put(f"QRZ-Upload für {call} fehlgeschlagen: {exc}")
            return
        if ok:
            self.log_result_queue.put(f"QRZ-Logbuch: {call} hochgeladen (LOGID {info}).")
        else:
            self.log_result_queue.put(f"QRZ-Logbuch: Upload für {call} fehlgeschlagen ({info}).")

    # -- tune button ------------------------------------------------------------

    def _on_tune_press(self, _event) -> None:
        if self.tune.active:
            return
        sign = -1 if self.offset_sign_var.get() == "below" else 1
        try:
            self.tune.start(self.tune_power_var.get(), sign, self.offset_var.get())
        except CatError as exc:
            self._log(f"Tune abgebrochen: {exc}")
            return
        self.tune_btn.configure(text="● ON AIR", bg=COL_RED, fg="white")
        offset = self.tune.last_offset_hz
        self._log(
            f"TUNE gestartet: {self.tune.saved_freq} Hz -> "
            f"{self.tune.saved_freq + offset} Hz "
            f"({self.tune.saved_mode}-Bandbreite {abs(offset)} Hz), "
            f"{self.tune_power_var.get():g} {self._power_unit_label()} CW"
        )

    def _on_tune_release(self, _event) -> None:
        if not self.tune.active:
            return
        target_freq = self.tune.saved_freq
        self.tune.stop()
        self.tune_btn.configure(text="TUNE (halten)", bg=COL_AMBER, fg="#1a1200")
        try:
            confirmed_freq = self.cat.get_freq_hz()
        except CatError:
            confirmed_freq = None
        if confirmed_freq is not None and target_freq is not None:
            self._log(
                f"TUNE beendet: Ziel {target_freq} Hz - Funkgerät bestätigt {confirmed_freq} Hz."
            )
        else:
            self._log("TUNE beendet, vorherige Frequenz/Mode/Leistung wiederhergestellt.")

    # -- periodic tick ------------------------------------------------------------

    def _tick_clock(self) -> None:
        self.clock_var.set(datetime.now(timezone.utc).strftime("%H:%M:%Sz"))
        self.after(1000, self._tick_clock)

    def _tick(self) -> None:
        try:
            while True:
                kind, payload = self.spot_result_queue.get_nowait()
                if kind == "ok":
                    self.spots = payload
                    self._update_country_options()
                    self._render_spots()
                    self._log(f"{len(self.spots)} Spots aktualisiert.")
                else:
                    self._log(f"Fehler beim Abrufen: {payload}")
        except queue.Empty:
            pass

        try:
            while True:
                self._log(self.log_result_queue.get_nowait())
        except queue.Empty:
            pass

        try:
            while True:
                self.outdoor_calls = self.outdoor_result_queue.get_nowait()
                self._render_spots()
                self._log(f"{len(self.outdoor_calls)} Draußenfunker-Rufzeichen geladen.")
        except queue.Empty:
            pass

        if self.tune.active and self.tune.elapsed() > MAX_TUNE_SECONDS:
            self.tune.stop()
            self.tune_btn.configure(text="TUNE (halten)", bg=COL_AMBER, fg="#1a1200")
            self._log("Sicherheits-Timeout erreicht, TUNE automatisch beendet.")

        self.after(200, self._tick)

    def destroy(self) -> None:
        self.stop_poll_event.set()
        if self.tune.active:
            self.tune.stop()
        self.cat.disconnect()
        if self.rigctld_process.running:
            self.rigctld_process.stop()
        super().destroy()


if __name__ == "__main__":
    App().mainloop()
