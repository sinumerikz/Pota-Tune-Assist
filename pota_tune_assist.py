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
    QSY the radio to its frequency and mode. Spots are placed on the map
    and distance-ranked by their *park reference's* coordinates, which
    POTA publishes for every park - that's where the activator actually
    is, and it needs no third-party subscription. Optionally each spot
    also gets a "Chance to Hear" estimate from live Reverse Beacon
    Network skimmer reports (see the RBN section below).
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

import functools
import io
import json
import math
import os
import queue
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import requests
import serial
import tkintermapview
from PIL import Image, ImageDraw, ImageTk
import serial.tools.list_ports

POTA_SPOTS_URL = "https://api.pota.app/spot/activator"
POTA_SPOT_POST_URL = "https://api.pota.app/spot/"
# Per-park detail endpoint - only used as a fallback for spots whose own
# JSON carries no coordinates (see fetch_park_info); a park reference is a
# fixed location, so results are cached in the DB for PARK_CACHE_TTL_DAYS.
POTA_PARK_URL = "https://api.pota.app/park/{}"
POTA_POLL_SECONDS_DEFAULT = 60
WORKED_TODAY_REFRESH_SECONDS = 60
QRZ_LOGBOOK_API_URL = "https://logbook.qrz.com/api"

# Park coordinates change ~never, so a long TTL is fine; a *failed* lookup
# is retried much sooner so one network hiccup doesn't blank a park's pin
# for half a year.
PARK_CACHE_TTL_DAYS = 180
PARK_CACHE_MISS_TTL_HOURS = 6
PARK_WORKER_COUNT = 4

# Default respot comment template, filled in via render_respot_comment().
# Placeholders: {call} {mycall} {rst_sent} {rst_rcvd} {freq} {mode} {ref}
RESPOT_TEMPLATE_DEFAULT = "Tnx fer QSO ({rst_sent}/{rst_rcvd}) 73 es {mycall}"

# Solar/propagation indices (N0NBH's widely-used ham radio solar data feed,
# free, no API key). SFI/K/A come from here; MUF is preferentially replaced
# below by a real Germany-specific reading from the Juliusruh ionosonde
# (see GIRO_MUF_URL) when that succeeds, falling back to this feed's own
# global MUF(3000km) reference-station figure otherwise.
SOLAR_DATA_URL = "https://www.hamqsl.com/solarxml.php"
SOLAR_POLL_SECONDS = 15 * 60

# GIRO/DIDBase (Global Ionosphere Radio Observatory) real-time data query
# for the Juliusruh ionosonde (Germany, URSI station code JR055), operated
# by the Leibniz Institute of Atmospheric Physics - MUFD = MUF(3000km) from
# actual German ionospheric soundings, not a generic global figure.
GIRO_MUF_URL = "https://lgdc.uml.edu/common/DIDBGetValues"
GIRO_STATION_CODE = "JR055"


def app_dir() -> Path:
    """Directory the script/exe lives in - config and logs live next to it."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# Legacy path from before settings moved into DB_PATH below - only read
# once, to migrate an existing install's settings into the database.
LEGACY_CONFIG_PATH = app_dir() / "pota_tune_assist_config.json"
DB_PATH = app_dir() / "pota_tune_assist.db"
LOG_DIR = app_dir() / "logs"
OUTDOOR_LIST_PATH = app_dir() / "draussenfunker.txt"
OUTDOOR_LIST_URL = "https://calls.draussenfunker.de/df-polo-notes.txt"

ADIF_HEADER = (
    "POTA Tune Assist ADIF Log\n"
    "<ADIF_VER:5>3.1.4\n"
    "<PROGRAMID:15>POTA-TuneAssist\n"
    "<EOH>\n"
)


def _db_connect() -> sqlite3.Connection:
    """Settings live in a sibling SQLite file (same directory as the .exe,
    same as the QSO logs) instead of the old JSON file - QSO logs
    themselves stay plain ADIF, only app settings/state moved here."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS qrz_cache ("
        "call TEXT PRIMARY KEY, lat REAL, lon REAL, op_name TEXT, fetched_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS park_cache ("
        "reference TEXT PRIMARY KEY, lat REAL, lon REAL, fetched_at TEXT NOT NULL)"
    )
    return conn


def _migrate_legacy_json_config(conn: sqlite3.Connection) -> None:
    """One-time import of an old pota_tune_assist_config.json into the
    settings table, for installs upgrading from a version that still used
    the JSON file. No-op once the settings table already has any rows."""
    if not LEGACY_CONFIG_PATH.exists():
        return
    if conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone():
        return
    try:
        with open(LEGACY_CONFIG_PATH, "r", encoding="utf-8") as f:
            old_config = json.load(f)
    except (OSError, ValueError):
        return
    conn.executemany(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        [(key, json.dumps(value)) for key, value in old_config.items()],
    )
    conn.commit()


def load_config() -> dict:
    try:
        conn = _db_connect()
    except sqlite3.Error:
        return {}
    try:
        _migrate_legacy_json_config(conn)
        config: dict = {}
        for key, raw_value in conn.execute("SELECT key, value FROM settings"):
            try:
                config[key] = json.loads(raw_value)
            except ValueError:
                config[key] = raw_value
        return config
    finally:
        conn.close()


def save_config(config: dict) -> None:
    """Upserts the given keys into the settings table - unlike the old
    JSON file, keys already stored but not present in `config` are left
    alone rather than dropped, since every caller here already loads,
    merges, and passes the full dict back anyway."""
    try:
        conn = _db_connect()
    except sqlite3.Error:
        return
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            [(key, json.dumps(value)) for key, value in config.items()],
        )
        conn.commit()
    finally:
        conn.close()


def load_qrz_cache() -> dict[str, "QrzCallsignInfo | None"]:
    """Callsign -> QrzCallsignInfo (or None for a confirmed "not in QRZ")
    persisted from previous sessions, so returning activators show their
    OP/KM immediately instead of waiting on a fresh lookup every single
    app start. Entries older than QRZ_CACHE_TTL_DAYS are treated as if
    they were never cached (a fresh lookup will refresh them)."""
    try:
        conn = _db_connect()
    except sqlite3.Error:
        return {}
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=QRZ_CACHE_TTL_DAYS)).isoformat()
        cache: dict[str, QrzCallsignInfo | None] = {}
        rows = conn.execute(
            "SELECT call, lat, lon, op_name FROM qrz_cache WHERE fetched_at >= ?", (cutoff,)
        )
        for call, lat, lon, op_name in rows:
            latlon = (lat, lon) if lat is not None and lon is not None else None
            cache[call] = QrzCallsignInfo(latlon=latlon, op_name=op_name) if (latlon or op_name) else None
        return cache
    finally:
        conn.close()


def save_qrz_cache_entry(call: str, info: "QrzCallsignInfo | None") -> None:
    lat = lon = op_name = None
    if info is not None:
        op_name = info.op_name
        if info.latlon is not None:
            lat, lon = info.latlon
    try:
        conn = _db_connect()
    except sqlite3.Error:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO qrz_cache (call, lat, lon, op_name, fetched_at) VALUES (?, ?, ?, ?, ?)",
            (call, lat, lon, op_name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def load_park_cache() -> dict[str, tuple[float, float] | None]:
    """Park reference -> coordinates (or None for a park POTA has no
    position for), persisted across runs. Parks don't move, so hits stay
    valid for PARK_CACHE_TTL_DAYS; misses expire after
    PARK_CACHE_MISS_TTL_HOURS so they get retried reasonably soon."""
    try:
        conn = _db_connect()
    except sqlite3.Error:
        return {}
    try:
        now = datetime.now(timezone.utc)
        hit_cutoff = (now - timedelta(days=PARK_CACHE_TTL_DAYS)).isoformat()
        miss_cutoff = (now - timedelta(hours=PARK_CACHE_MISS_TTL_HOURS)).isoformat()
        cache: dict[str, tuple[float, float] | None] = {}
        rows = conn.execute(
            "SELECT reference, lat, lon FROM park_cache "
            "WHERE (lat IS NOT NULL AND fetched_at >= ?) OR (lat IS NULL AND fetched_at >= ?)",
            (hit_cutoff, miss_cutoff),
        )
        for reference, lat, lon in rows:
            cache[reference] = (lat, lon) if lat is not None and lon is not None else None
        return cache
    finally:
        conn.close()


def save_park_cache_entry(reference: str, latlon: tuple[float, float] | None) -> None:
    lat, lon = latlon if latlon is not None else (None, None)
    try:
        conn = _db_connect()
    except sqlite3.Error:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO park_cache (reference, lat, lon, fetched_at) VALUES (?, ?, ?, ?)",
            (reference, lat, lon, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


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


def load_all_adif_records() -> list[dict[str, str]]:
    """Every QSO this app has ever logged (every daily ADIF file in
    LOG_DIR) - unlike load_worked_today() this isn't scoped to a single
    day, it's the source for the all-time stats panel."""
    if not LOG_DIR.is_dir():
        return []
    records: list[dict[str, str]] = []
    for path in sorted(LOG_DIR.glob("pota_tune_assist_log_*.adi")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        records.extend(parse_adif_records(text))
    return records


def load_adif_records_for_day(qso_date: str) -> list[dict[str, str]]:
    """QSOs logged on one UTC day (YYYYMMDD) - each day already has its own
    log file (see daily_adif_path()), so this is just that single file."""
    path = daily_adif_path(qso_date)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return parse_adif_records(text)


def load_adif_records_for_stats_filter(filter_key: str) -> list[dict[str, str]]:
    """Records for the stats panel's Heute/Gestern/Gesamt filter. "today"
    and "yesterday" are UTC calendar days, matching the UTC day boundary
    daily_adif_path() already splits log files on."""
    if filter_key == "today":
        return load_adif_records_for_day(datetime.now(timezone.utc).strftime("%Y%m%d"))
    if filter_key == "yesterday":
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        return load_adif_records_for_day(yesterday.strftime("%Y%m%d"))
    return load_all_adif_records()


def compute_band_mode_park_stats(records: list[dict[str, str]]) -> dict:
    """Aggregates logged QSOs into unique-park counts per band and mode
    category (cw/ssb/digital) for the stats panel - unique parks worked,
    not raw QSO counts, since that's what actually matters for POTA
    hunter awards (working the same park twice on the same band/mode
    doesn't count twice)."""
    band_mode_parks: dict[str, dict[str, set[str]]] = {}
    all_parks: set[str] = set()
    for rec in records:
        band = rec.get("BAND", "").strip().lower()
        park = rec.get("SIG_INFO", "").strip()
        if not band or not park:
            continue
        category = mode_category(rec.get("MODE", ""))
        band_mode_parks.setdefault(band, {"cw": set(), "ssb": set(), "digital": set()})[category].add(park)
        all_parks.add(park)
    return {
        "total_qsos": len(records),
        "total_parks": len(all_parks),
        "band_mode_parks": band_mode_parks,
    }


def worked_today_badge(spot: "Spot", worked_index: dict[str, list[dict[str, str]]]) -> str:
    """"" (nothing worked today), "DUPE" (exact band+mode+park already
    logged today), or "New <Band/Mode/Park> (bisher: band/mode, ...)" for a
    spot whose band, mode, or park hasn't been logged yet today for this
    call - only meaningful once the call has at least one QSO logged today.
    The parenthetical lists what WAS already worked today, not the current
    spot's own band/mode/freq (that's redundant - it's already shown in the
    spot row's own columns; what's actually useful here is what to compare
    it against)."""
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
    prior_band_modes = sorted({
        f"{r.get('BAND', '').strip()}/{r.get('MODE', '').strip().upper()}" for r in records
    })
    return f"New {'/'.join(new_dims)} (bisher: {', '.join(prior_band_modes)})"


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


def upload_to_wavelog(base_url: str, api_key: str, station_profile_id: str, adif_record: str) -> tuple[bool, str]:
    """POST a single ADIF record to a Wavelog (Cloudlog-API-compatible)
    instance's QSO endpoint. Returns (success, message). base_url is the
    user's own server (self-hosted or hosted), no fixed domain - the
    `/index.php/api/qso` path works regardless of whether the instance has
    pretty URLs/mod_rewrite enabled, since that's CodeIgniter's default
    routable path."""
    url = base_url.rstrip("/") + "/index.php/api/qso"
    resp = requests.post(
        url,
        json={
            "key": api_key,
            "station_profile_id": station_profile_id,
            "type": "adif",
            "string": adif_record,
        },
        headers={"Accept": "application/json"},
        timeout=10,
    )
    if resp.status_code in (401, 403):
        # Wavelog rejects here for two config reasons, not app bugs: the API
        # key must be a "read/write" key (a "read-only" one 401s on POST),
        # and station_profile_id must belong to the same Wavelog account
        # that issued the key.
        return False, (
            f"HTTP {resp.status_code} - API-Key prüfen (muss in Wavelog unter "
            "Settings -> API Keys als 'read/write' angelegt sein, nicht "
            "'read-only') und ob die Station-Profil-ID zu diesem Key gehört."
        )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        return False, f"Unerwartete Antwort (kein JSON): {resp.text[:200]!r}"
    status = str(data.get("status", "")).strip().lower()
    if status in ("created", "success", "ok"):
        return True, status or "ok"
    return False, str(data.get("reason") or data.get("message") or data)


def post_pota_spot(
    activator: str, spotter: str, frequency_khz: float, mode: str, reference: str, comments: str,
) -> tuple[bool, str]:
    """POST a spot to the public POTA spot network (same endpoint pota.app's
    own site and hunter tools like hunterlog use to (re-)spot an activator,
    e.g. right after logging them, so other hunters know they're still on
    frequency). frequency is in kHz, matching the units POTA_SPOTS_URL's GET
    response already uses elsewhere in this file (see fetch_pota_spots())."""
    resp = requests.post(
        POTA_SPOT_POST_URL,
        json={
            "activator": activator,
            "spotter": spotter,
            "frequency": f"{frequency_khz:.2f}",
            "reference": reference,
            "mode": mode,
            "source": "POTA-TuneAssist",
            "comments": comments,
        },
        headers={"Content-Type": "application/json", "origin": "https://pota.app"},
        timeout=10,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        return False, f"HTTP {resp.status_code} - {resp.text[:200]}"
    return True, "ok"


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render_respot_comment(template: str, values: dict[str, str]) -> str:
    """Fill {placeholder} slots in a user-configured respot template.
    Unknown placeholders are left as-is instead of raising, since this text
    comes from Settings and a typo shouldn't block the respot."""
    try:
        return template.format_map(_SafeFormatDict(values))
    except (ValueError, IndexError):
        return template


CAT_BAUD_DEFAULT = 38400
CAT_CMD_DELAY = 0.05
CAT_REPLY_TIMEOUT = 0.3

RIGCTLD_HOST_DEFAULT = "localhost"
RIGCTLD_PORT_DEFAULT = 4532
RIGCTLD_TIMEOUT = 1.0
RIGCTLD_LAUNCH_TIMEOUT = 5.0

CAT_HEALTH_CHECK_SECONDS = 15
RECONNECT_BACKOFF_INITIAL_SECONDS = 5
RECONNECT_BACKOFF_MAX_SECONDS = 60

TUNE_POWER_WATTS_DEFAULT = 5
RIGCTLD_TUNE_LEVEL_DEFAULT = 0.05
TUNE_OFFSET_HZ_DEFAULT = 5000
MAX_TUNE_SECONDS = 10

# Operating power restored after TUNE releases, looked up by mode instead of
# trying to read the pre-tune power back off the rig (not every backend
# supports reading RFPOWER back at all - see TuneController).
SSB_POWER_WATTS_DEFAULT = 100
CW_POWER_WATTS_DEFAULT = 100
RIGCTLD_SSB_LEVEL_DEFAULT = 0.5
RIGCTLD_CW_LEVEL_DEFAULT = 0.5

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
            except (serial.SerialException, OSError):
                pass
        self._ser = None

    def _trace_add(self, entry: str) -> None:
        self.trace.append(entry)
        del self.trace[:-60]

    def _write(self, text: str) -> None:
        assert self._ser is not None
        try:
            self._ser.write(text.encode("ascii"))
        except (serial.SerialException, OSError) as exc:
            self.disconnect()
            raise CatError(f"Funkgerät nicht mehr erreichbar (Kabel gezogen?): {exc}") from exc
        self._trace_add(f"TX {text!r}")
        time.sleep(CAT_CMD_DELAY)

    def _transact(self, cmd: str, timeout: float = CAT_REPLY_TIMEOUT) -> str:
        if not self.connected:
            raise CatError("Funkgerät nicht verbunden")
        assert self._ser is not None
        try:
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
        except (serial.SerialException, OSError) as exc:
            # Physically unplugging the USB-serial adapter mid-session
            # doesn't clear is_open on its own - the stale handle would
            # otherwise keep reporting "connected" forever, and every
            # command against it would keep failing the same way even
            # after the cable is plugged back in (a fresh Serial object/
            # OS handle is needed either way, see connect()).
            self.disconnect()
            raise CatError(f"Funkgerät nicht mehr erreichbar (Kabel gezogen?): {exc}") from exc
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
                # int() alone chokes on a decimal-formatted reply (e.g.
                # "14074000.000000", which some rig backends return) -
                # float() first accepts both that and a plain integer.
                try:
                    return int(float(line.split(":", 1)[1].strip()))
                except ValueError as exc:
                    raise CatError(f"Ungültige Frequenz in rigctld-Antwort: {line!r}") from exc
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

    def set_power(self, level: float) -> None:
        self._transact(f"L RFPOWER {level}")

    def key_down(self) -> None:
        self._transact("T 1")

    def key_up(self) -> None:
        self._transact("T 0")


def find_rigctld_executable(manual_path: str = "") -> str | None:
    """Locates rigctld. Checks, in order: a manually configured path (for
    the common case of Hamlib's Windows builds being a plain zip the user
    extracts anywhere, not an installer that adds itself to PATH or a
    fixed folder), then PATH, then a couple of common default install
    locations."""
    manual_path = (manual_path or "").strip()
    if manual_path and Path(manual_path).is_file():
        return manual_path

    exe = shutil.which("rigctld")
    if exe:
        return exe

    if sys.platform == "win32":
        candidates = []
        for envvar in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            base = os.environ.get(envvar)
            if base:
                candidates.append(Path(base) / "Hamlib" / "bin" / "rigctld.exe")
        for base in (os.environ.get("LOCALAPPDATA"), "C:\\"):
            if base:
                candidates.append(Path(base) / "Hamlib" / "bin" / "rigctld.exe")
        for candidate in candidates:
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
    """Mirrors the Cardputer firmware's tune logic: read back frequency and
    mode before transmitting, restore both afterwards - never assume,
    always verify first. Works against either backend (Ft710Cat or
    RigctldClient) since both expose the same get/set_freq_hz, get/set_mode
    and get/set_power methods.

    Power is handled differently: not every Hamlib rig backend supports
    reading RFPOWER back (many only support setting it), so instead of
    reading the pre-tune power off the rig and hoping to restore it,
    restore_power_by_mode (passed into start()) looks up the configured
    SSB/CW operating power for whatever mode is being restored to."""

    def __init__(self, cat):
        self.cat = cat
        self.active = False
        self.start_time = 0.0
        self.saved_freq: int | None = None
        self.saved_mode: str | None = None
        self.restore_power_by_mode = None
        self.last_offset_hz = 0

    def start(
        self, tune_power: float, offset_sign: int, fallback_offset_hz: int,
        restore_power_by_mode,
    ) -> None:
        if self.active:
            return
        if not self.cat.connected:
            raise CatError("Funkgerät nicht verbunden")

        freq = self.cat.get_freq_hz()
        mode = self.cat.get_mode()

        self.saved_freq = freq
        self.saved_mode = mode
        self.restore_power_by_mode = restore_power_by_mode

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
        # Each restore step runs even if an earlier one fails - e.g. a
        # rig backend that rejects the RFPOWER restore must not also skip
        # un-keying PTT or restoring frequency/mode. Failures are collected
        # and raised together at the end so the caller still finds out.
        errors: list[str] = []
        try:
            self.cat.key_up()
        except CatError as exc:
            errors.append(str(exc))
        if self.saved_mode is not None:
            try:
                set_verified(self.cat.get_mode, self.cat.set_mode, self.saved_mode)
            except CatError as exc:
                errors.append(str(exc))
        if self.saved_freq is not None:
            try:
                set_verified(self.cat.get_freq_hz, self.cat.set_freq_hz, self.saved_freq)
            except CatError as exc:
                errors.append(str(exc))
        if self.saved_mode is not None and self.restore_power_by_mode is not None:
            try:
                self.cat.set_power(self.restore_power_by_mode(self.saved_mode))
            except CatError as exc:
                errors.append(str(exc))
        if errors:
            raise CatError("; ".join(errors))

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
    # Park position, straight out of the spot feed when it carries one
    # (see park_latlon_from_payload). None means "not in this payload" -
    # the reference is then resolved via the park endpoint instead, so a
    # missing value here is a cache miss, not "no location exists".
    park_latlon: tuple[float, float] | None = None

    @property
    def freq_hz(self) -> int:
        return int(round(self.frequency_khz * 1000))


def _coerce_float(value) -> float | None:
    """POTA's JSON hands back coordinates as numbers on some endpoints and
    as strings on others (and null/"" when a park has none)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def park_latlon_from_payload(item: dict) -> tuple[float, float] | None:
    """Park coordinates from a POTA spot or park JSON object. Prefers the
    explicit lat/lon pair and falls back to the Maidenhead grid the same
    payloads carry, which is accurate to a few km - plenty for a map pin
    and a distance column."""
    lat = _coerce_float(item.get("latitude"))
    lon = _coerce_float(item.get("longitude"))
    # (0, 0) is in the Atlantic off Africa - as a park location it's always
    # an unset field serialized as zero, not a real position.
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        if not (lat == 0.0 and lon == 0.0):
            return lat, lon
    for key in ("grid6", "grid4", "grid"):
        latlon = grid_to_latlon(item.get(key) or "")
        if latlon is not None:
            return latlon
    return None


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
            park_latlon=park_latlon_from_payload(item),
        ))
    spots.sort(key=lambda s: s.frequency_khz)
    return spots


def fetch_park_info(reference: str) -> tuple[float, float] | None:
    """Coordinates for a single park reference. Raises on network errors so
    the caller can tell "POTA is unreachable right now" (retry soon) apart
    from "this park genuinely has no coordinates" (cache the miss)."""
    resp = requests.get(POTA_PARK_URL.format(urllib.parse.quote(reference)), timeout=10)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return park_latlon_from_payload(data)


def fetch_solar_data() -> dict[str, str] | None:
    """SFI/K/A/MUF from N0NBH's solar-terrestrial XML feed. Returns None
    on any network/parse failure - never raises, this is a nice-to-have
    display, not something that should ever interrupt the app."""
    try:
        resp = requests.get(SOLAR_DATA_URL, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except (requests.RequestException, ET.ParseError):
        return None
    solardata = root.find(".//solardata")
    if solardata is None:
        return None

    def get(tag: str) -> str:
        value = solardata.findtext(tag)
        return value.strip() if value and value.strip() else "?"

    result = {
        "sfi": get("solarflux"),
        "k": get("kindex"),
        "a": get("aindex"),
        "muf": get("muf"),
    }
    if result["muf"] == "?":
        # Diagnostic for whichever tag actually holds it, if the feed
        # doesn't use "muf" as assumed - couldn't verify this live.
        known_tags = ", ".join(child.tag for child in solardata)
        result["muf_hamqsl_diag"] = f"kein <muf> in Feed, vorhandene Felder: {known_tags}"
    return result


def fetch_juliusruh_muf() -> tuple[str | None, str]:
    """Latest MUF(3000km) reading from the Juliusruh ionosonde (GIRO/
    DIDBase, station JR055) - a real Germany-specific measurement, unlike
    hamqsl's global reference-station MUF. Returns (value_or_None,
    diagnostic) - this integration couldn't be tested against the live
    service, so the diagnostic carries enough of the raw response to
    debug a format mismatch instead of just failing silently."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=6)
    params = {
        "ursiCode": GIRO_STATION_CODE,
        "charName": "MUFD",
        "DMUF": "3000",
        "fromDate": since.strftime("%Y.%m.%d"),
        "toDate": now.strftime("%Y.%m.%d"),
    }
    try:
        resp = requests.get(GIRO_MUF_URL, params=params, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return None, f"Netzwerkfehler: {exc}"

    text = resp.text.strip()
    if not text:
        return None, "leere Antwort von GIRO/DIDBase"

    lines = [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not lines:
        return None, f"keine Datenzeilen, Rohantwort (Anfang): {text[:300]!r}"

    last_line = lines[-1]
    value = None
    for token in last_line.split()[1:]:
        if "." not in token:
            # DIDBase's confidence-score column is a plain integer
            # (e.g. "11", "88") and can fall in the same numeric range as
            # a real MUF reading - only a token with decimal precision is
            # trusted as the actual measured value.
            continue
        try:
            candidate = float(token)
        except ValueError:
            continue
        if 2.0 <= candidate <= 60.0:  # plausible MUF range in MHz
            value = candidate
            break
    if value is None:
        return None, f"konnte keinen Zahlenwert extrahieren, letzte Zeile: {last_line!r}"
    return f"{value:.1f}", "ok"


def grid_to_latlon(grid: str) -> tuple[float, float] | None:
    """Center coordinates of a Maidenhead grid square (4 or 6+ chars) -
    used to place "Eig. Locator" on the map for the distance column."""
    grid = (grid or "").strip().upper()
    if len(grid) < 4 or not grid[:2].isalpha() or not grid[2:4].isdigit():
        return None
    try:
        lon = (ord(grid[0]) - ord("A")) * 20 - 180
        lat = (ord(grid[1]) - ord("A")) * 10 - 90
        lon += int(grid[2]) * 2
        lat += int(grid[3]) * 1
        if len(grid) >= 6 and grid[4].isalpha() and grid[5].isalpha():
            lon += (ord(grid[4]) - ord("A")) * (2 / 24) + (1 / 24)
            lat += (ord(grid[5]) - ord("A")) * (1 / 24) + (0.5 / 24)
        else:
            lon += 1.0
            lat += 0.5
    except (ValueError, IndexError):
        return None
    return lat, lon


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def destination_point_km(lat: float, lon: float, bearing_deg: float, distance_km: float) -> tuple[float, float]:
    """Point reached from (lat, lon) after travelling distance_km along a
    great circle on bearing_deg (0 = north, clockwise) - the standard
    spherical "direct" geodesic problem, inverse of haversine_km. Used to
    build the "how far is this activator being heard" ring (see
    circle_points_km) point by point."""
    r = 6371.0
    lat1, lon1, brng = math.radians(lat), math.radians(lon), math.radians(bearing_deg)
    d_r = distance_km / r
    lat2 = math.asin(math.sin(lat1) * math.cos(d_r) + math.cos(lat1) * math.sin(d_r) * math.cos(brng))
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(d_r) * math.cos(lat1),
        math.cos(d_r) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180  # normalize to [-180, 180]


def circle_points_km(lat: float, lon: float, radius_km: float, segments: int = 72) -> list[tuple[float, float]]:
    """Points approximating a geodesic circle of radius_km around (lat, lon)
    - a real circle-on-the-sphere, not a lat/lon ellipse, so it stays round
    close to the poles too (irrelevant for ham bands, but free to get
    right). The map widget's polygon closes itself, so no need to repeat
    the first point at the end."""
    return [destination_point_km(lat, lon, bearing, radius_km) for bearing in range(0, 360, 360 // segments)]


QRZ_XML_URL = "https://xmldata.qrz.com/xml/current/"
LOCATOR_WORKER_COUNT = 12
QRZ_CACHE_TTL_DAYS = 30

# Fallback world view for the map panel, used only until an own QTH locator
# is known (see _update_own_qth_marker(), which then recenters on it).
MAP_DEFAULT_LAT = 20.0
MAP_DEFAULT_LON = 0.0
MAP_DEFAULT_ZOOM = 2

# CARTO's free "Dark Matter" basemap (© OpenStreetMap contributors, © CARTO)
# instead of tkintermapview's light-themed OSM default, to match the rest
# of the app's dark UI instead of a bright rectangle punched into it.
MAP_TILE_SERVER_DARK = "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"

MAP_OWN_MARKER_DIAMETER = 16
MAP_SPOT_MARKER_DIAMETER = 11

# Distinct color per band for spot markers on the map, so band is readable
# at a glance instead of every spot looking the same. Falls back to
# BAND_MAP_COLOR_DEFAULT for anything not listed (e.g. band_for_khz()
# returning "?" for an out-of-band frequency).
BAND_MAP_COLORS = {
    "160m": "#8B5A2B",
    "80m": "#E67E22",
    "60m": "#F1C40F",
    "40m": "#3498DB",
    "30m": "#1ABC9C",
    "20m": "#2ECC71",
    "17m": "#16A085",
    "15m": "#9B59B6",
    "12m": "#E91E8C",
    "10m": "#E74C3C",
    "6m": "#ECF0F1",
}
BAND_MAP_COLOR_DEFAULT = "#95A5A6"

# ---------------------------------------------------------------------------
# Reverse Beacon Network - "Chance to Hear"
#
# The RBN is a worldwide network of CW/RTTY skimmers: unattended receivers
# that continuously decode every callsign they hear and report it with a
# signal-to-noise figure. That makes it a live, measured propagation probe -
# if skimmers *near me* are hearing an activator strongly right now, so
# probably can I; if skimmers near me hear nothing while distant ones do,
# the path to my own QTH is the part that's closed.
#
# Feed is the classic DX-cluster-style telnet stream (no API/key, login is
# just a callsign), one line per report:
#   DX de EA5WU-#:    7018.3  RW1M           CW    19 dB  18 WPM  CQ  2259Z
# ---------------------------------------------------------------------------

RBN_HOST = "telnet.reversebeacon.net"
RBN_PORT = 7000  # CW/RTTY skimmers; port 7001 is the separate FT8-only feed
RBN_LOGIN_TIMEOUT = 20.0
# The feed is continuous, so a long silence means the connection died in a
# way that never surfaced as a socket error (very common on this service).
RBN_SOCKET_TIMEOUT = 120.0
RBN_RECONNECT_INITIAL_SECONDS = 5.0
RBN_RECONNECT_MAX_SECONDS = 300.0

# A skimmer report only says something about propagation *right now* - past
# this age it's dropped entirely rather than shown as current.
RBN_REPORT_TTL_SECONDS = 15 * 60
# Bound on the firehose: the RBN reports the whole world, of which only the
# handful of currently POTA-spotted calls interest us. Least-recently-heard
# calls are evicted once this many are tracked.
RBN_MAX_TRACKED_CALLS = 4000

# The store fills continuously in the background, but redrawing the whole
# spot table is disruptive (it resets scroll position), so chance values are
# only refreshed on this interval on top of the normal spot poll.
RBN_RENDER_REFRESH_SECONDS = 45.0
RBN_BADGE_REFRESH_SECONDS = 2.0
RBN_PRUNE_INTERVAL_SECONDS = 60.0

# Skimmers this close count as "my region" - i.e. what they hear is a fair
# proxy for what my own station can hear.
RBN_REGION_RADIUS_KM = 1200.0

# Map "how far is this activator being heard" ring, drawn around the park
# on QSY: number of points approximating the geodesic circle (see
# circle_points_km) - plenty smooth at any zoom level actually usable on a
# desktop map, without generating an excessive canvas polygon.
RBN_HEAR_CIRCLE_SEGMENTS = 72
# Within that region, reports are still distance-weighted: a skimmer
# RBN_WEIGHT_HALF_KM away counts half as much as one at my own QTH.
RBN_WEIGHT_HALF_KM = 400.0

RBN_LINE_RE = re.compile(
    # The skimmer's node suffix ("-#", "-1") is kept by the pattern and
    # stripped in normalize_skimmer_call(), so there's exactly one place
    # that knows about it.
    r"^DX\s+de\s+(?P<skimmer>[A-Z0-9/#-]+):\s+"
    r"(?P<khz>\d+(?:\.\d+)?)\s+"
    r"(?P<dx>[A-Z0-9/]+)\s+"
    r"(?P<mode>[A-Z0-9]+)\s+"
    r"(?P<snr>-?\d+)\s*dB",
    re.IGNORECASE,
)

# Approximate center of each DXCC entity that hosts skimmers, used to place
# a reporting skimmer on the map well enough to tell "near me" from "far
# away". Country-level accuracy is entirely sufficient at the ~400-1200 km
# scale the weighting works on, and unlike a QRZ lookup it costs nothing and
# needs no subscription. Matched longest-prefix-first, so the more specific
# entries below (EA8, VE7, ...) win over their parent prefix.
SKIMMER_PREFIX_LATLON: dict[str, tuple[float, float]] = {
    # -- Europe ----------------------------------------------------------
    "D": (51.0, 10.0),        # Germany (DA-DR); DU/DS/D2/D4/D6 overridden below
    "G": (52.5, -1.5), "M": (52.5, -1.5), "2E": (52.5, -1.5),
    "GM": (56.5, -4.0), "MM": (56.5, -4.0), "2M": (56.5, -4.0),
    "GW": (52.3, -3.6), "MW": (52.3, -3.6), "2W": (52.3, -3.6),
    "GI": (54.6, -6.5), "MI": (54.6, -6.5), "2I": (54.6, -6.5),
    "GD": (54.2, -4.5), "GJ": (49.2, -2.1), "GU": (49.5, -2.5),
    "EI": (53.3, -8.0), "EJ": (53.3, -8.0),
    "F": (46.5, 2.5), "TM": (46.5, 2.5), "TK": (42.1, 9.1),
    "ON": (50.6, 4.6), "OO": (50.6, 4.6), "OT": (50.6, 4.6),
    "PA": (52.2, 5.5), "PB": (52.2, 5.5), "PC": (52.2, 5.5), "PD": (52.2, 5.5),
    "PE": (52.2, 5.5), "PF": (52.2, 5.5), "PG": (52.2, 5.5), "PH": (52.2, 5.5),
    "PI": (52.2, 5.5),
    "LX": (49.8, 6.1), "OE": (47.6, 14.1),
    "HB": (46.8, 8.2), "HB0": (47.2, 9.5),
    "I": (42.8, 12.5), "IK": (42.8, 12.5), "IZ": (42.8, 12.5), "IW": (42.8, 12.5),
    "IU": (42.8, 12.5), "IS": (40.1, 9.1), "IT": (37.6, 14.0), "IQ": (42.8, 12.5),
    "EA": (40.3, -3.7), "EB": (40.3, -3.7), "EC": (40.3, -3.7), "ED": (40.3, -3.7),
    "EE": (40.3, -3.7), "EF": (40.3, -3.7), "EG": (40.3, -3.7), "EH": (40.3, -3.7),
    "EA6": (39.6, 2.9), "EA8": (28.3, -16.6), "EA9": (35.3, -3.0),
    "CT": (39.6, -8.0), "CR": (39.6, -8.0), "CQ": (39.6, -8.0),
    "CT3": (32.7, -16.9), "CR3": (32.7, -16.9), "CU": (38.5, -28.2),
    "SP": (52.0, 19.5), "SN": (52.0, 19.5), "SO": (52.0, 19.5),
    "SQ": (52.0, 19.5), "SR": (52.0, 19.5), "3Z": (52.0, 19.5), "HF": (52.0, 19.5),
    "OK": (49.8, 15.5), "OL": (49.8, 15.5), "OM": (48.7, 19.5),
    "HA": (47.2, 19.4), "HG": (47.2, 19.4),
    "YO": (45.9, 25.0), "YP": (45.9, 25.0), "YQ": (45.9, 25.0), "YR": (45.9, 25.0),
    "LZ": (42.7, 25.5), "S5": (46.1, 14.8), "9A": (45.1, 16.4),
    "YU": (44.2, 20.9), "YT": (44.2, 20.9), "YZ": (44.2, 20.9),
    "E7": (44.0, 17.8), "Z3": (41.6, 21.7), "4O": (42.7, 19.4), "ZA": (41.2, 20.1),
    "SV": (39.0, 22.0), "SW": (39.0, 22.0), "SX": (39.0, 22.0), "SZ": (39.0, 22.0),
    "SV5": (36.2, 28.0), "SV9": (35.2, 24.9),
    "5B": (35.1, 33.4), "C4": (35.1, 33.4), "H2": (35.1, 33.4),
    "TA": (39.0, 35.0), "TC": (39.0, 35.0),
    "OH": (62.5, 26.0), "OF": (62.5, 26.0), "OG": (62.5, 26.0), "OI": (62.5, 26.0),
    "OH0": (60.2, 20.0), "OJ0": (59.8, 19.5),
    "SM": (60.0, 15.5), "SA": (60.0, 15.5), "SB": (60.0, 15.5), "SC": (60.0, 15.5),
    "SD": (60.0, 15.5), "SE": (60.0, 15.5), "SF": (60.0, 15.5), "SG": (60.0, 15.5),
    "SH": (60.0, 15.5), "SI": (60.0, 15.5), "SJ": (60.0, 15.5), "SK": (60.0, 15.5),
    "SL": (60.0, 15.5), "7S": (60.0, 15.5), "8S": (60.0, 15.5),
    "LA": (61.0, 9.0), "LB": (61.0, 9.0), "LC": (61.0, 9.0), "LN": (61.0, 9.0),
    "JW": (78.2, 15.6), "JX": (71.0, -8.3),
    "OZ": (56.0, 10.0), "OU": (56.0, 10.0), "OV": (56.0, 10.0), "5Q": (56.0, 10.0),
    "OW": (56.0, 10.0), "OY": (62.0, -6.8), "OX": (72.0, -40.0),
    "TF": (64.9, -19.0),
    "ES": (58.7, 25.5), "YL": (56.9, 24.6), "LY": (55.3, 23.9),
    "EU": (53.7, 27.9), "EV": (53.7, 27.9), "EW": (53.7, 27.9),
    "UR": (49.0, 32.0), "US": (49.0, 32.0), "UT": (49.0, 32.0), "UU": (49.0, 32.0),
    "UV": (49.0, 32.0), "UW": (49.0, 32.0), "UX": (49.0, 32.0), "UY": (49.0, 32.0),
    "UZ": (49.0, 32.0), "EM": (49.0, 32.0), "EN": (49.0, 32.0), "EO": (49.0, 32.0),
    # Russia: European by default, Asiatic call areas 8/9/0 pulled east.
    "R": (55.7, 37.6), "U": (55.7, 37.6),
    "R8": (56.0, 68.0), "R9": (56.0, 68.0), "R0": (60.0, 100.0),
    "U8": (56.0, 68.0), "U9": (56.0, 68.0), "U0": (60.0, 100.0),
    "UA8": (56.0, 68.0), "UA9": (56.0, 68.0), "UA0": (60.0, 100.0),
    "RA9": (56.0, 68.0), "RA0": (60.0, 100.0),
    "RN9": (56.0, 68.0), "RN0": (60.0, 100.0),
    "RK9": (56.0, 68.0), "RK0": (60.0, 100.0),
    "RZ9": (56.0, 68.0), "RZ0": (60.0, 100.0),
    "RW9": (56.0, 68.0), "RW0": (60.0, 100.0),
    "RV9": (56.0, 68.0), "RV0": (60.0, 100.0),
    "RU9": (56.0, 68.0), "RU0": (60.0, 100.0),
    "RX9": (56.0, 68.0), "RX0": (60.0, 100.0),
    "R2F": (54.7, 20.5), "UA2": (54.7, 20.5), "RA2": (54.7, 20.5),
    "3A": (43.7, 7.4), "9H": (35.9, 14.4), "ZB": (36.1, -5.3),
    "T7": (43.9, 12.4), "HV": (41.9, 12.5), "1A": (41.9, 12.5),
    "OJ": (59.8, 19.5), "TR": (-0.8, 11.6),
    # -- Africa / Middle East ---------------------------------------------
    "CN": (32.0, -6.0), "SU": (26.8, 30.8), "7X": (28.0, 2.6), "3V": (34.0, 9.6),
    "5A": (27.0, 17.0), "ZS": (-29.0, 24.0), "V5": (-22.0, 17.0),
    "5Z": (-1.3, 36.8), "5H": (-6.4, 35.0), "9J": (-13.1, 27.9),
    "Z2": (-19.0, 29.9), "7Q": (-13.3, 34.3), "C9": (-18.7, 35.5),
    "3B8": (-20.3, 57.5), "3B9": (-19.7, 63.4), "FR": (-21.1, 55.5),
    "D2": (-11.2, 17.9), "D4": (16.0, -24.0), "D6": (-11.9, 43.3),
    "TU": (7.5, -5.5), "5N": (9.1, 8.7), "9G": (7.9, -1.0),
    "4X": (31.5, 34.9), "4Z": (31.5, 34.9), "OD": (33.9, 35.5), "JY": (31.9, 35.9),
    "A4": (21.5, 57.0), "A6": (24.3, 54.0), "A7": (25.3, 51.2), "A9": (26.0, 50.5),
    "HZ": (24.0, 45.0), "7Z": (24.0, 45.0), "9K": (29.3, 47.5), "YI": (33.3, 44.4),
    "EP": (32.5, 53.7), "EK": (40.2, 44.5), "4J": (40.4, 49.9), "4K": (40.4, 49.9),
    "4L": (42.0, 43.5), "EX": (41.2, 74.8), "EY": (38.9, 71.3), "EZ": (38.9, 59.6),
    "UN": (48.0, 68.0), "UO": (48.0, 68.0), "UP": (48.0, 68.0), "UQ": (48.0, 68.0),
    # -- Asia / Oceania ---------------------------------------------------
    "JA": (36.0, 138.0), "JE": (36.0, 138.0), "JF": (36.0, 138.0), "JG": (36.0, 138.0),
    "JH": (36.0, 138.0), "JI": (36.0, 138.0), "JJ": (36.0, 138.0), "JK": (36.0, 138.0),
    "JL": (36.0, 138.0), "JM": (36.0, 138.0), "JN": (36.0, 138.0), "JO": (36.0, 138.0),
    "JP": (36.0, 138.0), "JQ": (36.0, 138.0), "JR": (36.0, 138.0), "JS": (36.0, 138.0),
    "7J": (36.0, 138.0), "7K": (36.0, 138.0), "7L": (36.0, 138.0), "7M": (36.0, 138.0),
    "7N": (36.0, 138.0), "8J": (36.0, 138.0), "8N": (36.0, 138.0),
    "HL": (36.5, 127.9), "DS": (36.5, 127.9), "DT": (36.5, 127.9), "6K": (36.5, 127.9),
    "6L": (36.5, 127.9), "6M": (36.5, 127.9), "6N": (36.5, 127.9),
    "BY": (35.0, 105.0), "BA": (35.0, 105.0), "BD": (35.0, 105.0), "BG": (35.0, 105.0),
    "BH": (35.0, 105.0), "BI": (35.0, 105.0), "BT": (35.0, 105.0),
    "BV": (23.7, 121.0), "VR": (22.3, 114.2), "XX": (22.2, 113.5),
    "VU": (21.0, 78.0), "AT": (21.0, 78.0), "4S": (7.9, 80.8), "8Q": (3.2, 73.2),
    "S2": (23.7, 90.4), "9N": (28.4, 84.1), "AP": (30.4, 69.3),
    "HS": (15.0, 101.0), "E2": (15.0, 101.0), "XW": (18.0, 103.0),
    "XU": (12.6, 104.9), "XV": (16.0, 106.0), "3W": (16.0, 106.0),
    "9M": (4.2, 102.0), "9V": (1.35, 103.8), "V8": (4.5, 114.7),
    "YB": (-2.5, 118.0), "YC": (-2.5, 118.0), "YD": (-2.5, 118.0), "YE": (-2.5, 118.0),
    "YF": (-2.5, 118.0), "YG": (-2.5, 118.0), "YH": (-2.5, 118.0),
    "DU": (12.9, 121.8), "DV": (12.9, 121.8), "DW": (12.9, 121.8),
    "DX": (12.9, 121.8), "DY": (12.9, 121.8), "DZ": (12.9, 121.8),
    "VK": (-25.0, 134.0), "AX": (-25.0, 134.0), "VI": (-25.0, 134.0),
    "VK9": (-29.0, 168.0), "VK0": (-53.1, 73.5),
    "ZL": (-41.0, 174.0), "ZM": (-41.0, 174.0), "ZK": (-41.0, 174.0),
    "FK": (-21.3, 165.5), "FO": (-17.6, -149.4), "3D2": (-17.8, 178.0),
    "KH2": (13.4, 144.8), "KH0": (15.2, 145.7), "KH8": (-14.3, -170.7),
    # -- North America -----------------------------------------------------
    # US mainland is resolved from the call-area digit (see skimmer_latlon);
    # only the offshore entities need explicit prefixes here.
    "KH6": (20.8, -156.3), "WH6": (20.8, -156.3), "NH6": (20.8, -156.3), "AH6": (20.8, -156.3),
    "KH7": (20.8, -156.3), "WH7": (20.8, -156.3), "NH7": (20.8, -156.3), "AH7": (20.8, -156.3),
    "KL": (64.2, -149.5), "WL": (64.2, -149.5), "NL": (64.2, -149.5), "AL": (64.2, -149.5),
    "KP4": (18.2, -66.5), "WP4": (18.2, -66.5), "NP4": (18.2, -66.5), "KP3": (18.2, -66.5),
    "KP2": (17.7, -64.8), "WP2": (17.7, -64.8), "NP2": (17.7, -64.8),
    "VE": (56.0, -96.0), "VA": (56.0, -96.0), "VO": (48.9, -57.0), "VY": (63.0, -95.0),
    "VE1": (44.7, -63.6), "VA1": (44.7, -63.6), "VE9": (46.5, -66.5),
    "VE2": (46.8, -71.2), "VA2": (46.8, -71.2),
    "VE3": (44.0, -79.0), "VA3": (44.0, -79.0),
    "VE4": (50.0, -97.1), "VA4": (50.0, -97.1),
    "VE5": (52.1, -106.6), "VA5": (52.1, -106.6),
    "VE6": (53.5, -113.5), "VA6": (53.5, -113.5),
    "VE7": (49.3, -123.1), "VA7": (49.3, -123.1),
    "XE": (23.6, -102.5), "XF": (23.6, -102.5), "4A": (23.6, -102.5), "6D": (23.6, -102.5),
    "CO": (21.5, -79.5), "CM": (21.5, -79.5), "HI": (18.7, -70.2), "HH": (18.9, -72.3),
    "6Y": (18.1, -77.3), "8P": (13.2, -59.5), "9Y": (10.5, -61.3), "ZF": (19.3, -81.3),
    "V3": (17.2, -88.8), "TG": (15.5, -90.2), "YS": (13.8, -88.9), "HR": (14.8, -86.2),
    "YN": (12.9, -85.2), "TI": (9.9, -84.1), "HP": (8.5, -80.0), "V4": (17.3, -62.7),
    "FM": (14.6, -61.0), "FG": (16.2, -61.6), "FS": (18.1, -63.1), "PJ": (12.2, -68.9),
    "J3": (12.1, -61.7), "J6": (13.9, -61.0), "J7": (15.4, -61.4), "J8": (13.2, -61.2),
    "V2": (17.1, -61.8), "VP2": (18.0, -63.1), "VP5": (21.7, -71.8), "VP9": (32.3, -64.8),
    "C6": (24.7, -78.0), "ZP": (-23.4, -58.4),
    # -- South America -----------------------------------------------------
    "PY": (-15.8, -47.9), "PP": (-15.8, -47.9), "PQ": (-15.8, -47.9), "PR": (-15.8, -47.9),
    "PS": (-15.8, -47.9), "PT": (-15.8, -47.9), "PU": (-15.8, -47.9), "PV": (-15.8, -47.9),
    "PW": (-15.8, -47.9), "ZV": (-15.8, -47.9), "ZW": (-15.8, -47.9), "ZX": (-15.8, -47.9),
    "ZY": (-15.8, -47.9), "ZZ": (-15.8, -47.9),
    "LU": (-34.6, -58.4), "LO": (-34.6, -58.4), "LP": (-34.6, -58.4), "LQ": (-34.6, -58.4),
    "LR": (-34.6, -58.4), "LS": (-34.6, -58.4), "LT": (-34.6, -58.4), "LV": (-34.6, -58.4),
    "LW": (-34.6, -58.4), "AY": (-34.6, -58.4), "AZ": (-34.6, -58.4), "L2": (-34.6, -58.4),
    "CE": (-33.5, -70.7), "CA": (-33.5, -70.7), "CB": (-33.5, -70.7), "CC": (-33.5, -70.7),
    "CD": (-33.5, -70.7), "XQ": (-33.5, -70.7), "XR": (-33.5, -70.7), "3G": (-33.5, -70.7),
    "CX": (-34.9, -56.2), "CV": (-34.9, -56.2), "CW": (-34.9, -56.2),
    "CP": (-16.5, -68.1), "OA": (-12.0, -77.0), "OB": (-12.0, -77.0), "OC": (-12.0, -77.0),
    "HC": (-0.2, -78.5), "HD": (-0.2, -78.5), "HK": (4.6, -74.1), "HJ": (4.6, -74.1),
    "YV": (10.5, -66.9), "YW": (10.5, -66.9), "YX": (10.5, -66.9), "YY": (10.5, -66.9),
    "8R": (6.8, -58.2), "PZ": (5.8, -55.2), "FY": (4.9, -52.3),
}

# Rough center of each mainland US call area, keyed by the digit in the
# call - the only prefix system where the digit itself carries geography.
US_CALL_AREA_LATLON: dict[str, tuple[float, float]] = {
    "0": (41.5, -98.0),    # NE IA KS MO MN ND SD CO
    "1": (43.0, -71.5),    # New England
    "2": (40.9, -74.0),    # NY NJ
    "3": (40.2, -76.5),    # PA DE MD DC
    "4": (33.5, -82.0),    # Southeast
    "5": (32.0, -97.0),    # TX OK LA AR MS NM
    "6": (36.5, -119.5),   # CA
    "7": (44.0, -114.0),   # Northwest / Mountain
    "8": (40.5, -82.5),    # MI OH WV
    "9": (41.0, -88.5),    # IL IN WI
}

# Longest first, so "EA8" wins over "EA" and "DU" over "D".
_SKIMMER_PREFIXES_BY_LENGTH = sorted(SKIMMER_PREFIX_LATLON, key=len, reverse=True)


def normalize_skimmer_call(call: str) -> str:
    """Skimmer calls arrive with a node suffix ("DL0AAA-#", "OH6BG-1") that
    identifies the feed, not the station - strip it for geolocation."""
    call = (call or "").strip().upper()
    if "-" in call:
        call = call.split("-", 1)[0]
    return call.strip("#")


@functools.lru_cache(maxsize=8192)
def skimmer_latlon(call: str) -> tuple[float, float] | None:
    """Approximate position of an RBN skimmer from its callsign prefix.
    Returns None for a prefix that isn't in the table, in which case the
    skimmer is simply left out of the scoring rather than guessed at.

    Cached because the scoring re-resolves the same few hundred skimmer
    calls on every table render, and the lookup is a linear scan over the
    prefix table."""
    call = normalize_skimmer_call(call)
    if "/" in call:
        # A skimmer keyed as "DL/W1AW" is physically in the prefix country,
        # so unlike base_callsign_for_lookup() the *prefix* is what matters
        # here - but only when it really looks like a country prefix and not
        # a "/P"-style suffix on a normal call.
        head, tail = call.split("/", 1)
        tail = tail.split("/", 1)[0]
        if 1 <= len(head) <= 3 and not head.isdigit():
            call = head
        elif 1 <= len(tail) <= 3 and not tail.isdigit():
            call = tail
        else:
            call = head
    if not call:
        return None
    for prefix in _SKIMMER_PREFIXES_BY_LENGTH:
        if call.startswith(prefix):
            return SKIMMER_PREFIX_LATLON[prefix]
    # Mainland US (K/N/W/A...): the offshore K/N/W/A entities are already
    # covered by the explicit prefixes above, so anything reaching here is
    # placed by its call-area digit.
    if call[0] in "KNWA":
        for ch in call:
            if ch.isdigit():
                return US_CALL_AREA_LATLON.get(ch)
    return None


@dataclass
class RbnReport:
    skimmer: str
    dx_call: str
    khz: float
    band: str
    snr_db: int
    heard_at: float  # time.monotonic()


def parse_rbn_line(line: str) -> RbnReport | None:
    """One "DX de ..." feed line into an RbnReport, or None for anything
    else on the stream (banners, prompts, keepalives, malformed lines)."""
    match = RBN_LINE_RE.match(line.strip())
    if match is None:
        return None
    try:
        khz = float(match.group("khz"))
        snr = int(match.group("snr"))
    except ValueError:
        return None
    band = band_for_khz(khz)
    if band == "?":
        return None
    dx_call = base_callsign_for_lookup(match.group("dx"))
    if not dx_call:
        return None
    return RbnReport(
        skimmer=normalize_skimmer_call(match.group("skimmer")),
        dx_call=dx_call,
        khz=khz,
        band=band,
        snr_db=snr,
        heard_at=time.monotonic(),
    )


class RbnStore:
    """Recent RBN reports, indexed by (callsign, band) and deduplicated per
    skimmer so a station calling CQ for ten minutes counts once per skimmer
    rather than fifty times. Written from the RBN reader thread, read from
    the Tk main thread, hence the lock on every access."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reports: dict[tuple[str, str], dict[str, RbnReport]] = {}
        self._bucket_touched: dict[tuple[str, str], float] = {}
        # Every skimmer heard from recently, whether or not it reported
        # anything we care about - needed to tell "nobody near me hears this
        # station" (real evidence of a closed path) apart from "no skimmer
        # near me is on the air at all" (no evidence either way).
        self._skimmers_seen: dict[str, float] = {}

    def add(self, report: RbnReport) -> None:
        key = (report.dx_call, report.band)
        with self._lock:
            self._reports.setdefault(key, {})[report.skimmer] = report
            # Both timestamps track the *newest* report, never merely the
            # last one written: prune() drops a whole bucket whose touch
            # time has aged out, so letting an out-of-order (older) report
            # move it backwards would discard the fresh reports next to it.
            # -inf (not 0.0) as the "first time seen" default: time.monotonic()
            # is only guaranteed monotonic, not positive, so 0.0 is not a
            # safe stand-in for "older than anything real".
            self._bucket_touched[key] = max(self._bucket_touched.get(key, float("-inf")), report.heard_at)
            self._skimmers_seen[report.skimmer] = max(
                self._skimmers_seen.get(report.skimmer, float("-inf")), report.heard_at
            )

    def reports_for(self, call: str, band: str) -> list[RbnReport]:
        cutoff = time.monotonic() - RBN_REPORT_TTL_SECONDS
        with self._lock:
            bucket = self._reports.get((call, band))
            if not bucket:
                return []
            return [r for r in bucket.values() if r.heard_at >= cutoff]

    def active_skimmers(self) -> list[str]:
        cutoff = time.monotonic() - RBN_REPORT_TTL_SECONDS
        with self._lock:
            return [s for s, seen in self._skimmers_seen.items() if seen >= cutoff]

    def prune(self) -> None:
        cutoff = time.monotonic() - RBN_REPORT_TTL_SECONDS
        with self._lock:
            for key in [k for k, touched in self._bucket_touched.items() if touched < cutoff]:
                self._reports.pop(key, None)
                self._bucket_touched.pop(key, None)
            for skimmer in [s for s, seen in self._skimmers_seen.items() if seen < cutoff]:
                del self._skimmers_seen[skimmer]
            # Individual stale reports inside buckets that are otherwise
            # still fresh (a skimmer that stopped hearing this station).
            for key, bucket in list(self._reports.items()):
                for skimmer in [s for s, r in bucket.items() if r.heard_at < cutoff]:
                    del bucket[skimmer]
                if not bucket:
                    self._reports.pop(key, None)
                    self._bucket_touched.pop(key, None)
            excess = len(self._reports) - RBN_MAX_TRACKED_CALLS
            if excess > 0:
                oldest = sorted(self._bucket_touched.items(), key=lambda kv: kv[1])[:excess]
                for key, _ in oldest:
                    self._reports.pop(key, None)
                    self._bucket_touched.pop(key, None)

    def stats(self) -> tuple[int, int]:
        """(tracked call/band buckets, individual reports)."""
        with self._lock:
            return len(self._reports), sum(len(b) for b in self._reports.values())


def rbn_snr_score(snr_db: float) -> float:
    """Skimmer SNR (dB) -> 0-100 "could I hear this myself" score.

    Deliberately pessimistic relative to the raw number: a skimmer is a
    quiet, well-sited receiver with a real antenna and a decoder that copies
    signals a human ear can barely find, so its 5 dB is not a comfortable
    QSO. The anchor points below treat ~10 dB as marginal-but-workable and
    only call it a sure thing well above 25 dB."""
    anchors = [(0.0, 5.0), (3.0, 12.0), (10.0, 45.0), (20.0, 78.0), (30.0, 94.0), (45.0, 99.0)]
    if snr_db <= anchors[0][0]:
        return anchors[0][1]
    if snr_db >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= snr_db <= x1:
            return y0 + (y1 - y0) * (snr_db - x0) / (x1 - x0)
    return anchors[-1][1]


@dataclass
class ChanceToHear:
    score: int              # 0-100
    quality: str            # "gut" / "ok" / "schwach"
    estimate: bool          # True when no skimmer near the user could judge
    skimmer_count: int      # reporting skimmers the score is based on
    best_snr: int
    nearest_km: float


def chance_quality(score: int) -> str:
    if score >= 70:
        return "gut"
    if score >= 40:
        return "ok"
    return "schwach"


def chance_to_hear(
    reports: list[RbnReport],
    my_latlon: tuple[float, float],
    regional_skimmers_active: int,
) -> ChanceToHear | None:
    """How likely the user is to hear a station, from what RBN skimmers
    around them are reporting about it right now. None = no usable data.

    Three cases, in descending order of confidence:
      1. Skimmers inside RBN_REGION_RADIUS_KM hear it -> distance-weighted
         SNR of exactly those, the strongest evidence available.
      2. Only distant skimmers hear it, but skimmers near the user *are*
         active -> they'd have heard it if the path were open, so this is
         genuine evidence against, scored low.
      3. Only distant skimmers hear it and there's no active skimmer near
         the user at all -> nothing can be concluded about the local path,
         so the figure is flagged as an estimate (shown with a "~").
    """
    located: list[tuple[RbnReport, float]] = []
    for report in reports:
        latlon = skimmer_latlon(report.skimmer)
        if latlon is None:
            continue
        located.append((report, haversine_km(my_latlon[0], my_latlon[1], latlon[0], latlon[1])))
    if not located:
        return None

    regional = [(r, km) for r, km in located if km <= RBN_REGION_RADIUS_KM]
    if regional:
        weights = [1.0 / (1.0 + (km / RBN_WEIGHT_HALF_KM) ** 2) for _, km in regional]
        weighted_snr = sum(w * r.snr_db for w, (r, _) in zip(weights, regional)) / sum(weights)
        # More independent skimmers agreeing makes the figure more solid; a
        # lone report is deliberately held back a little.
        count_factor = min(1.0, 0.75 + 0.25 * (len(regional) - 1) / 3.0)
        score = rbn_snr_score(weighted_snr) * count_factor
        estimate = False
        pool = regional
    else:
        best_snr_all = max(r.snr_db for r, _ in located)
        if regional_skimmers_active > 0:
            score = min(30.0, rbn_snr_score(best_snr_all) * 0.30)
            estimate = False
        else:
            score = rbn_snr_score(best_snr_all) * 0.60
            estimate = True
        pool = located

    score_int = max(1, min(99, int(round(score))))
    return ChanceToHear(
        score=score_int,
        quality=chance_quality(score_int),
        estimate=estimate,
        skimmer_count=len(pool),
        best_snr=max(r.snr_db for r, _ in pool),
        nearest_km=min(km for _, km in pool),
    )


class RbnClient:
    """Background reader for the RBN telnet feed.

    One supervisor thread owns the whole lifecycle: it idles while the
    feature is off or no callsign is configured, connects and logs in when
    it is, streams reports into the store, and reconnects with exponential
    backoff on any failure. Settings changes are picked up by bumping
    `_generation`, which makes the read loop drop the current connection and
    come back around with the new configuration."""

    def __init__(self, store: RbnStore, log_queue: queue.Queue) -> None:
        self.store = store
        self.log_queue = log_queue
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._callsign = ""
        self._enabled = False
        self._generation = 0
        self._sock: socket.socket | None = None
        self._connected = False
        self._status = "aus"
        self._thread: threading.Thread | None = None
        self._backoff = RBN_RECONNECT_INITIAL_SECONDS

    # -- public API (called from the Tk main thread) ------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def configure(self, enabled: bool, callsign: str) -> None:
        callsign = (callsign or "").strip().upper()
        with self._lock:
            if enabled == self._enabled and callsign == self._callsign:
                return
            self._enabled = enabled
            self._callsign = callsign
            self._generation += 1
        self._drop_socket()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._drop_socket()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def status(self) -> str:
        return self._status

    # -- internals ----------------------------------------------------------

    def _log(self, message: str) -> None:
        self.log_queue.put(f"RBN: {message}")

    def _drop_socket(self) -> None:
        """Unblocks a thread parked in recv() - shutdown() rather than
        close() alone, since closing a socket another thread is reading does
        not reliably wake it up."""
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                enabled, callsign, generation = self._enabled, self._callsign, self._generation
            if not enabled or not callsign:
                self._connected = False
                self._status = "aus" if not enabled else "kein Rufzeichen"
                # configure()/stop() both set the event, so this is only a
                # safety net, not the actual wakeup path.
                self._wake.wait(5.0)
                self._wake.clear()
                continue
            try:
                self._session(callsign, generation)
            except Exception as exc:  # noqa: BLE001 - a reader thread must never die
                self._connected = False
                with self._lock:
                    current_generation = self._generation
                # A drop we caused ourselves (settings change, shutdown) is
                # not a failure and must neither be logged nor backed off.
                if self._stop.is_set() or current_generation != generation:
                    continue
                self._status = "Fehler"
                self._log(f"Verbindung fehlgeschlagen ({exc}), neuer Versuch in {self._backoff:.0f}s.")
                self._wake.wait(self._backoff)
                self._wake.clear()
                self._backoff = min(self._backoff * 2, RBN_RECONNECT_MAX_SECONDS)
            finally:
                self._connected = False
                self._drop_socket()

    def _session(self, callsign: str, generation: int) -> None:
        self._status = "verbinde…"
        sock = socket.create_connection((RBN_HOST, RBN_PORT), timeout=RBN_LOGIN_TIMEOUT)
        with self._lock:
            if self._stop.is_set() or self._generation != generation:
                sock.close()
                return
            self._sock = sock

        buffer = b""
        # The server greets with a banner and asks for a callsign; the
        # prompt has no trailing newline, so this waits for the word rather
        # than for a line. Sending unprompted after the timeout is the
        # documented fallback behaviour of the usual cluster clients.
        deadline = time.monotonic() + RBN_LOGIN_TIMEOUT
        while True:
            if b"call" in buffer.lower():
                break
            if time.monotonic() >= deadline:
                break
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                raise ConnectionError("Verbindung vor dem Login geschlossen")
            buffer += chunk
            # Only the tail can still contain the prompt, and a server that
            # floods without ever asking must not grow this without bound.
            buffer = buffer[-8192:]
        sock.sendall(f"{callsign}\r\n".encode("ascii", "ignore"))
        buffer = b""

        sock.settimeout(RBN_SOCKET_TIMEOUT)
        self._connected = True
        self._status = "verbunden"
        # Reset here rather than on a clean return from _session(): a live
        # session almost always *ends* in an exception (the far end drops
        # it), so keying the reset off that would let the delay ratchet up
        # to the maximum across sessions that were each perfectly healthy.
        self._backoff = RBN_RECONNECT_INITIAL_SECONDS
        self._log(f"verbunden mit {RBN_HOST}:{RBN_PORT} als {callsign}.")

        while not self._stop.is_set():
            with self._lock:
                if self._generation != generation:
                    return
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Gegenstelle hat die Verbindung geschlossen")
            buffer += chunk
            # Guard against a stream that never sends a newline (a stuck
            # server or a binary/telnet-negotiation burst) growing forever.
            if len(buffer) > 1 << 20:
                buffer = b""
                continue
            *lines, buffer = buffer.split(b"\n")
            for raw in lines:
                report = parse_rbn_line(raw.decode("latin-1", "replace"))
                if report is not None:
                    self.store.add(report)


def _patch_tkintermapview_tile_loading() -> None:
    """tkintermapview.TkinterMapView.request_image() fetches each tile with a
    bare requests.get(...) that has no timeout, running on one of 25 daemon
    worker threads. A single stalled connection (slow tile server, flaky
    network) then blocks that worker forever, and a failed tile is cached
    as a permanent blank image with no retry. Over a session, enough stalls
    exhaust the worker pool and some tiles never get (re)loaded, leaving the
    map with unfilled white/grey patches. This replaces request_image() with
    a version that times out and retries transient failures a few times
    before giving up, and - unlike the original - doesn't poison the cache
    on a transient failure, so a later redraw can still fetch it fresh."""
    map_widget_module = tkintermapview.map_widget
    TkinterMapView = map_widget_module.TkinterMapView

    def request_image_with_retry(self, zoom: int, x: int, y: int, db_cursor=None):
        if db_cursor is not None:
            try:
                db_cursor.execute(
                    "SELECT t.tile_image FROM tiles t WHERE t.zoom=? AND t.x=? AND t.y=? AND t.server=?;",
                    (zoom, x, y, self.tile_server),
                )
                result = db_cursor.fetchone()

                if result is not None:
                    image = Image.open(io.BytesIO(result[0]))
                    image_tk = ImageTk.PhotoImage(image)
                    self.tile_image_cache[f"{zoom}{x}{y}"] = image_tk
                    return image_tk
                elif self.use_database_only:
                    return self.empty_tile_image
            except sqlite3.OperationalError:
                if self.use_database_only:
                    return self.empty_tile_image
            except Exception:
                return self.empty_tile_image

        url = self.tile_server.replace("{x}", str(x)).replace("{y}", str(y)).replace("{z}", str(zoom))

        attempts = 3
        for attempt in range(attempts):
            try:
                response = requests.get(url, stream=True, headers={"User-Agent": "TkinterMapView"}, timeout=8)
                image = Image.open(response.raw)

                if self.overlay_tile_server is not None:
                    overlay_url = self.overlay_tile_server.replace("{x}", str(x)).replace("{y}", str(y)).replace("{z}", str(zoom))
                    overlay_response = requests.get(overlay_url, stream=True, headers={"User-Agent": "TkinterMapView"}, timeout=8)
                    image_overlay = Image.open(overlay_response.raw)
                    image = image.convert("RGBA")
                    image_overlay = image_overlay.convert("RGBA")
                    if image_overlay.size != (self.tile_size, self.tile_size):
                        image_overlay = image_overlay.resize((self.tile_size, self.tile_size), Image.LANCZOS)
                    image.paste(image_overlay, (0, 0), image_overlay)

                if not self.running:
                    return self.empty_tile_image

                image_tk = ImageTk.PhotoImage(image)
                self.tile_image_cache[f"{zoom}{x}{y}"] = image_tk
                return image_tk

            except Image.UnidentifiedImageError:
                # no tile exists for these coordinates - retrying won't change that
                self.tile_image_cache[f"{zoom}{x}{y}"] = self.empty_tile_image
                return self.empty_tile_image

            except Exception:
                if attempt < attempts - 1:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                # give up for now, but leave the cache alone so a later
                # redraw (pan/zoom back) gets a fresh attempt instead of a
                # permanently blank tile
                return self.empty_tile_image

        return self.empty_tile_image

    TkinterMapView.request_image = request_image_with_retry


def _make_map_dot_icon(color: str, diameter: int) -> ImageTk.PhotoImage:
    """A small filled-circle marker icon - tkintermapview's built-in marker
    shape (a big teardrop pin) has no size option at all, hardcoded pixel
    dimensions in its drawing code, so a plain custom icon is the only way
    to get compact, precisely-centered dots instead."""
    scale = 4  # draw oversized and downscale for antialiased edges
    img = Image.new("RGBA", (diameter * scale, diameter * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((scale, scale, diameter * scale - scale, diameter * scale - scale),
                 fill=color, outline="#111111", width=scale)
    img = img.resize((diameter, diameter), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


class QrzXmlError(Exception):
    pass


class QrzXmlAuthError(QrzXmlError):
    pass


@dataclass
class QrzCallsignInfo:
    latlon: tuple[float, float] | None
    op_name: str | None


# Portable/mobile-style suffixes that never form part of an operator's
# actual registered callsign - stripped in base_callsign_for_lookup() below.
_CALLSIGN_MODIFIER_SUFFIXES = {"P", "M", "MM", "AM", "QRP", "A", "B", "R", "LH"}


def base_callsign_for_lookup(call: str) -> str:
    """QRZ's XML lookup is keyed on an operator's actual registered
    callsign, so a compound call like "F4MPJ/P" (portable) or "DL/W1AW"
    (operating from another country) never matches anything by itself -
    strip the modifier/prefix part so the lookup can find the operator.
    Heuristic: drop parts that are pure call-area digits (W1AW/4) or a
    known portable/mobile suffix (/P, /M, /QRP, ...), then, if a prefix
    override remains (DL/W1AW), keep the longest remaining part - a real
    callsign always has a digit plus letters on both sides of it, so it's
    reliably longer than a bare 1-3 letter country prefix. Not resolved
    against a full ITU prefix table, so this can still guess wrong for
    unusually short vintage-style home calls."""
    call = (call or "").strip().upper()
    if "/" not in call:
        return call
    parts = [p for p in call.split("/") if p]
    candidates = [p for p in parts if p not in _CALLSIGN_MODIFIER_SUFFIXES and not p.isdigit()]
    if not candidates:
        candidates = parts
    return max(candidates, key=len) if candidates else call


class QrzXmlClient:
    """Thin client for QRZ.com's XML lookup API (a separate subscription
    and separate username/password login from the QRZ Logbook API used
    for ADIF uploads) - resolves a callsign to the lat/lon and operator
    name from the operator's QRZ profile, used for the spot table's
    distance and OP columns."""

    def __init__(self) -> None:
        self.session_key: str | None = None
        # Guards only session_key reads/writes during (re-)authentication -
        # deliberately NOT held around the actual per-callsign lookup
        # request, so many lookups can run concurrently once a session
        # exists instead of queuing up behind one another one at a time.
        self._auth_lock = threading.Lock()

    @staticmethod
    def _request(params: dict) -> ET.Element:
        resp = requests.get(QRZ_XML_URL, params=params, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        for elem in root.iter():
            if "}" in elem.tag:
                elem.tag = elem.tag.split("}", 1)[1]
        return root

    def _authenticate(self, username: str, password: str) -> None:
        root = self._request({"username": username, "password": password})
        error = root.findtext("./Session/Error")
        if error:
            self.session_key = None
            raise QrzXmlAuthError(error)
        key = root.findtext("./Session/Key")
        if not key:
            self.session_key = None
            raise QrzXmlAuthError("QRZ-Login: keine Session erhalten.")
        self.session_key = key

    def _ensure_session(self, username: str, password: str) -> str:
        with self._auth_lock:
            if not self.session_key:
                self._authenticate(username, password)
            return self.session_key

    def lookup_callsign(self, username: str, password: str, callsign: str) -> QrzCallsignInfo | None:
        session_key = self._ensure_session(username, password)
        root = self._request({"s": session_key, "callsign": callsign})
        error = root.findtext("./Session/Error")
        if error and "session" in error.lower():
            with self._auth_lock:
                self._authenticate(username, password)
                session_key = self.session_key
            root = self._request({"s": session_key, "callsign": callsign})
            error = root.findtext("./Session/Error")
        if error:
            if "not found" in error.lower():
                return None
            raise QrzXmlError(error)

        lat = root.findtext("./Callsign/lat")
        lon = root.findtext("./Callsign/lon")
        latlon = None
        if lat is not None and lon is not None:
            try:
                latlon = (float(lat), float(lon))
            except ValueError:
                latlon = None

        # QRZ splits first/last name into separate fields; either can be
        # missing on a sparsely filled-out profile.
        fname = (root.findtext("./Callsign/fname") or "").strip()
        lname = (root.findtext("./Callsign/name") or "").strip()
        op_name = " ".join(part for part in (fname, lname) if part) or None

        if latlon is None and op_name is None:
            return None
        return QrzCallsignInfo(latlon=latlon, op_name=op_name)


def format_spot_time(iso_time: str) -> str:
    try:
        dt = datetime.strptime(iso_time, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.strftime("%H:%M")
    except ValueError:
        return iso_time


def spot_age_seconds(iso_time: str) -> int:
    """Seconds since the spot was posted; a large sentinel for spots whose
    time couldn't be parsed, so they sort/filter as the oldest rather than
    crashing or looking freshest."""
    try:
        dt = datetime.strptime(iso_time, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 10**9
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))


def format_age(iso_time: str) -> str:
    seconds = spot_age_seconds(iso_time)
    if seconds >= 10**9:
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


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
MODE_FILTER_OPTIONS = ["Alle Modes", "CW", "SSB", "CW & SSB", "Digital"]
AGE_FILTER_OPTIONS = ["Alle", "5 min", "10 min", "15 min"]
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
COL_WORKED_BG = "#0a1a0a"
COL_QSY_BG = "#1b4f72"
COL_HEAR_CIRCLE = "#e08a3c"


def apply_dark_titlebar(window) -> bool:
    """Windows 10 (2004+)/11 only: switches the *native* window title bar
    to dark mode via DWM. Tkinter has no theme hook for the OS-drawn
    title bar - it stays white/light regardless of the app's own colors
    unless a program explicitly opts in through this Win32 API. No-op
    (silently) on anything else, including older Windows without this
    DWM attribute. Returns True if DWM confirmed the change, False if it
    was rejected (older Windows) or this isn't Windows at all - the caller
    can use that to tell the difference between "worked" and "unsupported"
    instead of assuming success."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        # The real top-level HWND that Windows/DWM draws the title bar for
        # only exists once Tk has actually realized the window - calling
        # this immediately after creating the widget (before it's mapped)
        # gets HWND 0 and DWM silently ignores the call, leaving the title
        # bar white. update() forces full realization first.
        window.update()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1)
        # 20 = official DWMWA_USE_IMMERSIVE_DARK_MODE (Windows 10 20H1+/11).
        # 19 = same attribute on Windows 10 1809-1909 insider builds that
        # shipped it under a different, later-renumbered ID. Neither call
        # raises on failure (DwmSetWindowAttribute just returns a non-zero
        # HRESULT), so the return value - not an exception - is what tells
        # us whether it actually took effect.
        for attr in (20, 19):
            hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value),
            )
            if hr == 0:
                return True
        return False
    except (OSError, AttributeError):
        return False


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
        # Taller than before the map/stats panel was added - that content
        # has its own real minimum height (see list_pane/map_pane
        # pack_propagate(False) below), and at the old 680/560 heights it
        # silently pushed the TUNE bar and status bar completely off the
        # bottom of the window instead of just cramping the spot list.
        self.geometry("1320x820")
        self.minsize(1180, 700)
        self.configure(bg=COL_BG)
        self._dark_titlebar_ok = apply_dark_titlebar(self)
        # Some Windows builds only pick up the DWM attribute once the window
        # is actually mapped on screen - a second call shortly after start
        # (harmless no-op if the first one already worked) catches those.
        self.after(150, self._retry_dark_titlebar)

        self._init_style()

        self.cat = Ft710Cat()
        self.tune = TuneController(self.cat)
        self.spots: list[Spot] = []
        self.skipped_ids: set[int] = set()
        self.logged_spot_ids: set[int] = set()
        # Last spot QSY'd to - kept highlighted across refreshes/re-sorts
        # until a different spot is QSY'd to, so it doesn't get lost when
        # the list re-sorts it further down.
        self.qsy_spot_id: int | None = None
        self.worked_today_index: dict[str, list[dict[str, str]]] = {}
        self.auto_refresh = True
        self.priority_alert_enabled = True
        self.known_spot_ids: set[int] | None = None
        self._alert_toasts: list[tk.Toplevel] = []
        self.spot_result_queue: queue.Queue = queue.Queue()
        self.log_result_queue: queue.Queue = queue.Queue()
        self.app_log_lines: list[str] = []
        self.outdoor_result_queue: queue.Queue = queue.Queue()
        self.stop_poll_event = threading.Event()

        config = load_config()
        self.backend_var = tk.StringVar(value=config.get("backend", "ft710"))
        self.backend_display_var = tk.StringVar(
            value=BACKEND_KEY_TO_DISPLAY.get(self.backend_var.get(), BACKEND_KEY_TO_DISPLAY["ft710"])
        )

        self.my_callsign_var = tk.StringVar(value=config.get("my_callsign", ""))
        self.my_grid_var = tk.StringVar(value=config.get("my_gridsquare", ""))
        self.qrz_api_key_var = tk.StringVar(value=config.get("qrz_api_key", ""))
        self.wavelog_url_var = tk.StringVar(value=config.get("wavelog_url", ""))
        self.wavelog_api_key_var = tk.StringVar(value=config.get("wavelog_api_key", ""))
        self.wavelog_station_id_var = tk.StringVar(value=config.get("wavelog_station_profile_id", ""))
        self.qrz_xml_user_var = tk.StringVar(value=config.get("qrz_xml_username", ""))
        self.qrz_xml_pass_var = tk.StringVar(value=config.get("qrz_xml_password", ""))
        self.respot_enabled_var = tk.BooleanVar(value=config.get("respot_enabled", True))
        self.respot_template_var = tk.StringVar(
            value=config.get("respot_template", RESPOT_TEMPLATE_DEFAULT)
        )
        self.qrz_xml_client = QrzXmlClient()
        self.qrz_xml_auth_failed = False
        self.locator_cache: dict[str, QrzCallsignInfo | None] = load_qrz_cache()
        self.locator_lookup_pending: set[str] = set()
        self.locator_result_queue: queue.Queue = queue.Queue()
        self.locator_work_queue: queue.Queue = queue.Queue()

        # Park coordinates: primary source for map pins, the KM column and
        # the QSY line. Comes free with the spot feed for most spots and is
        # resolved per reference for the rest - either way it needs no QRZ
        # subscription, unlike the callsign lookup above.
        self.park_cache: dict[str, tuple[float, float] | None] = load_park_cache()
        self.park_lookup_pending: set[str] = set()
        self.park_result_queue: queue.Queue = queue.Queue()
        self.park_work_queue: queue.Queue = queue.Queue()
        self._park_miss_logged = False

        # Reverse Beacon Network -> "Chance to Hear" column.
        self.rbn_enabled_var = tk.BooleanVar(value=config.get("rbn_enabled", False))
        self.rbn_store = RbnStore()
        self.rbn_client = RbnClient(self.rbn_store, self.log_result_queue)
        self.rbn_badge_var = tk.StringVar(value="RBN aus")
        self._chance_cache: dict[int, ChanceToHear | None] = {}
        self._my_latlon: tuple[float, float] | None = None
        self._regional_skimmers_active = 0
        self._rbn_next_badge_at = 0.0
        self._rbn_next_prune_at = time.monotonic() + RBN_PRUNE_INTERVAL_SECONDS
        self._rbn_next_render_at = time.monotonic() + RBN_RENDER_REFRESH_SECONDS
        self._rbn_last_rendered_reports = -1
        self.favorite_calls: set[str] = set(config.get("favorite_calls", []))
        self.outdoor_calls: set[str] = set()

        self.port_var = tk.StringVar(value=config.get("cat_port", ""))
        self.baud_var = tk.IntVar(value=config.get("cat_baud", CAT_BAUD_DEFAULT))
        self.host_var = tk.StringVar(value=config.get("rigctld_host", RIGCTLD_HOST_DEFAULT))
        self.rig_port_var = tk.IntVar(value=config.get("rigctld_port", RIGCTLD_PORT_DEFAULT))
        self.rigctld_process = RigctldProcess()
        self.connect_result_queue: queue.Queue = queue.Queue()
        self._manual_disconnect = False
        self.reconnect_backoff_seconds = RECONNECT_BACKOFF_INITIAL_SECONDS
        self._next_reconnect_attempt_at = 0.0
        self.rig_models: list[tuple[int, str, str]] = []
        self.rig_model_displays: list[str] = []
        self.rig_model_display_var = tk.StringVar(value=config.get("rig_model_display", ""))
        self.rigctld_path_var = tk.StringVar(value=config.get("rigctld_path", ""))
        self.tune_power_var = tk.DoubleVar(value=float(TUNE_POWER_WATTS_DEFAULT))
        self.power_unit_var = tk.StringVar(value="Leistung (W)")
        # Operating power restored after TUNE releases (see TuneController) -
        # unlike tune_power_var these are real settings, persisted like the
        # rest of config. Default depends on backend since rigctld's RFPOWER
        # is a 0.0-1.0 fraction, not absolute watts.
        rigctld_at_startup = self.backend_var.get() == "rigctld"
        self.ssb_power_var = tk.DoubleVar(value=config.get(
            "ssb_power",
            RIGCTLD_SSB_LEVEL_DEFAULT if rigctld_at_startup else float(SSB_POWER_WATTS_DEFAULT),
        ))
        self.cw_power_var = tk.DoubleVar(value=config.get(
            "cw_power",
            RIGCTLD_CW_LEVEL_DEFAULT if rigctld_at_startup else float(CW_POWER_WATTS_DEFAULT),
        ))
        self.offset_var = tk.IntVar(value=TUNE_OFFSET_HZ_DEFAULT)
        self.offset_sign_var = tk.StringVar(value="above")
        saved_band = config.get("band_filter", BAND_FILTER_OPTIONS[0])
        saved_mode = config.get("mode_filter", MODE_FILTER_OPTIONS[0])
        saved_age = config.get("age_filter", AGE_FILTER_OPTIONS[0])
        self.band_filter_var = tk.StringVar(
            value=saved_band if saved_band in BAND_FILTER_OPTIONS else BAND_FILTER_OPTIONS[0])
        self.mode_filter_var = tk.StringVar(
            value=saved_mode if saved_mode in MODE_FILTER_OPTIONS else MODE_FILTER_OPTIONS[0])
        self.age_filter_var = tk.StringVar(
            value=saved_age if saved_age in AGE_FILTER_OPTIONS else AGE_FILTER_OPTIONS[0])
        self.sort_column: str | None = None
        self.sort_reverse: bool = False
        self.selected_countries: set[str] = set(config.get("hunt_countries", []))
        self.known_countries: set[str] = set(self.selected_countries)
        self.country_filter_label_var = tk.StringVar(value=self._country_filter_label())
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._render_spots())
        self.clock_var = tk.StringVar(value="--:--:--z")
        self.cat_badge_var = tk.StringVar(value="CAT")
        self.count_badge_var = tk.StringVar(value="0 Spots")
        self.solar_data_var = tk.StringVar(value="SFI -- · K -- · A -- · MUF -- MHz")
        self.solar_result_queue: queue.Queue = queue.Queue()
        self._solar_diag_logged = False
        self.status_var = tk.StringVar(value="Bereit.")
        self.conn_status_var = tk.StringVar(value="Nicht verbunden")

        self._build_ui()
        self._refresh_ports()
        self._tick_cat_health()
        self._start_poll_thread()
        threading.Thread(target=self._load_outdoor_calls_async, daemon=True).start()
        for _ in range(LOCATOR_WORKER_COUNT):
            threading.Thread(target=self._locator_worker, daemon=True).start()
        for _ in range(PARK_WORKER_COUNT):
            threading.Thread(target=self._park_worker, daemon=True).start()
        self.rbn_client.start()
        self._apply_rbn_settings()
        self._tick_solar_data()
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
        # Default 'clam' sash is a thin near-invisible line on a dark
        # background - widen it and give it a visible color so the
        # list/map divider actually reads as something draggable.
        style.configure("TPanedwindow", background=COL_BG)
        style.configure("Sash", sashthickness=8, gripcount=8, sashrelief="raised")

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
        # Clickable: the RBN feature is off by default (it opens a permanent
        # connection to an outside service and logs in with the user's
        # callsign, so it shouldn't just start on its own), and this badge
        # is the one-click way to turn it on without going into Settings.
        self.rbn_badge = _badge(title_row, textvariable=self.rbn_badge_var, fg=COL_MUTED, bg=COL_PANEL_ALT)
        self.rbn_badge.pack(side="right", padx=6)
        self.rbn_badge.configure(cursor="hand2")
        self.rbn_badge.bind("<Button-1>", lambda _e: self._toggle_rbn())
        _badge(title_row, textvariable=self.count_badge_var, fg=COL_TEXT, bg=COL_PANEL_ALT).pack(side="right", padx=6)
        _badge(
            title_row, textvariable=self.solar_data_var, fg=COL_AMBER, bg=COL_PANEL_ALT,
        ).pack(side="right", padx=6)

        filter_row = tk.Frame(header, bg=COL_BG)
        filter_row.pack(fill="x", pady=(8, 0))

        tk.Label(filter_row, text="Band", fg=COL_MUTED, bg=COL_BG,
                 font=("Segoe UI", 8)).pack(side="left", padx=(0, 4))
        band_combo = ttk.Combobox(filter_row, textvariable=self.band_filter_var, style="Dark.TCombobox",
                                   values=BAND_FILTER_OPTIONS, width=10, state="readonly")
        band_combo.pack(side="left", padx=(0, 10))
        band_combo.bind("<<ComboboxSelected>>", lambda *_: self._on_filter_setting_changed())

        tk.Label(filter_row, text="Mode", fg=COL_MUTED, bg=COL_BG,
                 font=("Segoe UI", 8)).pack(side="left", padx=(0, 4))
        mode_combo = ttk.Combobox(filter_row, textvariable=self.mode_filter_var, style="Dark.TCombobox",
                                   values=MODE_FILTER_OPTIONS, width=10, state="readonly")
        mode_combo.pack(side="left", padx=(0, 10))
        mode_combo.bind("<<ComboboxSelected>>", lambda *_: self._on_filter_setting_changed())

        tk.Label(filter_row, text="Alter", fg=COL_MUTED, bg=COL_BG,
                 font=("Segoe UI", 8)).pack(side="left", padx=(0, 4))
        age_combo = ttk.Combobox(filter_row, textvariable=self.age_filter_var, style="Dark.TCombobox",
                                  values=AGE_FILTER_OPTIONS, width=8, state="readonly")
        age_combo.pack(side="left", padx=(0, 10))
        age_combo.bind("<<ComboboxSelected>>", lambda *_: self._on_filter_setting_changed())

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
        self.alert_btn = _chip_button(filter_row, "Alarm: An", command=self._toggle_priority_alert)
        self.alert_btn.pack(side="left", padx=4)
        _chip_button(filter_row, "Alarm testen", command=self._test_priority_alert).pack(side="left", padx=4)
        _chip_button(filter_row, "Settings", command=self._toggle_settings_panel).pack(side="left", padx=4)
        _chip_button(filter_row, "Alle anzeigen", command=self._unskip_all).pack(side="left", padx=4)
        _chip_button(filter_row, "CAT-Log", command=self._open_cat_log).pack(side="left", padx=4)
        _chip_button(filter_row, "Programm-Log", command=self._open_program_log).pack(side="left", padx=4)

        # -- table -----------------------------------------------------------
        table_frame = tk.Frame(self, bg=COL_BG)
        table_frame.pack(fill="both", expand=True, padx=12, pady=6)

        # Settings used to be a separate Toplevel dialog - now it's a
        # collapsible panel docked to the right of the spot table, built
        # once here and just shown/hidden by _toggle_settings_panel(), so
        # it never steals focus into its own window.
        self._settings_visible = False
        self.settings_panel = tk.Frame(table_frame, bg=COL_PANEL, width=460)
        self.settings_panel.pack_propagate(False)
        self._build_settings_panel(self.settings_panel)

        self.tree_area = tk.Frame(table_frame, bg=COL_BG)
        self.tree_area.pack(side="left", fill="both", expand=True)

        # Spot list on top, world map below - a vertical PanedWindow instead
        # of a fixed split so the map doesn't permanently eat into the
        # list's space on smaller windows; drag the sash to resize.
        vertical_split = ttk.PanedWindow(self.tree_area, orient="vertical")
        vertical_split.pack(fill="both", expand=True)

        # pack_propagate(False) + a fixed starting height on both panes is
        # load-bearing, not cosmetic: without it, ttk.PanedWindow reports
        # its *children's own natural content size* (18-row spot list +
        # map/stats) as its requested size, which can exceed the actual
        # window height - Tk then has nowhere to shrink that to and simply
        # overflows the window, silently pushing the TUNE bar/status bar
        # below the visible area instead of just cramping this split. The
        # user can still freely resize both panes afterward by dragging
        # the sash; this only fixes the *starting* sizes.
        list_pane = tk.Frame(vertical_split, bg=COL_BG, height=320)
        list_pane.pack_propagate(False)
        map_pane = tk.Frame(vertical_split, bg=COL_BG, height=220)
        map_pane.pack_propagate(False)
        vertical_split.add(list_pane, weight=3)
        vertical_split.add(map_pane, weight=1)

        columns = (
            "fav", "outdoor", "qsy", "call", "op", "worked", "freq", "mode", "ref", "name", "loc",
            "dist", "chance", "age", "skip", "log",
        )
        headers = {
            "fav": "", "outdoor": "", "qsy": "", "call": "CALLSIGN", "op": "OP", "worked": "HEUTE",
            "freq": "FREQ (KHZ)", "mode": "MODE", "ref": "REF", "name": "NAME", "loc": "LOC", "dist": "KM",
            "chance": "HÖRCHANCE", "age": "AGE", "skip": "", "log": "",
        }
        widths = {
            "fav": 30, "outdoor": 30, "qsy": 60, "call": 90, "op": 120, "worked": 220, "freq": 90, "mode": 70,
            "ref": 90, "name": 260, "loc": 70, "dist": 55, "chance": 100, "age": 60, "skip": 60, "log": 70,
        }
        self.column_headers = headers
        self.all_columns = columns
        sortable_columns = {
            "call", "op", "worked", "freq", "mode", "ref", "name", "loc", "dist", "chance", "age",
        }
        self.tree = ttk.Treeview(list_pane, columns=columns, show="headings", height=18)
        for col in columns:
            if col in sortable_columns:
                self.tree.heading(col, text=headers[col], command=lambda c=col: self._sort_by_column(c))
            else:
                self.tree.heading(col, text=headers[col])
            anchor = "center" if col in ("fav", "outdoor", "qsy", "skip", "mode", "age", "loc", "dist") else "w"
            self.tree.column(col, width=widths[col], anchor=anchor)
        self._update_optional_column_visibility()

        vsb = ttk.Scrollbar(list_pane, orient="vertical", command=self.tree.yview,
                             style="Dark.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Map on the left, stats panel on the right - a second, horizontal
        # PanedWindow nested inside map_pane so both are independently
        # resizable (drag the vertical divider to trade space between the
        # two, same as the list/map split above).
        horizontal_split = ttk.PanedWindow(map_pane, orient="horizontal")
        horizontal_split.pack(fill="both", expand=True)
        map_widget_pane = tk.Frame(horizontal_split, bg=COL_BG)
        stats_pane = tk.Frame(horizontal_split, bg=COL_BG, width=320)
        horizontal_split.add(map_widget_pane, weight=3)
        horizontal_split.add(stats_pane, weight=1)

        self._build_map_panel(map_widget_pane)
        self._build_stats_panel(stats_pane)

        # ttk.Treeview resolves conflicting tag options (e.g. two tags on
        # the same row both setting "background") by which tag was
        # tag_configure()'d *first* - NOT by the order tags are listed in
        # a given row's own tags tuple in _render_spots(). "qsy_current"
        # is configured before every other row tag so the currently
        # QSY'd-to spot always stays visually findable, even on a
        # favorite/outdoor row, and survives refreshes/re-sorts that would
        # otherwise move it out of sight with no way to tell which row it
        # was.
        self.tree.tag_configure("qsy_current", background=COL_QSY_BG)
        self.tree.tag_configure("cw_even", background=COL_ROW_EVEN, foreground=COL_ACCENT)
        self.tree.tag_configure("cw_odd", background=COL_ROW_ODD, foreground=COL_ACCENT)
        self.tree.tag_configure("ssb_even", background=COL_ROW_EVEN, foreground=COL_TEXT)
        self.tree.tag_configure("ssb_odd", background=COL_ROW_ODD, foreground=COL_TEXT)
        self.tree.tag_configure("digital_even", background=COL_ROW_EVEN, foreground=COL_AMBER)
        self.tree.tag_configure("digital_odd", background=COL_ROW_ODD, foreground=COL_AMBER)
        self.tree.tag_configure("invalid", background=COL_ROW_EVEN, foreground=COL_RED)
        self.tree.tag_configure("logged", background=COL_WORKED_BG, foreground=COL_ACCENT_DIM)
        # Listed after the mode/invalid/logged tags on favorite/outdoor rows
        # so their background wins while the other tag's foreground (mode
        # color, red for invalid, ...) still shows through - only
        # background+font are set here, on purpose.
        # No bold font override here (unlike "favorite" below): forcing a
        # bold weight makes Tk fall back to a monochrome glyph for the 🏕
        # emoji on Windows instead of the full-color one, since the color
        # emoji font has no bold variant - the background color alone is
        # enough to make outdoor rows stand out.
        self.tree.tag_configure("outdoor", background=COL_OUTDOOR_BG)
        self.tree.tag_configure("favorite", background=COL_FAVORITE_BG, font=("Segoe UI", 10, "bold"))

        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Motion>", self._on_tree_motion)

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

    def _power_for_mode(self, mode: str) -> float:
        """SSB/CW operating power configured in Settings for the given
        mode - used by TuneController to restore power after TUNE releases.
        Digital modes (FT8, RTTY, ...) fall under the SSB value, same as
        everywhere else in the app that only distinguishes cw/non-cw."""
        return self.cw_power_var.get() if mode_category(mode) == "cw" else self.ssb_power_var.get()

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

    def _toggle_settings_panel(self) -> None:
        if self._settings_visible:
            self.settings_panel.pack_forget()
        else:
            # before=self.tree_area matters here: without it, pack() would
            # append this panel after the tree area in the packing order,
            # and since the tree area already claimed the whole cavity as
            # an expand=True slave, the panel would get squeezed down to a
            # sliver instead of its requested width - inserting it before
            # the tree area in the order makes pack size the tree area
            # around the panel's fixed width instead.
            self.settings_panel.pack(side="right", fill="y", padx=(12, 0), before=self.tree_area)
            if not self.rig_model_displays:
                self._refresh_rig_models()
        self._settings_visible = not self._settings_visible

    def _build_settings_panel(self, parent: tk.Frame) -> None:
        header_row = tk.Frame(parent, bg=COL_PANEL)
        header_row.pack(fill="x")
        tk.Label(header_row, text="Settings", fg=COL_ACCENT, bg=COL_PANEL,
                 font=("Segoe UI", 11, "bold")).pack(side="left", padx=14, pady=10)
        _chip_button(header_row, "✕", command=self._toggle_settings_panel).pack(side="right", padx=10, pady=6)

        # Scrollable body - the settings content is taller than the panel
        # allows in most windows, so it lives in a canvas+scrollbar instead
        # of being packed directly into the panel (which just clipped the
        # bottom with no way to reach it).
        canvas = tk.Canvas(parent, bg=COL_PANEL, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        content = tk.Frame(canvas, bg=COL_PANEL)
        content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(content_window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event) -> None:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        def row(parent, label):
            r = tk.Frame(parent, bg=COL_PANEL)
            r.pack(fill="x", padx=14, pady=6)
            tk.Label(r, text=label, fg=COL_MUTED, bg=COL_PANEL, width=14, anchor="w",
                     font=("Segoe UI", 9)).pack(side="left")
            return r

        tk.Label(content, text="Funkgerät", fg=COL_ACCENT, bg=COL_PANEL,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(10, 0))

        backend_row = row(content, "Backend")
        backend_combo = ttk.Combobox(
            backend_row, textvariable=self.backend_display_var, style="Dark.TCombobox",
            values=list(BACKEND_DISPLAY_TO_KEY.keys()), width=22, state="readonly",
        )
        backend_combo.pack(side="left")

        param_frame = tk.Frame(content, bg=COL_PANEL)
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
        # Exposed so _toggle_settings_panel() can lazily trigger the first
        # 'rigctld -l' model scan only once the panel is actually opened,
        # not at app startup (that subprocess call can be slow, or log a
        # "rigctld not found" line that's noise for pure FT-710 users).
        self._refresh_rig_models = refresh_models

        rigctld_path_row = row(rigctld_frame, "rigctld-Pfad")
        rigctld_path_entry = ttk.Entry(
            rigctld_path_row, textvariable=self.rigctld_path_var, style="Dark.TEntry", width=22,
        )
        rigctld_path_entry.pack(side="left")
        rigctld_path_entry.bind("<FocusOut>", lambda _e: self._save_rigctld_path())

        def browse_rigctld() -> None:
            filetypes = [("rigctld.exe", "rigctld.exe"), ("Alle Dateien", "*.*")]
            path = filedialog.askopenfilename(title="rigctld auswählen", filetypes=filetypes)
            if path:
                self.rigctld_path_var.set(path)
                self._save_rigctld_path()
                refresh_models()

        _chip_button(rigctld_path_row, "Durchsuchen…", command=browse_rigctld).pack(side="left", padx=4)

        tk.Label(
            rigctld_frame,
            text="Nur nötig, falls rigctld nicht automatisch gefunden wird (z. B.\n"
                 "Hamlib als .zip entpackt statt über einen Installer eingerichtet) -\n"
                 "auf rigctld.exe zeigen, leer lassen für automatische Suche.",
            fg=COL_MUTED, bg=COL_PANEL, font=("Segoe UI", 8), justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 4))

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

        tk.Label(
            content, text="Betriebsleistung (nach TUNE)", fg=COL_MUTED, bg=COL_PANEL,
            font=("Segoe UI", 8, "italic"),
        ).pack(anchor="w", padx=14, pady=(6, 0))

        ssb_power_row = row(content, "SSB-Leistung")
        ttk.Entry(ssb_power_row, textvariable=self.ssb_power_var, style="Dark.TEntry", width=8).pack(side="left")
        tk.Label(ssb_power_row, textvariable=self.power_unit_var, fg=COL_MUTED, bg=COL_PANEL,
                 font=("Segoe UI", 8)).pack(side="left", padx=(6, 0))

        cw_power_row = row(content, "CW-Leistung")
        ttk.Entry(cw_power_row, textvariable=self.cw_power_var, style="Dark.TEntry", width=8).pack(side="left")
        tk.Label(cw_power_row, textvariable=self.power_unit_var, fg=COL_MUTED, bg=COL_PANEL,
                 font=("Segoe UI", 8)).pack(side="left", padx=(6, 0))

        tk.Label(
            content, text="Wird nach jedem Loslassen von TUNE automatisch gesetzt (je\n"
                      "nach Mode SSB oder CW), statt zu versuchen die vorherige\n"
                      "Leistung vom Funkgerät zurückzulesen (nicht jedes Rig-Backend\n"
                      "unterstützt das zuverlässig).",
            fg=COL_MUTED, bg=COL_PANEL, font=("Segoe UI", 7), justify="left", anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 8))

        self.connect_btn = _chip_button(
            content, "Trennen" if self.cat.connected else "Verbinden", command=self._toggle_connect,
        )
        self.connect_btn.pack(padx=14, pady=(10, 4), fill="x")

        tk.Label(content, textvariable=self.conn_status_var, fg=COL_MUTED, bg=COL_PANEL,
                 font=("Segoe UI", 8)).pack(padx=14, pady=(0, 10))

        ttk.Separator(content, orient="horizontal").pack(fill="x", padx=14, pady=(0, 6))

        tk.Label(content, text="Log / QRZ Logbook", fg=COL_ACCENT, bg=COL_PANEL,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(0, 0))

        call_row = row(content, "Eig. Rufzeichen")
        ttk.Entry(call_row, textvariable=self.my_callsign_var, style="Dark.TEntry", width=16).pack(side="left")

        grid_row = row(content, "Eig. Locator")
        ttk.Entry(grid_row, textvariable=self.my_grid_var, style="Dark.TEntry", width=16).pack(side="left")

        qrz_row = row(content, "QRZ API-Key")
        ttk.Entry(qrz_row, textvariable=self.qrz_api_key_var, style="Dark.TEntry", width=28, show="•").pack(side="left")

        tk.Label(
            content, text="QRZ-Logbook-API-Key aus dem QRZ-Logbook (Settings -> API Key).\n"
                      "Leer lassen, um nicht zu QRZ hochzuladen.",
            fg=COL_MUTED, bg=COL_PANEL, font=("Segoe UI", 7), justify="left", anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 8))

        ttk.Separator(content, orient="horizontal").pack(fill="x", padx=14, pady=(0, 6))

        tk.Label(content, text="Wavelog", fg=COL_ACCENT, bg=COL_PANEL,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(0, 0))

        wavelog_url_row = row(content, "Server-URL")
        ttk.Entry(
            wavelog_url_row, textvariable=self.wavelog_url_var, style="Dark.TEntry", width=28,
        ).pack(side="left")

        wavelog_key_row = row(content, "API-Key")
        ttk.Entry(
            wavelog_key_row, textvariable=self.wavelog_api_key_var, style="Dark.TEntry", width=28, show="•",
        ).pack(side="left")

        wavelog_station_row = row(content, "Station-Profil-ID")
        ttk.Entry(
            wavelog_station_row, textvariable=self.wavelog_station_id_var, style="Dark.TEntry", width=10,
        ).pack(side="left")

        tk.Label(
            content, text="Eigene Wavelog-Instanz (z. B. https://log.example.com, ohne\n"
                      "abschließenden Slash) - API-Key unter Settings -> API Keys im\n"
                      "Wavelog erzeugen. Station-Profil-ID steht in Wavelog unter Station\n"
                      "Setup (meist \"1\" bei nur einem Profil). Alle drei Felder nötig, sonst\n"
                      "wird nicht zu Wavelog hochgeladen.",
            fg=COL_MUTED, bg=COL_PANEL, font=("Segoe UI", 7), justify="left", anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 8))

        ttk.Separator(content, orient="horizontal").pack(fill="x", padx=14, pady=(0, 6))

        tk.Label(content, text="QRZ XML-Lookup (Name des Aktivators)", fg=COL_ACCENT, bg=COL_PANEL,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(0, 0))

        qrz_xml_user_row = row(content, "QRZ-Benutzer")
        ttk.Entry(qrz_xml_user_row, textvariable=self.qrz_xml_user_var, style="Dark.TEntry", width=20).pack(side="left")

        qrz_xml_pass_row = row(content, "QRZ-Passwort")
        ttk.Entry(
            qrz_xml_pass_row, textvariable=self.qrz_xml_pass_var, style="Dark.TEntry", width=20, show="•",
        ).pack(side="left")

        tk.Label(
            content, text="Eigener QRZ.com-Login (nicht der Logbook-API-Key oben) - nötig für\n"
                      "die kostenpflichtige XML-Lookup-Funktion. Mit beiden Feldern hier\n"
                      "ausgefüllt erscheint die OP-Spalte (Name des Aktivators) in der\n"
                      "Spot-Liste. Leer lassen = Spalte bleibt aus, keine Abfragen.\n"
                      "Für Karte und KM-Spalte wird das nicht mehr gebraucht: die kommen\n"
                      "aus den Koordinaten der Park-Referenz und damit von POTA selbst.\n"
                      "QRZ dient dort nur noch als Notnagel für die seltenen Parks ohne\n"
                      "hinterlegte Position.",
            fg=COL_MUTED, bg=COL_PANEL, font=("Segoe UI", 7), justify="left", anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 8))

        ttk.Separator(content, orient="horizontal").pack(fill="x", padx=14, pady=(0, 6))

        tk.Label(content, text="Hörchance (Reverse Beacon Network)", fg=COL_ACCENT, bg=COL_PANEL,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(0, 0))

        tk.Checkbutton(
            content, text="RBN-Daten abrufen und Spalte HÖRCHANCE anzeigen",
            variable=self.rbn_enabled_var, command=self._on_rbn_toggle,
            fg=COL_TEXT, bg=COL_PANEL, selectcolor=COL_PANEL_ALT,
            activebackground=COL_PANEL, activeforeground=COL_TEXT,
            font=("Segoe UI", 9), anchor="w",
        ).pack(fill="x", padx=14, pady=(4, 4))

        tk.Label(
            content, text="Verbindet sich mit telnet.reversebeacon.net und wertet aus, wie\n"
                      "stark die Skimmer-Empfänger in deiner Umgebung einen Aktivator\n"
                      "gerade hören - daraus wird eine Prozentzahl für deine eigene\n"
                      "Hörchance geschätzt. Kostenlos und ohne Anmeldung, es werden nur\n"
                      "'Eig. Rufzeichen' (als Login) und 'Eig. Locator' (für die Entfernung\n"
                      "zu den Skimmern) von oben gebraucht. Funktioniert nur für CW und\n"
                      "RTTY - dort schaut kein Skimmer hin, steht in der Spalte '–'.\n"
                      "'~' vor dem Wert = Schätzung, weil gerade kein Skimmer in deiner\n"
                      "Nähe aktiv ist. Per QSY zeigt die Karte zusätzlich einen Ring um\n"
                      "den Park: wie weit der Aktivator laut RBN gerade maximal gehört\n"
                      "wird.",
            fg=COL_MUTED, bg=COL_PANEL, font=("Segoe UI", 7), justify="left", anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 8))

        ttk.Separator(content, orient="horizontal").pack(fill="x", padx=14, pady=(0, 6))

        tk.Label(content, text="Respot nach dem Loggen", fg=COL_ACCENT, bg=COL_PANEL,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(0, 0))

        tk.Checkbutton(
            content, text="Aktivator nach jedem geloggten QSO automatisch respotten",
            variable=self.respot_enabled_var,
            fg=COL_TEXT, bg=COL_PANEL, selectcolor=COL_PANEL_ALT,
            activebackground=COL_PANEL, activeforeground=COL_TEXT,
            font=("Segoe UI", 9), anchor="w",
        ).pack(fill="x", padx=14, pady=(4, 4))

        respot_template_row = row(content, "Kommentar-Vorlage")
        ttk.Entry(
            respot_template_row, textvariable=self.respot_template_var, style="Dark.TEntry", width=34,
        ).pack(side="left")

        tk.Label(
            content, text="Sendet den geloggten Kontakt als neuen Spot an api.pota.app,\n"
                      "damit andere Jäger sehen, dass der Aktivator noch aktiv ist.\n"
                      "Platzhalter für die Vorlage: {call} {mycall} {rst_sent} {rst_rcvd}\n"
                      "{freq} {mode} {ref} - unbekannte Platzhalter bleiben unverändert stehen.",
            fg=COL_MUTED, bg=COL_PANEL, font=("Segoe UI", 7), justify="left", anchor="w",
        ).pack(fill="x", padx=14, pady=(0, 8))

        _chip_button(content, "Speichern", command=self._save_log_settings).pack(padx=14, pady=(0, 10), fill="x")

    def _save_log_settings(self) -> None:
        config = load_config()
        config.update({
            "my_callsign": self.my_callsign_var.get().strip().upper(),
            "my_gridsquare": self.my_grid_var.get().strip().upper(),
            "qrz_api_key": self.qrz_api_key_var.get().strip(),
            "wavelog_url": self.wavelog_url_var.get().strip(),
            "wavelog_api_key": self.wavelog_api_key_var.get().strip(),
            "wavelog_station_profile_id": self.wavelog_station_id_var.get().strip(),
            "qrz_xml_username": self.qrz_xml_user_var.get().strip(),
            "qrz_xml_password": self.qrz_xml_pass_var.get().strip(),
            "respot_enabled": bool(self.respot_enabled_var.get()),
            "respot_template": self.respot_template_var.get().strip() or RESPOT_TEMPLATE_DEFAULT,
            "ssb_power": self.ssb_power_var.get(),
            "cw_power": self.cw_power_var.get(),
            "rbn_enabled": bool(self.rbn_enabled_var.get()),
        })
        save_config(config)
        self.qrz_xml_auth_failed = False
        self.qrz_xml_client.session_key = None
        self._update_optional_column_visibility()
        self._update_own_qth_marker()
        self._update_map_hint()
        # The callsign doubles as the RBN login, so a changed one has to
        # reach the client here too (a no-op when nothing relevant changed).
        self._apply_rbn_settings()
        self._render_spots()
        self._log("Log-/QRZ-Einstellungen gespeichert.")

    def _on_rbn_toggle(self) -> None:
        enabled = bool(self.rbn_enabled_var.get())
        config = load_config()
        config["rbn_enabled"] = enabled
        save_config(config)
        self._apply_rbn_settings()
        self._render_spots()
        if enabled and not self.my_callsign_var.get().strip():
            self._log("RBN: 'Eig. Rufzeichen' in den Settings eintragen - es dient als Login.")
        if enabled and grid_to_latlon(self.my_grid_var.get()) is None:
            self._log("RBN: ohne 'Eig. Locator' kann keine Hörchance berechnet werden.")

    def _toggle_rbn(self) -> None:
        self.rbn_enabled_var.set(not self.rbn_enabled_var.get())
        self._on_rbn_toggle()

    # -- rigctld model list / auto-launch --------------------------------------

    def _load_rig_models(self) -> list[str]:
        exe = find_rigctld_executable(self.rigctld_path_var.get())
        if not exe:
            self._log(
                "rigctld nicht gefunden - Hamlib installieren (hamlib.github.io), zum PATH "
                "hinzufügen oder den rigctld-Pfad oben manuell eintragen."
            )
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

    def _save_rigctld_path(self) -> None:
        config = load_config()
        config["rigctld_path"] = self.rigctld_path_var.get().strip()
        save_config(config)

    # -- CAT trace dialog -----------------------------------------------------

    def _open_cat_log(self) -> None:
        dlg = tk.Toplevel(self, bg=COL_PANEL)
        dlg.title("CAT-Log")
        dlg.geometry("640x420")
        dlg.configure(bg=COL_PANEL)
        apply_dark_titlebar(dlg)
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

    def _open_program_log(self) -> None:
        dlg = tk.Toplevel(self, bg=COL_PANEL)
        dlg.title("Programm-Log")
        dlg.geometry("720x460")
        dlg.configure(bg=COL_PANEL)
        apply_dark_titlebar(dlg)
        dlg.transient(self)

        tk.Label(
            dlg, text="Programmereignisse und Fehler (Verbindung, Spots, Uploads, "
            "interne Fehler) - neueste Zeile unten.",
            fg=COL_MUTED, bg=COL_PANEL, font=("Segoe UI", 8), justify="left", wraplength=690,
        ).pack(anchor="w", padx=10, pady=(10, 4))

        text = tk.Text(
            dlg, bg=COL_PANEL_ALT, fg=COL_TEXT, insertbackground=COL_TEXT,
            font=("Consolas", 9), wrap="word", relief="flat", bd=0,
        )
        text.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        def refresh() -> None:
            text.configure(state="normal")
            text.delete("1.0", "end")
            if not self.app_log_lines:
                text.insert("end", "Noch keine Ereignisse aufgezeichnet.")
            else:
                text.insert("end", "\n".join(self.app_log_lines))
                text.see("end")
            text.configure(state="disabled")

        btn_row = tk.Frame(dlg, bg=COL_PANEL)
        btn_row.pack(fill="x", padx=10, pady=(0, 10))
        _chip_button(btn_row, "Aktualisieren", command=refresh).pack(side="left")
        _chip_button(
            btn_row, "Leeren",
            command=lambda: (self.app_log_lines.clear(), refresh()),
        ).pack(side="left", padx=6)

        refresh()

    # -- logging / status ----------------------------------------------------

    def _log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        self.status_var.set(line)
        self.app_log_lines.append(line)
        if len(self.app_log_lines) > 500:
            del self.app_log_lines[: len(self.app_log_lines) - 500]

    # -- serial connection ----------------------------------------------------

    def _refresh_ports(self) -> None:
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if hasattr(self, "port_combo"):
            self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def _toggle_connect(self) -> None:
        if self.cat.connected:
            self._manual_disconnect = True
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

        try:
            cat, status, log_lines = self._connect_cat()
        except (CatError, OSError, serial.SerialException) as exc:
            messagebox.showerror("Verbindungsfehler", str(exc))
            return
        self._apply_connected_cat(cat, status, log_lines)

    def _connect_cat(self):
        """Core connect logic - reads settings StringVars (safe from any
        thread) but never touches Tkinter widgets and never shows a
        messagebox, so it's shared by the Verbinden button (main thread,
        caller shows errors via messagebox), startup auto-connect, and
        auto-reconnect after a dropped connection (both background
        threads, caller routes errors through log_result_queue instead).
        Returns (cat_client, status_message, log_lines) or raises
        CatError/OSError/serial.SerialException."""
        log_lines: list[str] = []
        if self.backend_var.get() == "rigctld":
            host = self.host_var.get().strip() or RIGCTLD_HOST_DEFAULT
            managed = host.lower() in ("localhost", "127.0.0.1")
            rig_port = self.rig_port_var.get()

            if managed:
                display = self.rig_model_display_var.get().strip()
                model_id = self._rig_model_id_from_display(display)
                if model_id is None:
                    raise CatError("Bitte ein Rig-Modell auswählen (ggf. erst über ↻ laden).")
                serial_port = self.port_var.get()
                if not serial_port:
                    raise CatError("Bitte den CAT-Port des Funkgeräts auswählen.")
                exe = find_rigctld_executable(self.rigctld_path_var.get())
                if not exe:
                    raise CatError(
                        "Hamlib ist nicht installiert, rigctld nicht im PATH und kein "
                        "rigctld-Pfad in den Settings eingetragen. Siehe https://hamlib.github.io/"
                    )
                self.rigctld_process.start(
                    exe, model_id, serial_port, self.baud_var.get(), "127.0.0.1", rig_port,
                )
                log_lines.append(f"rigctld gestartet (Modell {model_id}, {serial_port}, Port {rig_port}).")
                connect_host = "127.0.0.1"
            else:
                connect_host = host

            cat = RigctldClient()
            try:
                cat.connect(connect_host, rig_port)
            except (OSError, CatError):
                if managed:
                    self.rigctld_process.stop()
                raise
            if managed:
                self._save_rig_model(self.rig_model_display_var.get().strip(), model_id)
            status = f"Verbunden (rigctld {connect_host}:{rig_port})"
            log_lines.append(f"Verbunden mit rigctld auf {connect_host}:{rig_port}.")
            return cat, status, log_lines

        port = self.port_var.get()
        if not port:
            raise CatError("Bitte einen Port auswählen.")
        cat = Ft710Cat()
        cat.connect(port, self.baud_var.get())
        status = f"Verbunden ({port}, Freq.-Breite {cat.freq_width})"
        log_lines.append(f"Verbunden mit {port}.")
        return cat, status, log_lines

    def _apply_connected_cat(self, cat, status: str, log_lines: list[str]) -> None:
        self.cat = cat
        self.tune.cat = cat
        self.conn_status_var.set(status)
        for line in log_lines:
            self._log(line)
        if hasattr(self, "connect_btn"):
            self.connect_btn.configure(text="Trennen")
        self.cat_badge.configure(bg=COL_GREEN)
        self._save_connection_settings()
        self.reconnect_backoff_seconds = RECONNECT_BACKOFF_INITIAL_SECONDS
        self._next_reconnect_attempt_at = 0.0
        self._manual_disconnect = False

    def _has_saved_connection_settings(self) -> bool:
        if self.backend_var.get() == "rigctld":
            return bool(self.host_var.get().strip())
        return bool(self.port_var.get().strip())

    def _tick_cat_health(self) -> None:
        threading.Thread(target=self._cat_health_check_async, daemon=True).start()
        self.after(CAT_HEALTH_CHECK_SECONDS * 1000, self._tick_cat_health)

    def _cat_health_check_async(self) -> None:
        if self.cat.connected:
            try:
                self.cat.get_freq_hz()
            except CatError:
                self.connect_result_queue.put(("lost", None, None, None))
            return
        if self._manual_disconnect or not self._has_saved_connection_settings():
            return
        if time.monotonic() < self._next_reconnect_attempt_at:
            return
        try:
            cat, status, log_lines = self._connect_cat()
        except (CatError, OSError, serial.SerialException) as exc:
            self.reconnect_backoff_seconds = min(
                self.reconnect_backoff_seconds * 2, RECONNECT_BACKOFF_MAX_SECONDS,
            )
            self._next_reconnect_attempt_at = time.monotonic() + self.reconnect_backoff_seconds
            self.log_result_queue.put(
                f"Auto-Reconnect fehlgeschlagen (nächster Versuch in "
                f"{self.reconnect_backoff_seconds}s): {exc}"
            )
            return
        self.connect_result_queue.put(("ok", cat, status, log_lines))

    def _save_connection_settings(self) -> None:
        config = load_config()
        config["backend"] = self.backend_var.get()
        config["cat_port"] = self.port_var.get()
        config["cat_baud"] = self.baud_var.get()
        config["rigctld_host"] = self.host_var.get().strip()
        config["rigctld_port"] = self.rig_port_var.get()
        save_config(config)

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

    def _toggle_priority_alert(self) -> None:
        self.priority_alert_enabled = not self.priority_alert_enabled
        self.alert_btn.configure(
            text="Alarm: An" if self.priority_alert_enabled else "Alarm: Aus",
            fg=COL_ACCENT if self.priority_alert_enabled else COL_MUTED,
        )

    def _test_priority_alert(self) -> None:
        """Manually fires sound+toast, bypassing the mute toggle, so the
        alert can be tried out without waiting for a real favorite/
        Draussenfunker spot."""
        self._play_alert_sound()
        self._show_alert_toast(["🔔 Test-Alarm - so sieht/klingt ein echter Treffer aus."])
        self._log("Alarm-Test ausgelöst.")

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

    def _check_new_priority_spots(self, new_spots: list["Spot"]) -> None:
        """Alerts (sound + toast) on favorites/Draussenfunker spots that
        weren't in the previous poll - never on the first poll after
        startup, since every spot on air at that point is "new" to us but
        not actually a fresh spot."""
        new_ids = {s.spot_id for s in new_spots}
        if self.known_spot_ids is not None and self.priority_alert_enabled:
            fresh_ids = new_ids - self.known_spot_ids
            hits = [
                s for s in new_spots
                if s.spot_id in fresh_ids and not s.invalid
                and (self._is_favorite(s) or self._is_outdoor(s))
            ]
            if hits:
                self._alert_priority_spots(hits)
        self.known_spot_ids = new_ids

    def _alert_priority_spots(self, hits: list["Spot"]) -> None:
        self._play_alert_sound()
        lines = []
        for s in hits:
            icon = "⭐" if self._is_favorite(s) else "🏕"
            lines.append(f"{icon} {s.activator}  {s.frequency_khz:.1f} kHz {s.mode}  {s.reference}")
        self._show_alert_toast(lines)
        self._log(f"Alarm: {len(hits)}× Favorit/Draußenfunker neu gespottet.")

    def _play_alert_sound(self) -> None:
        # Belt-and-suspenders: try winsound.Beep() (a directly generated
        # tone, independent of the Windows sound scheme) AND always also
        # trigger Tk's own bell() - whichever the user's system actually
        # plays audibly wins. Any winsound failure is logged to the status
        # line instead of being swallowed, since we can't test real Windows
        # audio output from here. Runs in a thread since Beep() blocks for
        # its duration and would otherwise freeze the UI.
        def play() -> None:
            error = None
            try:
                import winsound
                winsound.Beep(880, 150)
                winsound.Beep(1175, 180)
            except Exception as exc:  # noqa: BLE001 - must never crash the alert
                error = f"{type(exc).__name__}: {exc}"
            self.after(0, self.bell)  # bell() must run on the Tk main thread
            if error:
                self.after(0, lambda: self._log(f"Alarm-Sound (winsound) fehlgeschlagen: {error}"))

        threading.Thread(target=play, daemon=True).start()

    def _show_alert_toast(self, lines: list[str]) -> None:
        toast = tk.Toplevel(self)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.configure(bg=COL_ACCENT)
        inner = tk.Frame(toast, bg=COL_PANEL, padx=14, pady=10)
        inner.pack(padx=2, pady=2)
        tk.Label(inner, text="🔔 Spot-Alarm", fg=COL_ACCENT, bg=COL_PANEL,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        for line in lines:
            tk.Label(inner, text=line, fg=COL_TEXT, bg=COL_PANEL,
                     font=("Segoe UI", 9), justify="left").pack(anchor="w", pady=(2, 0))

        self._alert_toasts.append(toast)

        def close() -> None:
            if toast in self._alert_toasts:
                self._alert_toasts.remove(toast)
            toast.destroy()

        toast.update_idletasks()
        offset = 40 + 90 * (len(self._alert_toasts) - 1)
        x = self.winfo_x() + self.winfo_width() - toast.winfo_width() - 24
        y = self.winfo_y() + offset
        toast.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        toast.bind("<Button-1>", lambda _e: close())
        toast.after(7000, close)

    # -- table rendering ----------------------------------------------------------

    def _on_filter_setting_changed(self) -> None:
        config = load_config()
        config["band_filter"] = self.band_filter_var.get()
        config["mode_filter"] = self.mode_filter_var.get()
        config["age_filter"] = self.age_filter_var.get()
        save_config(config)
        self._render_spots()

    def _spot_passes_filters(self, spot: Spot) -> bool:
        if spot.spot_id in self.skipped_ids:
            return False
        band = self.band_filter_var.get()
        if band != BAND_FILTER_OPTIONS[0] and band_for_khz(spot.frequency_khz) != band:
            return False
        mode_filter = self.mode_filter_var.get()
        if mode_filter == "CW & SSB":
            if mode_category(spot.mode) not in ("cw", "ssb"):
                return False
        elif mode_filter != MODE_FILTER_OPTIONS[0] and mode_category(spot.mode) != mode_filter.lower():
            return False
        age_filter = self.age_filter_var.get()
        if age_filter != AGE_FILTER_OPTIONS[0]:
            max_minutes = int(age_filter.split()[0])
            if spot_age_seconds(spot.spot_time) > max_minutes * 60:
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

    def _fetch_solar_data_async(self) -> None:
        data = fetch_solar_data() or {"sfi": "?", "k": "?", "a": "?", "muf": "?"}
        muf_value, muf_diag = fetch_juliusruh_muf()
        if muf_value is not None:
            data["muf"] = muf_value
            data["muf_source"] = "Juliusruh"
        else:
            data["muf_source"] = "hamqsl" if data["muf"] != "?" else "?"
            data["muf_diag"] = muf_diag
        self.solar_result_queue.put(data)

    def _tick_solar_data(self) -> None:
        threading.Thread(target=self._fetch_solar_data_async, daemon=True).start()
        self.after(SOLAR_POLL_SECONDS * 1000, self._tick_solar_data)

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
        apply_dark_titlebar(dlg)

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

    # -- QRZ XML distance lookup --------------------------------------------------

    def _qrz_xml_ready(self) -> bool:
        return bool(
            self.qrz_xml_user_var.get().strip()
            and self.qrz_xml_pass_var.get().strip()
            and not self.qrz_xml_auth_failed
        )

    def _update_optional_column_visibility(self) -> None:
        # OP is the only column still tied to the paid QRZ XML lookup - KM
        # now comes from the park's own coordinates, so it stays visible for
        # everyone. HÖRCHANCE is only meaningful with the RBN feed running.
        hidden: set[str] = set()
        if not self._qrz_xml_ready():
            hidden.add("op")
        if not self.rbn_enabled_var.get():
            hidden.add("chance")
        self.tree["displaycolumns"] = [c for c in self.all_columns if c not in hidden]

    # -- world map ------------------------------------------------------------

    def _build_map_panel(self, parent: tk.Frame) -> None:
        self.map_hint_var = tk.StringVar(value="")
        tk.Label(
            parent, textvariable=self.map_hint_var, fg=COL_MUTED, bg=COL_BG,
            font=("Segoe UI", 8), anchor="w",
        ).pack(fill="x", pady=(0, 2))

        _patch_tkintermapview_tile_loading()
        self.map_widget = tkintermapview.TkinterMapView(parent, corner_radius=0, bg_color=COL_BG)
        self.map_widget.pack(fill="both", expand=True)
        self.map_widget.set_tile_server(MAP_TILE_SERVER_DARK, max_zoom=20)
        self.map_widget.set_position(MAP_DEFAULT_LAT, MAP_DEFAULT_LON)
        self.map_widget.set_zoom(MAP_DEFAULT_ZOOM)

        # Kept as instance attributes - Tkinter PhotoImages are garbage
        # collected (and vanish from the canvas) the moment nothing in
        # Python still references them.
        self._map_icon_own = _make_map_dot_icon(COL_ACCENT, MAP_OWN_MARKER_DIAMETER)
        self._map_band_icons: dict[str, ImageTk.PhotoImage] = {}
        # 1x1 fully transparent icon: passing *any* icon (even invisible)
        # takes CanvasPositionMarker's "custom icon" draw path instead of
        # its hardcoded big teardrop-pin shape, so a marker with only text
        # (the QSY line's km label) shows just the label, no shape at all.
        self._map_icon_blank = ImageTk.PhotoImage(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))

        self.own_qth_marker = None
        self.spot_markers: list = []
        self.qsy_line = None
        self.qsy_line_label = None
        self.qsy_hear_circle = None
        self.qsy_hear_circle_label = None
        self._qsy_line_animation_job = None
        self._qsy_line_dash_offset = 0
        self._update_own_qth_marker()
        self._update_map_hint()

    def _update_map_hint(self) -> None:
        # Spot markers sit on the *park's* coordinates now, which come from
        # POTA itself and need no credentials at all - the only thing that
        # can still be missing is the user's own locator for the green
        # marker and the distance column.
        self.map_hint_var.set(
            "" if grid_to_latlon(self.my_grid_var.get()) is not None else
            "Für Entfernung und eigenen Standort auf der Karte: 'Eig. Locator' in den Settings eintragen."
        )

    def _update_own_qth_marker(self) -> None:
        if self.own_qth_marker is not None:
            self.own_qth_marker.delete()
            self.own_qth_marker = None
        latlon = grid_to_latlon(self.my_grid_var.get())
        if latlon is None:
            return
        lat, lon = latlon
        # No text label here on purpose: this marker sits at a fixed spot
        # every QSY line starts from, so a permanent label there would
        # collide with the km-distance label on almost every line (see
        # _update_qsy_line) - color + size (see MAP_OWN_MARKER_DIAMETER)
        # already distinguish it from spot markers.
        self.own_qth_marker = self.map_widget.set_marker(
            lat, lon, icon=self._map_icon_own, icon_anchor="center",
        )
        self.map_widget.set_position(lat, lon)

    def _get_band_marker_icon(self, band: str) -> ImageTk.PhotoImage:
        icon = self._map_band_icons.get(band)
        if icon is None:
            color = BAND_MAP_COLORS.get(band, BAND_MAP_COLOR_DEFAULT)
            icon = _make_map_dot_icon(color, MAP_SPOT_MARKER_DIAMETER)
            self._map_band_icons[band] = icon
        return icon

    def _update_map_markers(self, visible_spots: list[Spot]) -> None:
        for marker in self.spot_markers:
            marker.delete()
        self.spot_markers = []
        self._update_map_hint()
        # One pin per park reference (not per callsign): the reference is
        # what actually has a position, and two activators in the same park
        # would otherwise stack two markers on the identical coordinates.
        plotted: set[str] = set()
        for spot in visible_spots:
            key = (spot.reference or "").strip().upper() or base_callsign_for_lookup(spot.activator)
            if not key or key in plotted:
                continue
            latlon = self._spot_latlon(spot)
            if latlon is None:
                continue
            plotted.add(key)
            lat, lon = latlon
            band = band_for_khz(spot.frequency_khz)
            marker = self.map_widget.set_marker(
                lat, lon, text=spot.activator,
                icon=self._get_band_marker_icon(band), icon_anchor="center",
                text_color=BAND_MAP_COLORS.get(band, BAND_MAP_COLOR_DEFAULT),
                command=self._on_map_marker_click, data=spot.spot_id,
            )
            self.spot_markers.append(marker)

    def _on_map_marker_click(self, marker) -> None:
        if marker.data is not None:
            self._qsy_to_spot_id(marker.data)

    # -- hunter stats -----------------------------------------------------------

    def _build_stats_panel(self, parent: tk.Frame) -> None:
        header_row = tk.Frame(parent, bg=COL_BG)
        header_row.pack(fill="x", pady=(0, 4))
        tk.Label(header_row, text="Statistik", fg=COL_ACCENT, bg=COL_BG,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        _chip_button(header_row, "↻", command=self._refresh_stats).pack(side="right")

        filter_row = tk.Frame(parent, bg=COL_BG)
        filter_row.pack(fill="x", pady=(0, 4))
        self.stats_filter = "all"
        self.stats_filter_btns: dict[str, tk.Button] = {}
        for key, label in (("today", "Heute"), ("yesterday", "Gestern"), ("all", "Gesamt")):
            btn = _chip_button(filter_row, label, command=lambda k=key: self._set_stats_filter(k))
            btn.configure(padx=10, pady=3, font=("Segoe UI", 8, "bold"))
            btn.pack(side="left", padx=(0, 4))
            self.stats_filter_btns[key] = btn
        self._update_stats_filter_buttons()

        self.stats_summary_var = tk.StringVar(value="")
        tk.Label(
            parent, textvariable=self.stats_summary_var, fg=COL_MUTED, bg=COL_BG,
            font=("Segoe UI", 8), anchor="w", justify="left",
        ).pack(fill="x", pady=(0, 4))

        stats_tree_frame = tk.Frame(parent, bg=COL_BG)
        stats_tree_frame.pack(fill="both", expand=True)

        columns = ("band", "cw", "ssb", "digital", "total")
        headers = {"band": "Band", "cw": "CW", "ssb": "SSB", "digital": "Digital", "total": "Gesamt"}
        widths = {"band": 55, "cw": 45, "ssb": 45, "digital": 55, "total": 55}
        # height=6 is just the *starting* visible row count, not a cap -
        # up to 11 bands + a totals row can exist, reachable via the
        # scrollbar rather than silently invisible.
        self.stats_tree = ttk.Treeview(stats_tree_frame, columns=columns, show="headings", height=6)
        for col in columns:
            self.stats_tree.heading(col, text=headers[col])
            self.stats_tree.column(col, width=widths[col], anchor="w" if col == "band" else "center")
        stats_vsb = ttk.Scrollbar(stats_tree_frame, orient="vertical", command=self.stats_tree.yview,
                                   style="Dark.Vertical.TScrollbar")
        self.stats_tree.configure(yscrollcommand=stats_vsb.set)
        self.stats_tree.pack(side="left", fill="both", expand=True)
        stats_vsb.pack(side="right", fill="y")
        self.stats_tree.tag_configure("stats_total", foreground=COL_ACCENT, font=("Segoe UI", 10, "bold"))

        self.stats_hint_var = tk.StringVar(value="")
        tk.Label(
            parent, textvariable=self.stats_hint_var,
            fg=COL_MUTED, bg=COL_BG, font=("Segoe UI", 7), anchor="w", justify="left", wraplength=300,
        ).pack(fill="x", pady=(4, 0))

        self._refresh_stats()

    _STATS_FILTER_LABELS = {"today": "heute", "yesterday": "gestern", "all": "alle Tage"}

    def _set_stats_filter(self, filter_key: str) -> None:
        self.stats_filter = filter_key
        self._update_stats_filter_buttons()
        self._refresh_stats()

    def _update_stats_filter_buttons(self) -> None:
        for key, btn in self.stats_filter_btns.items():
            active = key == self.stats_filter
            btn.configure(fg=COL_ACCENT if active else COL_MUTED, bg=COL_BORDER if active else COL_PANEL_ALT)

    def _refresh_stats(self) -> None:
        if not hasattr(self, "stats_tree"):
            return
        stats = compute_band_mode_park_stats(load_adif_records_for_stats_filter(self.stats_filter))
        self.stats_summary_var.set(
            f"{stats['total_parks']} eindeutige Parks - {stats['total_qsos']} QSOs gesamt"
        )
        self.stats_hint_var.set(
            f"Eindeutige Parks je Band/Mode aus dem eigenen ADIF-Log "
            f"({self._STATS_FILTER_LABELS[self.stats_filter]})."
        )

        self.stats_tree.delete(*self.stats_tree.get_children())
        band_mode_parks = stats["band_mode_parks"]
        for _, _, band in BAND_RANGES_KHZ:
            per_mode = band_mode_parks.get(band)
            if not per_mode:
                continue
            total = len(per_mode["cw"] | per_mode["ssb"] | per_mode["digital"])
            self.stats_tree.insert("", "end", values=(
                band, len(per_mode["cw"]), len(per_mode["ssb"]), len(per_mode["digital"]), total,
            ))

        if band_mode_parks:
            all_cw = set().union(*(v["cw"] for v in band_mode_parks.values()))
            all_ssb = set().union(*(v["ssb"] for v in band_mode_parks.values()))
            all_digital = set().union(*(v["digital"] for v in band_mode_parks.values()))
            self.stats_tree.insert("", "end", tags=("stats_total",), values=(
                "Gesamt", len(all_cw), len(all_ssb), len(all_digital), stats["total_parks"],
            ))

    # -- QSY great-circle line ------------------------------------------------

    def _clear_qsy_line(self) -> None:
        if self._qsy_line_animation_job is not None:
            self.after_cancel(self._qsy_line_animation_job)
            self._qsy_line_animation_job = None
        if self.qsy_line is not None:
            self.qsy_line.delete()
            self.qsy_line = None
        if self.qsy_line_label is not None:
            self.qsy_line_label.delete()
            self.qsy_line_label = None
        if self.qsy_hear_circle is not None:
            self.qsy_hear_circle.delete()
            self.qsy_hear_circle = None
        if self.qsy_hear_circle_label is not None:
            self.qsy_hear_circle_label.delete()
            self.qsy_hear_circle_label = None

    def _update_qsy_line(self, spot: Spot) -> None:
        """Draws an animated dashed line from the own QTH to the spot just
        QSY'd to, labelled with the great-circle-ish distance - same park
        coordinates as the map markers and the KM column. Also draws a ring
        around the activator showing how far they're currently being heard
        (see _draw_hear_circle) when RBN data for them is available."""
        self._clear_qsy_line()
        my_latlon = grid_to_latlon(self.my_grid_var.get())
        if my_latlon is None:
            return
        target_latlon = self._spot_latlon(spot)
        if target_latlon is None:
            return
        km = haversine_km(my_latlon[0], my_latlon[1], target_latlon[0], target_latlon[1])

        self.qsy_line = self.map_widget.set_path([my_latlon, target_latlon], color=COL_ACCENT, width=3)
        self.map_widget.canvas.itemconfig(self.qsy_line.canvas_line, dash=(6, 4))
        self._qsy_line_dash_offset = 0
        self._animate_qsy_line()

        # Geometric midpoint, but lifted well above the line via
        # text_y_offset below - own-QTH marker has no text of its own
        # (see _update_own_qth_marker) so sitting at 50% no longer collides
        # with anything there, and staying centered (rather than biased
        # toward either end) keeps clear of both the spot's own label and
        # the dashed line itself.
        label_frac = 0.5
        label_lat = my_latlon[0] + (target_latlon[0] - my_latlon[0]) * label_frac
        label_lon = my_latlon[1] + (target_latlon[1] - my_latlon[1]) * label_frac
        self.qsy_line_label = self.map_widget.set_marker(
            label_lat, label_lon, text=f"{km:.0f} km",
            icon=self._map_icon_blank, icon_anchor="center", text_color=COL_ACCENT,
        )
        self.qsy_line_label.text_y_offset = -18
        self.qsy_line_label.draw()

        self._draw_hear_circle(spot, target_latlon)

    def _draw_hear_circle(self, spot: Spot, center_latlon: tuple[float, float]) -> None:
        """Ring around the activator's park at the distance of the
        farthest-away RBN skimmer currently hearing them on this band - a
        live, measured "how far does his signal actually reach right now"
        indicator, not a modelled propagation contour. Requires the RBN
        feature on and at least one located skimmer report; otherwise draws
        nothing (silently - the HÖRCHANCE column's own '–'/'aus' already
        covers explaining why)."""
        if not self.rbn_enabled_var.get():
            return
        call = base_callsign_for_lookup(spot.activator)
        band = band_for_khz(spot.frequency_khz)
        if not call or band == "?":
            return
        farthest_km = 0.0
        farthest_skimmer = ""
        for report in self.rbn_store.reports_for(call, band):
            skimmer_pos = skimmer_latlon(report.skimmer)
            if skimmer_pos is None:
                continue
            dist = haversine_km(center_latlon[0], center_latlon[1], skimmer_pos[0], skimmer_pos[1])
            if dist > farthest_km:
                farthest_km = dist
                farthest_skimmer = report.skimmer
        if farthest_km <= 0:
            return

        points = circle_points_km(center_latlon[0], center_latlon[1], farthest_km, RBN_HEAR_CIRCLE_SEGMENTS)
        self.qsy_hear_circle = self.map_widget.set_polygon(
            points, outline_color=COL_HEAR_CIRCLE, fill_color=None, border_width=2,
        )
        label_lat, label_lon = destination_point_km(center_latlon[0], center_latlon[1], 0.0, farthest_km)
        self.qsy_hear_circle_label = self.map_widget.set_marker(
            label_lat, label_lon, text=f"max. gehört ~{farthest_km:.0f} km ({farthest_skimmer})",
            icon=self._map_icon_blank, icon_anchor="center", text_color=COL_HEAR_CIRCLE,
        )
        self.qsy_hear_circle_label.text_y_offset = -10
        self.qsy_hear_circle_label.draw()

    def _animate_qsy_line(self) -> None:
        if self.qsy_line is None or self.qsy_line.canvas_line is None:
            self._qsy_line_animation_job = None
            return
        self._qsy_line_dash_offset = (self._qsy_line_dash_offset + 1) % 10
        self.map_widget.canvas.itemconfig(self.qsy_line.canvas_line, dashoffset=self._qsy_line_dash_offset)
        self._qsy_line_animation_job = self.after(80, self._animate_qsy_line)

    def _spot_latlon(self, spot: Spot) -> tuple[float, float] | None:
        """Where the activator physically is. The park reference is the
        authoritative answer - it's a fixed, published location, unlike the
        activator's QRZ home address, which is where they live rather than
        where they're currently sitting in a park. QRZ is only consulted as
        a last resort for spots whose reference has no coordinates."""
        if spot.park_latlon is not None:
            return spot.park_latlon
        reference = (spot.reference or "").strip().upper()
        if reference:
            # A cached None means POTA itself has no position for this
            # reference - falling through to QRZ below is then the only
            # remaining option, and _queue_park_lookups() won't ask again.
            cached = self.park_cache.get(reference)
            if cached is not None:
                return cached
        if self._qrz_xml_ready():
            info = self.locator_cache.get(base_callsign_for_lookup(spot.activator))
            if info and info.latlon:
                return info.latlon
        return None

    def _spot_distance_km(self, spot: Spot) -> float | None:
        my_latlon = grid_to_latlon(self.my_grid_var.get())
        if my_latlon is None:
            return None
        target = self._spot_latlon(spot)
        if target is None:
            return None
        return haversine_km(my_latlon[0], my_latlon[1], target[0], target[1])

    def _format_distance(self, spot: Spot) -> str:
        dist = self._spot_distance_km(spot)
        return f"{dist:.0f}" if dist is not None else ""

    # -- park reference -> coordinates -----------------------------------------

    def _queue_park_lookups(self, spots: list[Spot]) -> None:
        """Only spots whose own feed entry carried no coordinates need the
        per-park endpoint, so in practice this queue stays near-empty."""
        for spot in spots:
            if spot.park_latlon is not None:
                continue
            reference = (spot.reference or "").strip().upper()
            if not reference or reference in self.park_cache or reference in self.park_lookup_pending:
                continue
            self.park_lookup_pending.add(reference)
            self.park_work_queue.put(reference)

    def _park_worker(self) -> None:
        while True:
            reference = self.park_work_queue.get()
            try:
                latlon = fetch_park_info(reference)
                save_park_cache_entry(reference, latlon)
                self.park_result_queue.put(("ok", reference, latlon))
            except Exception as exc:  # noqa: BLE001 - worker must not die
                # Deliberately not cached: a failed request says nothing
                # about whether this park has coordinates, so the next spot
                # poll re-queues it instead of writing a sticky "no
                # position" that would survive for PARK_CACHE_MISS_TTL.
                self.park_result_queue.put(("error", reference, str(exc)))
            finally:
                self.park_work_queue.task_done()

    # -- RBN "Chance to Hear" ---------------------------------------------------

    def _apply_rbn_settings(self) -> None:
        self.rbn_client.configure(bool(self.rbn_enabled_var.get()), self.my_callsign_var.get())
        self._update_optional_column_visibility()
        self._update_rbn_badge()

    def _update_rbn_badge(self) -> None:
        if not self.rbn_enabled_var.get():
            self.rbn_badge_var.set("RBN aus")
            self.rbn_badge.configure(fg=COL_MUTED)
            return
        status = self.rbn_client.status
        if self.rbn_client.connected:
            calls, reports = self.rbn_store.stats()
            self.rbn_badge_var.set(f"RBN {reports} Rprt · {calls} Calls")
            self.rbn_badge.configure(fg=COL_GREEN)
        else:
            self.rbn_badge_var.set(f"RBN {status}")
            self.rbn_badge.configure(fg=COL_AMBER)

    def _tick_rbn(self) -> None:
        """Called from the 200 ms UI tick. Everything in here is on its own
        much slower timer - the RBN feed delivers several reports a second,
        and neither the badge nor a full table redraw should follow that."""
        now = time.monotonic()
        if now >= self._rbn_next_badge_at:
            self._rbn_next_badge_at = now + RBN_BADGE_REFRESH_SECONDS
            self._update_rbn_badge()
        if now >= self._rbn_next_prune_at:
            self._rbn_next_prune_at = now + RBN_PRUNE_INTERVAL_SECONDS
            self.rbn_store.prune()
        if not self.rbn_enabled_var.get():
            return
        if now >= self._rbn_next_render_at:
            self._rbn_next_render_at = now + RBN_RENDER_REFRESH_SECONDS
            _, reports = self.rbn_store.stats()
            if reports != self._rbn_last_rendered_reports:
                self._rbn_last_rendered_reports = reports
                self._render_spots()

    def _refresh_chance_context(self) -> None:
        """Per-render setup for the HÖRCHANCE column: the user's own
        position and how many skimmers are active near them (see
        chance_to_hear - that count decides whether "no nearby skimmer heard
        it" means anything)."""
        self._chance_cache = {}
        self._my_latlon = grid_to_latlon(self.my_grid_var.get())
        self._regional_skimmers_active = 0
        if self._my_latlon is None or not self.rbn_enabled_var.get():
            return
        lat, lon = self._my_latlon
        for skimmer in self.rbn_store.active_skimmers():
            latlon = skimmer_latlon(skimmer)
            if latlon is not None and haversine_km(lat, lon, latlon[0], latlon[1]) <= RBN_REGION_RADIUS_KM:
                self._regional_skimmers_active += 1

    def _chance_for_spot(self, spot: Spot) -> ChanceToHear | None:
        if self._my_latlon is None or not self.rbn_enabled_var.get():
            return None
        if spot.spot_id in self._chance_cache:
            return self._chance_cache[spot.spot_id]
        call = base_callsign_for_lookup(spot.activator)
        band = band_for_khz(spot.frequency_khz)
        result = None
        if call and band != "?":
            result = chance_to_hear(
                self.rbn_store.reports_for(call, band),
                self._my_latlon,
                self._regional_skimmers_active,
            )
        self._chance_cache[spot.spot_id] = result
        return result

    def _format_chance(self, spot: Spot) -> str:
        if not self.rbn_enabled_var.get():
            return ""
        if self._my_latlon is None:
            return "?"
        chance = self._chance_for_spot(spot)
        if chance is None:
            # No skimmer heard this station on this band recently. For CW
            # that is itself mildly informative; for SSB/FT8 the RBN's CW
            # feed simply never covers it, so neither is dressed up as a
            # number here.
            return "–"
        prefix = "~" if chance.estimate else ""
        return f"{prefix}{chance.score}% {chance.quality}"

    def _spot_op_name(self, spot: Spot) -> str:
        if not self._qrz_xml_ready():
            return ""
        info = self.locator_cache.get(base_callsign_for_lookup(spot.activator))
        return info.op_name if info and info.op_name else ""

    def _queue_locator_lookups(self, spots: list[Spot]) -> None:
        if not self._qrz_xml_ready():
            return
        calls = {base_callsign_for_lookup(s.activator) for s in spots if s.activator}
        for call in calls:
            if not call or call in self.locator_cache or call in self.locator_lookup_pending:
                continue
            self.locator_lookup_pending.add(call)
            self.locator_work_queue.put(call)

    def _locator_worker(self) -> None:
        """Persistent pool of LOCATOR_WORKER_COUNT threads pulling from a
        shared queue - a single QRZ session key is reused across all of
        them (see QrzXmlClient), so this gives real parallel lookups
        instead of one thread per callsign serializing behind a lock."""
        while True:
            call = self.locator_work_queue.get()
            username = self.qrz_xml_user_var.get().strip()
            password = self.qrz_xml_pass_var.get().strip()
            if username and password:
                try:
                    info = self.qrz_xml_client.lookup_callsign(username, password, call)
                    save_qrz_cache_entry(call, info)
                    self.locator_result_queue.put(("ok", call, info))
                except QrzXmlAuthError as exc:
                    self.locator_result_queue.put(("auth_error", call, str(exc)))
                except (QrzXmlError, requests.RequestException) as exc:
                    self.locator_result_queue.put(("error", call, str(exc)))
            else:
                self.locator_result_queue.put(("error", call, "keine Zugangsdaten mehr hinterlegt"))
            self.locator_work_queue.task_done()

    def _column_sort_key(self, col: str):
        if col == "freq":
            return lambda s: s.frequency_khz
        if col == "age":
            return lambda s: spot_age_seconds(s.spot_time)
        if col == "dist":
            return lambda s: (d if (d := self._spot_distance_km(s)) is not None else 10**9)
        if col == "chance":
            # Descending by default (best chance on top on the first click),
            # so the key is negated - spots with no RBN data sort last.
            return lambda s: -(c.score if (c := self._chance_for_spot(s)) is not None else -1)
        if col == "call":
            return lambda s: (s.activator or "").upper()
        if col == "op":
            return lambda s: self._spot_op_name(s).upper()
        if col == "mode":
            return lambda s: (s.mode or "").upper()
        if col == "ref":
            return lambda s: (s.reference or "").upper()
        if col == "name":
            return lambda s: (s.park_name or "").upper()
        if col == "loc":
            return lambda s: (s.location_desc or "").upper()
        if col == "worked":
            return lambda s: worked_today_badge(s, self.worked_today_index)
        return lambda s: 0

    def _sort_by_column(self, col: str) -> None:
        if self.sort_column == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = col
            self.sort_reverse = False
        for c, base_text in self.column_headers.items():
            if c not in self.tree["columns"] or not base_text:
                continue
            if c == self.sort_column:
                arrow = " ▼" if self.sort_reverse else " ▲"
                self.tree.heading(c, text=base_text + arrow)
            else:
                self.tree.heading(c, text=base_text)
        self._render_spots()

    def _render_spots(self) -> None:
        self.tree.delete(*self.tree.get_children())
        # Must run before the sort below: _column_sort_key("chance") scores
        # spots against exactly this context.
        self._refresh_chance_context()
        visible = [s for s in self.spots if self._spot_passes_filters(s)]
        # Stable sort, least significant first: column sort (if any) orders
        # spots within each favorite/outdoor group, then the priority sort
        # moves favorites/outdoor spots to the top regardless of column sort.
        if self.sort_column:
            visible.sort(key=self._column_sort_key(self.sort_column), reverse=self.sort_reverse)
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
            if spot.spot_id == self.qsy_spot_id:
                tags += ("qsy_current",)

            self.tree.insert("", "end", iid=str(spot.spot_id), tags=tags, values=(
                fav_icon,
                outdoor_icon,
                "▶ QSY",
                call,
                self._spot_op_name(spot),
                worked_text,
                freq_text,
                spot.mode,
                spot.reference,
                name,
                spot.location_desc,
                self._format_distance(spot),
                self._format_chance(spot),
                format_age(spot.spot_time),
                "✕ Skip",
                "📝 Log",
            ))
        self.count_badge_var.set(f"{len(visible)} Spots")
        self._update_map_markers(visible)

    def _on_tree_click(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
        if not row_id or not col_id:
            return
        # identify_column() numbers columns by their current *display*
        # position, not their fixed position in `columns` - and the "dist"
        # and "op" columns are shown/hidden at runtime (see
        # _update_optional_column_visibility), which shifts everything after
        # them. Resolve the clicked column name from the live
        # displaycolumns instead of a hardcoded "#N", so Skip/Log etc.
        # keep working either way.
        col = self._resolve_display_column(col_id)
        if col == "fav":
            self._toggle_favorite(int(row_id))
        elif col == "qsy":
            self._qsy_to_spot_id(int(row_id))
        elif col == "skip":
            self.skipped_ids.add(int(row_id))
            self._render_spots()
        elif col == "log":
            self._open_log_dialog_for_spot_id(int(row_id))
        elif col == "call":
            self._open_pota_profile(int(row_id))
        elif col == "name":
            self._open_pota_park(int(row_id))

    def _on_tree_motion(self, event) -> None:
        col_id = self.tree.identify_column(event.x)
        row_id = self.tree.identify_row(event.y)
        col = self._resolve_display_column(col_id) if row_id else None
        self.tree.configure(cursor="hand2" if col in ("call", "name") else "")

    def _resolve_display_column(self, col_id: str) -> str | None:
        try:
            idx = int(col_id.lstrip("#")) - 1
        except ValueError:
            return None
        displaycols = self.tree["displaycolumns"]
        if displaycols in ("#all", ""):
            displaycols = self.all_columns
        if not (0 <= idx < len(displaycols)):
            return None
        return displaycols[idx]

    def _open_pota_profile(self, spot_id: int) -> None:
        spot = next((s for s in self.spots if s.spot_id == spot_id), None)
        call = (spot.activator or "").strip().upper() if spot else ""
        if call:
            webbrowser.open(f"https://pota.app/#/profile/{call}")

    def _open_pota_park(self, spot_id: int) -> None:
        spot = next((s for s in self.spots if s.spot_id == spot_id), None)
        ref = (spot.reference or "").strip().upper() if spot else ""
        if ref:
            webbrowser.open(f"https://pota.app/#/park/{ref}")

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
        self.qsy_spot_id = spot_id
        self._update_qsy_line(spot)
        self._render_spots()

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
        apply_dark_titlebar(dlg)

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
        self._refresh_stats()

        api_key = self.qrz_api_key_var.get().strip()
        if api_key:
            threading.Thread(
                target=self._upload_to_qrz_async, args=(api_key, record, call), daemon=True,
            ).start()

        wavelog_url = self.wavelog_url_var.get().strip()
        wavelog_key = self.wavelog_api_key_var.get().strip()
        wavelog_station_id = self.wavelog_station_id_var.get().strip()
        if wavelog_url and wavelog_key and wavelog_station_id:
            threading.Thread(
                target=self._upload_to_wavelog_async,
                args=(wavelog_url, wavelog_key, wavelog_station_id, record, call),
                daemon=True,
            ).start()

        if self.respot_enabled_var.get():
            try:
                frequency_khz = float(vars_["freq"].get().strip()) * 1000
            except ValueError:
                frequency_khz = None
            if frequency_khz is not None:
                comment = render_respot_comment(self.respot_template_var.get(), {
                    "call": call,
                    "mycall": self.my_callsign_var.get().strip().upper(),
                    "rst_sent": fields.get("RST_SENT", ""),
                    "rst_rcvd": fields.get("RST_RCVD", ""),
                    "freq": vars_["freq"].get().strip(),
                    "mode": fields["MODE"],
                    "ref": vars_["sig_info"].get().strip(),
                })
                threading.Thread(
                    target=self._post_respot_async,
                    args=(call, frequency_khz, fields["MODE"], vars_["sig_info"].get().strip(), comment),
                    daemon=True,
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

    def _upload_to_wavelog_async(
        self, base_url: str, api_key: str, station_profile_id: str, record: str, call: str,
    ) -> None:
        try:
            ok, info = upload_to_wavelog(base_url, api_key, station_profile_id, record)
        except requests.RequestException as exc:
            self.log_result_queue.put(f"Wavelog-Upload für {call} fehlgeschlagen: {exc}")
            return
        if ok:
            self.log_result_queue.put(f"Wavelog: {call} hochgeladen.")
        else:
            self.log_result_queue.put(f"Wavelog: Upload für {call} fehlgeschlagen ({info}).")

    def _post_respot_async(
        self, activator: str, frequency_khz: float, mode: str, reference: str, comments: str,
    ) -> None:
        spotter = self.my_callsign_var.get().strip().upper()
        try:
            ok, info = post_pota_spot(activator, spotter, frequency_khz, mode, reference, comments)
        except requests.RequestException as exc:
            self.log_result_queue.put(f"Respot für {activator} fehlgeschlagen: {exc}")
            return
        if ok:
            self.log_result_queue.put(f"Respot für {activator} gesendet ({reference}, {mode}).")
        else:
            self.log_result_queue.put(f"Respot für {activator} fehlgeschlagen ({info}).")

    # -- tune button ------------------------------------------------------------

    def _on_tune_press(self, _event) -> None:
        if self.tune.active:
            return
        sign = -1 if self.offset_sign_var.get() == "below" else 1
        try:
            self.tune.start(self.tune_power_var.get(), sign, self.offset_var.get(), self._power_for_mode)
        except Exception as exc:  # noqa: BLE001 - a Tk callback exception is otherwise
            # invisible in a --windowed build (no console for the traceback), so
            # anything going wrong here must be caught and shown, not just CatError.
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
        try:
            self.tune.stop()
        except Exception as exc:  # noqa: BLE001 - see _on_tune_press
            self._log(f"Fehler beim Beenden von TUNE: {exc}")
        finally:
            self.tune_btn.configure(text="TUNE (halten)", bg=COL_AMBER, fg="#1a1200")
        try:
            confirmed_freq = self.cat.get_freq_hz()
        except Exception:  # noqa: BLE001
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

    def _retry_dark_titlebar(self) -> None:
        if not self._dark_titlebar_ok:
            self._dark_titlebar_ok = apply_dark_titlebar(self)
        if sys.platform == "win32" and not self._dark_titlebar_ok:
            self._log(
                "Hinweis: Windows hat die dunkle Titelleiste nicht übernommen "
                "(von DWM abgelehnt) - dafür wird Windows 10 Version 2004/20H1 "
                "oder neuer bzw. Windows 11 benötigt."
            )

    def _tick(self) -> None:
        try:
            self._tick_body()
        except Exception as exc:
            # Any uncaught exception here would otherwise stop the
            # self.after() reschedule below, silently freezing spots,
            # solar data and CAT status updates for the rest of the run.
            self._log(f"Interner Fehler in der Aktualisierungsschleife: {exc}")
        self.after(200, self._tick)

    def _tick_body(self) -> None:
        try:
            while True:
                kind, payload = self.spot_result_queue.get_nowait()
                if kind == "ok":
                    self._check_new_priority_spots(payload)
                    self.spots = payload
                    self._update_country_options()
                    self._queue_locator_lookups(payload)
                    self._queue_park_lookups(payload)
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

        locator_changed = False
        try:
            while True:
                kind, call, payload = self.locator_result_queue.get_nowait()
                self.locator_lookup_pending.discard(call)
                if kind == "ok":
                    self.locator_cache[call] = payload
                    locator_changed = True
                elif kind == "error":
                    self.locator_cache[call] = None
                    locator_changed = True
                elif kind == "auth_error":
                    self.qrz_xml_auth_failed = True
                    self._update_optional_column_visibility()
                    self._log(f"QRZ-XML-Login fehlgeschlagen: {payload}")
        except queue.Empty:
            pass
        if locator_changed:
            self._render_spots()

        park_changed = False
        try:
            while True:
                kind, reference, payload = self.park_result_queue.get_nowait()
                self.park_lookup_pending.discard(reference)
                if kind == "ok":
                    self.park_cache[reference] = payload
                    park_changed = True
                    if payload is None and not self._park_miss_logged:
                        # Couldn't be verified against the live endpoint
                        # while this was written, so say it out loud once
                        # rather than silently showing an empty KM column if
                        # the response shape ever differs from the spot
                        # feed's (which uses latitude/longitude/grid6).
                        self._park_miss_logged = True
                        self._log(
                            f"Park {reference}: keine Koordinaten in der POTA-Antwort - "
                            f"Karte/KM fallen für diesen Park auf QRZ zurück."
                        )
                else:
                    self._log(f"Park {reference} konnte nicht geladen werden: {payload}")
        except queue.Empty:
            pass
        if park_changed:
            self._render_spots()

        self._tick_rbn()

        try:
            while True:
                data = self.solar_result_queue.get_nowait()
                self.solar_data_var.set(
                    f"SFI {data['sfi']} · K {data['k']} · A {data['a']} · MUF {data['muf']} MHz"
                )
                if not self._solar_diag_logged:
                    self._solar_diag_logged = True
                    if data.get("muf_diag") and data["muf_diag"] != "ok":
                        self._log(
                            f"Juliusruh-MUF nicht verfügbar (zeige {data['muf_source']}-Wert "
                            f"stattdessen): {data['muf_diag']}"
                        )
                    elif data.get("muf_source") == "Juliusruh":
                        self._log("MUF: Juliusruh-Ionosonde erfolgreich abgefragt.")
                    if data.get("muf_hamqsl_diag"):
                        self._log(f"hamqsl-Feed: {data['muf_hamqsl_diag']}")
        except queue.Empty:
            pass

        try:
            while True:
                kind, cat, status, log_lines = self.connect_result_queue.get_nowait()
                if kind == "lost":
                    if self.tune.active:
                        self.tune.active = False
                        self.tune_btn.configure(text="TUNE (halten)", bg=COL_AMBER, fg="#1a1200")
                    self.cat.disconnect()
                    if self.rigctld_process.running:
                        self.rigctld_process.stop()
                    self.conn_status_var.set("Verbindung verloren - versuche automatisch neu zu verbinden…")
                    if hasattr(self, "connect_btn"):
                        self.connect_btn.configure(text="Verbinden")
                    self.cat_badge.configure(bg=COL_RED)
                    self._log("CAT-Verbindung verloren, versuche automatisch neu zu verbinden.")
                elif kind == "ok":
                    self._apply_connected_cat(cat, status, log_lines)
                    self._log("Automatisch (neu) verbunden.")
        except queue.Empty:
            pass

        if self.tune.active and self.tune.elapsed() > MAX_TUNE_SECONDS:
            self.tune.stop()
            self.tune_btn.configure(text="TUNE (halten)", bg=COL_AMBER, fg="#1a1200")
            self._log("Sicherheits-Timeout erreicht, TUNE automatisch beendet.")

    def destroy(self) -> None:
        self.stop_poll_event.set()
        self.rbn_client.stop()
        if self.tune.active:
            self.tune.stop()
        self.cat.disconnect()
        if self.rigctld_process.running:
            self.rigctld_process.stop()
        super().destroy()


if __name__ == "__main__":
    App().mainloop()
