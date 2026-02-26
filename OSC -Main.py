#!/usr/bin/env python3
"""
VRC OSC Chatbox — Professional VRChat Chatbox Controller
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font
import json
import os
import socket
import struct
import threading
import time
import re
import datetime
import subprocess
import sys
import http.server
import urllib.request
import urllib.parse
import socketserver
from pathlib import Path


# ─────────────────────────────────────────────
#  Minimal OSC Implementation (no dependencies)
# ─────────────────────────────────────────────

def _pad4(n):
    return (4 - n % 4) % 4

def _encode_string(s: str) -> bytes:
    b = s.encode("utf-8") + b"\x00"
    b += b"\x00" * _pad4(len(b))
    return b

def _encode_int(i: int) -> bytes:
    return struct.pack(">i", i)

def _encode_float(f: float) -> bytes:
    return struct.pack(">f", f)

def _encode_bool(b: bool) -> bytes:
    return b""  # bool has no data in OSC, only type tag

def osc_message(address: str, *args) -> bytes:
    addr_bytes = _encode_string(address)
    type_tag = ","
    data_bytes = b""
    for arg in args:
        if isinstance(arg, bool):
            type_tag += "T" if arg else "F"
        elif isinstance(arg, int):
            type_tag += "i"
            data_bytes += _encode_int(arg)
        elif isinstance(arg, float):
            type_tag += "f"
            data_bytes += _encode_float(arg)
        elif isinstance(arg, str):
            type_tag += "s"
            data_bytes += _encode_string(arg)
    tag_bytes = _encode_string(type_tag)
    return addr_bytes + tag_bytes + data_bytes

def send_osc(ip: str, port: int, address: str, *args):
    msg = osc_message(address, *args)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(msg, (ip, port))


def parse_osc_message(data: bytes):
    try:
        addr_end = data.index(b'\x00')
        address = data[:addr_end].decode('utf-8', errors='replace')
        addr_padded = addr_end + 1 + _pad4(addr_end + 1)
        rest = data[addr_padded:]
        if not rest or rest[0:1] != b',':
            return address, []
        tag_end = rest.index(b'\x00')
        type_tags = rest[1:tag_end].decode('utf-8', errors='replace')
        tag_padded = tag_end + 1 + _pad4(tag_end + 1)
        rest = rest[tag_padded:]
        args = []
        for tag in type_tags:
            if tag == 'i':
                args.append(struct.unpack('>i', rest[:4])[0]); rest = rest[4:]
            elif tag == 'f':
                args.append(round(struct.unpack('>f', rest[:4])[0], 4)); rest = rest[4:]
            elif tag == 's':
                s_end = rest.index(b'\x00')
                args.append(rest[:s_end].decode('utf-8', errors='replace'))
                s_padded = s_end + 1 + _pad4(s_end + 1)
                rest = rest[s_padded:]
            elif tag == 'T':
                args.append(True)
            elif tag == 'F':
                args.append(False)
        return address, args
    except Exception:
        return None, []


# ─────────────────────────────────────────────
#  Song State (shared with HTTP server)
# ─────────────────────────────────────────────

song_state = {
    "title": "",
    "url": "",
    "duration": 0,
    "elapsed": 0,
    "playing": False,
}

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

PRESETS_FILE = BASE_DIR / "presets.json"
CONFIG_FILE  = BASE_DIR / "config.json"


DEFAULT_CONFIG = {
    "ip":   "127.0.0.1",
    "port": 9000,
    "http_port": 8765,
    "typing_indicator": True,
    "send_immediately": False,
}


# ─────────────────────────────────────────────
#  Colour / Style Tokens
# ─────────────────────────────────────────────

DARK   = "#0d0d0f"
PANEL  = "#16161a"
CARD   = "#1e1e24"
BORDER = "#2a2a35"
ACCENT = "#7c6aff"
ACCENT2= "#5eead4"
TEXT   = "#e8e8f0"
MUTED  = "#6b6b80"
SUCCESS= "#22c55e"
WARN   = "#f59e0b"
ERR    = "#ef4444"
HOVER  = "#252530"
SEL    = "#2d2b50"

FONT_MAIN  = ("Segoe UI", 10)
FONT_BOLD  = ("Segoe UI", 10, "bold")
FONT_TITLE = ("Segoe UI", 13, "bold")
FONT_SMALL = ("Segoe UI", 9)
FONT_MONO  = ("Consolas", 10)
FONT_HUGE  = ("Segoe UI", 16, "bold")


# ─────────────────────────────────────────────
#  Variable Substitution
# ─────────────────────────────────────────────

COAST_TZ = {
    "west": ("PST", "PDT", -8, -7),
    "east": ("EST", "EDT", -5, -4),
}

def is_dst(dt: datetime.datetime) -> bool:
    import calendar
    # US DST: 2nd Sunday March → 1st Sunday Nov
    y = dt.year
    # 2nd Sunday March
    march_first = datetime.datetime(y, 3, 1)
    days_to_sun = (6 - march_first.weekday()) % 7
    dst_start = datetime.datetime(y, 3, 1 + days_to_sun + 7, 2, 0)
    # 1st Sunday November
    nov_first = datetime.datetime(y, 11, 1)
    days_to_sun2 = (6 - nov_first.weekday()) % 7
    dst_end = datetime.datetime(y, 11, 1 + days_to_sun2, 2, 0)
    return dst_start <= dt < dst_end

def get_coast_time() -> str:
    utc = datetime.datetime.utcnow()
    dst = is_dst(utc)
    # West (Pacific)
    off_w = COAST_TZ["west"][3] if dst else COAST_TZ["west"][2]
    tz_w  = COAST_TZ["west"][1] if dst else COAST_TZ["west"][0]
    # East (Eastern)
    off_e = COAST_TZ["east"][3] if dst else COAST_TZ["east"][2]
    tz_e  = COAST_TZ["east"][1] if dst else COAST_TZ["east"][0]
    t_w = (utc + datetime.timedelta(hours=off_w)).strftime("%I:%M %p")
    t_e = (utc + datetime.timedelta(hours=off_e)).strftime("%I:%M %p")
    return f"{t_w} {tz_w}  /  {t_e} {tz_e}"

def fmt_duration(secs: int) -> str:
    m, s = divmod(max(0, int(secs)), 60)
    return f"{m}:{s:02d}"

def resolve_vars(text: str, muted: bool, engine_on: bool = False) -> str:
    text = text.replace("{mute}", "🔇Muted" if muted else "🔊Live")
    text = text.replace("{time}", get_coast_time())
    remaining = song_state["duration"] - song_state["elapsed"]
    song_str = f"♪ {song_state['title']} [{fmt_duration(remaining)}]" if song_state["title"] else "♪ Nothing Playing"
    text = text.replace("{song}", song_str)
    text = text.replace(r" \|\ ", "  |  ")
    engine_tag = "⚙ OSC Quest Engine\nBy -service-"
    text = text.replace("{engine}", engine_tag if engine_on else "")
    if engine_on and "{engine}" not in text:
        text = text.rstrip() + "\n" + engine_tag
    return text





# Pending items to push to the web player queue
_pending_queue: list = []

def resolve_media_url(url: str) -> list:
    """
    Given a URL, return a list of {id, title, artist, thumb, source, url} dicts.
    Handles: YT video, YT playlist, SoundCloud track/playlist.
    """
    url = url.strip()
    results = []

    # ── YouTube playlist ──────────────────────────────────────────────────
    yt_pl = re.search(r'[?&]list=([A-Za-z0-9_-]+)', url)
    if yt_pl and 'youtube.com' in url or 'youtu.be' in url:
        pl_id = yt_pl.group(1)
        try:
            pl_url = f"https://www.youtube.com/playlist?list={pl_id}"
            req = urllib.request.Request(pl_url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode('utf-8', errors='replace')
            data = _parse_ytInitialData(html)
            if data:
                try:
                    items = (data['contents']['twoColumnBrowseResultsRenderer']
                                 ['tabs'][0]['tabRenderer']['content']
                                 ['sectionListRenderer']['contents'][0]
                                 ['itemSectionRenderer']['contents'][0]
                                 ['playlistVideoListRenderer']['contents'])
                    for item in items:
                        v = item.get('playlistVideoRenderer', {})
                        vid = v.get('videoId', '')
                        if not vid:
                            continue
                        title = ''
                        try: title = v['title']['runs'][0]['text']
                        except Exception: pass
                        artist = ''
                        try: artist = v['shortBylineText']['runs'][0]['text']
                        except Exception: pass
                        results.append({
                            'id': vid, 'title': title, 'artist': artist,
                            'thumb': f'https://i.ytimg.com/vi/{vid}/mqdefault.jpg',
                            'source': 'youtube',
                            'url': f'https://www.youtube.com/watch?v={vid}',
                        })
                except Exception:
                    pass
        except Exception as e:
            print(f'[Playlist] YT playlist error: {e}')
        if results:
            return results

    # ── YouTube single video ──────────────────────────────────────────────
    yt_id = None
    m = (re.search(r'[?&]v=([A-Za-z0-9_-]{11})', url) or
         re.search(r'youtu\.be/([A-Za-z0-9_-]{11})', url) or
         re.search(r'embed/([A-Za-z0-9_-]{11})', url) or
         re.search(r'shorts/([A-Za-z0-9_-]{11})', url))
    if m:
        yt_id = m.group(1)
    elif re.match(r'^[A-Za-z0-9_-]{11}$', url):
        yt_id = url
    if yt_id:
        title, artist = '', ''
        try:
            oe = urllib.request.urlopen(
                f'https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={yt_id}&format=json',
                timeout=5)
            j = json.loads(oe.read())
            title  = j.get('title', '')
            artist = j.get('author_name', '')
        except Exception:
            pass
        return [{'id': yt_id, 'title': title or yt_id, 'artist': artist,
                 'thumb': f'https://i.ytimg.com/vi/{yt_id}/mqdefault.jpg',
                 'source': 'youtube',
                 'url': f'https://www.youtube.com/watch?v={yt_id}'}]

    # ── SoundCloud ────────────────────────────────────────────────────────
    if 'soundcloud.com' in url:
        try:
            oe_url = f'https://soundcloud.com/oembed?url={urllib.parse.quote(url)}&format=json'
            req = urllib.request.Request(oe_url, headers={'User-Agent': 'OSC-Quest-Engine'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                j = json.loads(resp.read())
            title  = j.get('title', '')
            artist = j.get('author_name', '')
            thumb  = j.get('thumbnail_url', '')
            return [{'id': url, 'title': title, 'artist': artist,
                     'thumb': thumb, 'source': 'soundcloud', 'url': url}]
        except Exception as e:
            print(f'[SoundCloud] oEmbed error: {e}')
            return [{'id': url, 'title': url, 'artist': '', 'thumb': '',
                     'source': 'soundcloud', 'url': url}]

    return []

# ─────────────────────────────────────────────
#  HTTP Server for YouTube Song Sync
# ─────────────────────────────────────────────

HTML_URL = "https://raw.githubusercontent.com/bingnut/Quest-OSC-engine/refs/heads/main/OSC%20HTML%20SERVER.html"
_html_cache = {"data": None, "etag": "", "version": 0}

def _fetch_html_once():
    """Fetch HTML from GitHub, update cache and bump version if changed."""
    try:
        req = urllib.request.Request(HTML_URL, headers={
            "User-Agent":   "OSC-Quest-Engine",
            "Cache-Control":"no-cache",
            "If-None-Match": _html_cache["etag"],
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
            new_etag = resp.headers.get("ETag", "")
            if data != _html_cache["data"]:
                _html_cache["data"]    = data
                _html_cache["etag"]    = new_etag
                _html_cache["version"] += 1
                print(f"[HTML] Updated → version {_html_cache['version']}")
    except urllib.error.HTTPError as e:
        if e.code != 304:
            print(f"[HTML] Fetch error: {e}")
    except Exception as e:
        print(f"[HTML] Fetch error: {e}")

def _html_poll_loop():
    while True:
        _fetch_html_once()
        time.sleep(5)

# Start background poller immediately
threading.Thread(target=_html_poll_loop, daemon=True).start()

def get_html_page() -> bytes:
    if _html_cache["data"]:
        return _html_cache["data"]
    return b"<h1>Loading... please refresh in a moment.</h1>"





def _extract_videos(items):
    results = []
    for item in items:
        v = item.get("videoRenderer")
        if not v:
            continue
        video_id = v.get("videoId", "")
        if not video_id:
            continue
        title = ""
        try: title = v["title"]["runs"][0]["text"]
        except Exception: pass
        duration_str = ""
        try: duration_str = v["lengthText"]["simpleText"]
        except Exception: pass
        channel = ""
        try: channel = v["ownerText"]["runs"][0]["text"]
        except Exception: pass
        views = ""
        try: views = v["viewCountText"]["simpleText"]
        except Exception: pass
        results.append({
            "id":       video_id,
            "title":    title,
            "duration": duration_str,
            "channel":  channel,
            "views":    views,
            "thumb":    f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
        })
    return results


def _extract_continuation(data):
    try:
        sections = (data["contents"]["twoColumnSearchResultsRenderer"]
                       ["primaryContents"]["sectionListRenderer"]["contents"])
        for section in sections:
            cr = section.get("continuationItemRenderer", {})
            token = (cr.get("continuationEndpoint", {})
                       .get("continuationCommand", {})
                       .get("token", ""))
            if token:
                return token
    except Exception:
        pass
    return ""


def _parse_ytInitialData(html):
    marker = "var ytInitialData = "
    idx = html.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    depth = 0
    end = start
    for i, ch in enumerate(html[start:], start):
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        return json.loads(html[start:end])
    except Exception:
        return None


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def youtube_search(query: str, continuation: str = "") -> dict:
    """Return {results, continuation} — continuation token enables load-more."""
    try:
        if continuation:
            api_url = "https://www.youtube.com/youtubei/v1/search"
            payload = json.dumps({
                "continuation": continuation,
                "context": {
                    "client": {
                        "clientName": "WEB",
                        "clientVersion": "2.20231121.09.00",
                    }
                }
            }).encode()
            req = urllib.request.Request(api_url, data=payload, headers={
                **HEADERS,
                "Content-Type": "application/json",
                "X-YouTube-Client-Name": "1",
                "X-YouTube-Client-Version": "2.20231121.09.00",
            })
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))

            results = []
            try:
                items = (data["onResponseReceivedCommands"][0]
                            ["appendContinuationItemsAction"]
                            ["continuationItems"])
                for item in items:
                    results.extend(_extract_videos(
                        item.get("itemSectionRenderer", {}).get("contents", [])
                    ))
            except Exception:
                pass

            next_token = ""
            try:
                items = (data["onResponseReceivedCommands"][0]
                            ["appendContinuationItemsAction"]
                            ["continuationItems"])
                for item in items:
                    cr = item.get("continuationItemRenderer", {})
                    t = (cr.get("continuationEndpoint", {})
                           .get("continuationCommand", {})
                           .get("token", ""))
                    if t:
                        next_token = t
                        break
            except Exception:
                pass

            return {"results": results, "continuation": next_token}

        else:
            q = urllib.parse.quote_plus(query)
            url = f"https://www.youtube.com/results?search_query={q}"
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read().decode("utf-8", errors="replace")

            data = _parse_ytInitialData(html)
            if not data:
                return {"results": [], "continuation": ""}

            try:
                contents = (
                    data["contents"]["twoColumnSearchResultsRenderer"]
                        ["primaryContents"]["sectionListRenderer"]["contents"]
                )
            except (KeyError, IndexError):
                return {"results": [], "continuation": ""}

            results = []
            for section in contents:
                results.extend(_extract_videos(
                    section.get("itemSectionRenderer", {}).get("contents", [])
                ))

            return {"results": results, "continuation": _extract_continuation(data)}

    except Exception as e:
        print(f"[Search] Error: {e}")
        return {"results": [], "continuation": ""}


class SongHTTPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # silence

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        _path = urllib.parse.urlparse(self.path).path
        if _path == "/api/song":
            self._json(song_state)
        elif _path == "/api/html-version":
            self._json({"version": _html_cache["version"]})
        elif _path == "/api/queue/poll":
            items = _pending_queue[:]
            _pending_queue.clear()
            self._json({"items": items})
        elif _path.startswith("/api/search"):
            self._handle_search()
        elif _path.startswith("/api/resolve"):
            self._handle_resolve()
        elif _path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._cors()
            self.end_headers()
            self.wfile.write(get_html_page())
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}
        if self.path == "/api/song":
            song_state.update(body)
            self._json({"ok": True})
        elif self.path == "/api/queue/push":
            url = body.get("url", "").strip()
            if url:
                def _resolve():
                    items = resolve_media_url(url)
                    _pending_queue.extend(items)
                threading.Thread(target=_resolve, daemon=True).start()
            self._json({"ok": True})

    def _handle_resolve(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            url = params.get("url", [""])[0].strip()
            if not url:
                self._json({"items": [], "error": "No URL"})
                return
            items = resolve_media_url(url)
            self._json({"items": items})
        except Exception as e:
            self._json({"items": [], "error": str(e)})

    def _handle_search(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            query        = params.get("q", [""])[0].strip()
            continuation = params.get("continuation", [""])[0].strip()
            if not query and not continuation:
                self._json({"results": [], "error": "No query"})
                return
            data = youtube_search(query, continuation)
            self._json(data)
        except Exception as e:
            self._json({"results": [], "error": str(e)})

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)


def start_http_server(port: int):
    try:
        socketserver.TCPServer.allow_reuse_address = True
        server = socketserver.TCPServer(("0.0.0.0", port), SongHTTPHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.daemon = True
        t.start()

        # Get real LAN IP by connecting a UDP socket (never sends data)
        try:
            import socket as _s
            s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = "127.0.0.1"

        url = f"http://{ip}:{port}"
        bar = "=" * 52
        print()
        print(bar)
        print(f"  ⚙  OSC Quest Engine — Player Server Running")
        print(bar)
        print(f"  ► Local:    http://localhost:{port}")
        print(f"  ► Network:  {url}")
        print(f"  ► Use {{song}} in chatbox to show now playing")
        print(bar)
        print()
        return True
    except OSError as e:
        print(f"[OSC Quest Engine] HTTP server FAILED on port {port}: {e}")
        return False
    except Exception as e:
        print(f"[OSC Quest Engine] HTTP server error: {e}")
        return False


# ─────────────────────────────────────────────
#  Song Elapsed Timer
# ─────────────────────────────────────────────

def _song_tick():
    while True:
        time.sleep(1)
        if song_state["playing"] and song_state["duration"] > 0:
            song_state["elapsed"] = min(song_state["elapsed"] + 1, song_state["duration"])


# ─────────────────────────────────────────────
#  Unicode Picker Popup
# ─────────────────────────────────────────────

UNICODE_CATEGORIES = {
    "Emoticons": "😀😁😂🤣😃😄😅😆😇😈😉😊😋😌😍😎😏😐😑😒😓😔😕😖😗😘😙😚😛😜😝😞😟😠😡😢😣😤😥😦😧😨😩😪😫😬😭😮😯😰😱😲😳😴😵😶😷",
    "Hearts": "❤️🧡💛💚💙💜🖤🤍🤎💔💕💞💓💗💖💘💝💟❣️",
    "Music":  "🎵🎶🎼🎹🎸🎺🎻🥁🎷🎤🎧🎙️🎚️🎛️📻🔊🔉🔈🔇🔔🔕🎵♬♫♩♪",
    "Stars":  "⭐🌟💫✨🌠🌌🌃🌉🌠☀️🌤️⛅🌥️🌦️🌧️⛈️🌩️🌨️❄️☃️⛄🌀🌈",
    "Symbols":"✅❎🔴🟠🟡🟢🔵🟣⚫⚪🟤🔶🔷🔸🔹🔺🔻💠🔘🔲🔳▪️▫️◾◽◼️◻️⬛⬜",
    "VRC":    "👋🤚🖐️✋🖖🤙👌🤌🤏✌️🤞🖖🤟🤘🤙👈👉👆🖕👇☝️👍👎✊👊🤛🤜",
    "Misc":   "🌸🌺🌻🌼🌷🌹🥀🌾🍀🍁🍂🍃🎋🎍🎑🎃🎄🎆🎇🧨✨🎉🎊🎈🎀🎁",
    "Arrows": "←→↑↓↖↗↘↙↔↕➡⬅⬆⬇↩↪⤴⤵🔄🔃🔁🔂🔀▶⏩⏭⏯◀⏪⏮⏫⏬⏸⏹⏺🎦",
    "Box":    "─│┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬╭╮╯╰┄┆╌╎┈┊",
}


class UnicodePicker(tk.Toplevel):
    def __init__(self, parent, insert_cb):
        super().__init__(parent)
        self.insert_cb = insert_cb
        self.title("Unicode Picker  [Alt+Y]")
        self.configure(bg=DARK)
        self.resizable(True, True)
        self.geometry("540x440")
        self._build()
        self.grab_set()

    def _build(self):
        # Search bar
        top = tk.Frame(self, bg=DARK, pady=8, padx=10)
        top.pack(fill="x")
        tk.Label(top, text="Search:", bg=DARK, fg=MUTED, font=FONT_SMALL).pack(side="left")
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._refresh())
        entry = tk.Entry(top, textvariable=self._search_var, bg=CARD, fg=TEXT,
                         insertbackground=TEXT, relief="flat", font=FONT_MAIN,
                         bd=0, highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT)
        entry.pack(side="left", fill="x", expand=True, padx=(8, 0))
        entry.focus_set()

        # Category tabs
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=8, pady=(0,8))
        self._frames = {}
        for cat, chars in UNICODE_CATEGORIES.items():
            f = tk.Frame(self._nb, bg=CARD)
            self._nb.add(f, text=cat)
            self._frames[cat] = (f, chars)
        self._rendered = {}
        self._nb.bind("<<NotebookTabChanged>>", lambda _: self._refresh())
        self._refresh()

    def _refresh(self):
        query = self._search_var.get().lower()
        idx = self._nb.index("current")
        cat = list(UNICODE_CATEGORIES.keys())[idx]
        frame, chars = self._frames[cat]
        for w in frame.winfo_children():
            w.destroy()
        chars_list = [c for c in chars if query in c.lower() or not query]
        row_frame = None
        for i, ch in enumerate(chars_list):
            if i % 12 == 0:
                row_frame = tk.Frame(frame, bg=CARD)
                row_frame.pack(fill="x", padx=6, pady=2)
            btn = tk.Button(row_frame, text=ch, bg=CARD, fg=TEXT, relief="flat",
                            font=("Segoe UI Emoji", 16), cursor="hand2", width=2,
                            activebackground=SEL, activeforeground=TEXT,
                            command=lambda c=ch: self._pick(c))
            btn.pack(side="left", padx=2, pady=2)

    def _pick(self, char):
        self.insert_cb(char)


# ─────────────────────────────────────────────
#  Custom Widgets
# ─────────────────────────────────────────────

class SidebarButton(tk.Frame):
    def __init__(self, parent, text, icon, command, **kw):
        super().__init__(parent, bg=PANEL, cursor="hand2", **kw)
        self.command = command
        self._active = False
        self._icon_lbl = tk.Label(self, text=icon, bg=PANEL, fg=MUTED,
                                  font=("Segoe UI Emoji", 16), width=2)
        self._icon_lbl.pack(side="left", padx=(12, 6), pady=10)
        self._text_lbl = tk.Label(self, text=text, bg=PANEL, fg=MUTED,
                                  font=FONT_BOLD, anchor="w")
        self._text_lbl.pack(side="left", fill="x", expand=True)
        self._bar = tk.Frame(self, bg=PANEL, width=4)
        self._bar.pack(side="right", fill="y")
        for w in (self, self._icon_lbl, self._text_lbl, self._bar):
            w.bind("<Button-1>", lambda _: self.command())
            w.bind("<Enter>", self._hover_on)
            w.bind("<Leave>", self._hover_off)

    def _hover_on(self, _=None):
        if not self._active:
            for w in (self, self._icon_lbl, self._text_lbl): w.config(bg=HOVER)

    def _hover_off(self, _=None):
        if not self._active:
            for w in (self, self._icon_lbl, self._text_lbl): w.config(bg=PANEL)

    def set_active(self, active: bool):
        self._active = active
        if active:
            for w in (self, self._icon_lbl, self._text_lbl): w.config(bg=SEL)
            self._icon_lbl.config(fg=ACCENT)
            self._text_lbl.config(fg=TEXT)
            self._bar.config(bg=ACCENT)
        else:
            for w in (self, self._icon_lbl, self._text_lbl): w.config(bg=PANEL)
            self._icon_lbl.config(fg=MUTED)
            self._text_lbl.config(fg=MUTED)
            self._bar.config(bg=PANEL)


class StyledEntry(tk.Entry):
    def __init__(self, parent, **kw):
        kw.setdefault("bg", CARD)
        kw.setdefault("fg", TEXT)
        kw.setdefault("insertbackground", TEXT)
        kw.setdefault("relief", "flat")
        kw.setdefault("font", FONT_MONO)
        kw.setdefault("bd", 0)
        kw.setdefault("highlightthickness", 1)
        kw.setdefault("highlightbackground", BORDER)
        kw.setdefault("highlightcolor", ACCENT)
        super().__init__(parent, **kw)


class StyledButton(tk.Button):
    def __init__(self, parent, text, color=ACCENT, fg=TEXT, **kw):
        super().__init__(parent, text=text, bg=color, fg=fg,
                         activebackground=color, activeforeground=fg,
                         relief="flat", cursor="hand2", font=FONT_BOLD,
                         bd=0, padx=16, pady=8, **kw)
        self.bind("<Enter>", lambda _: self.config(bg=self._lighten(color)))
        self.bind("<Leave>", lambda _: self.config(bg=color))

    @staticmethod
    def _lighten(hex_col: str) -> str:
        r, g, b = int(hex_col[1:3], 16), int(hex_col[3:5], 16), int(hex_col[5:7], 16)
        r = min(255, r + 25); g = min(255, g + 25); b = min(255, b + 25)
        return f"#{r:02x}{g:02x}{b:02x}"


class Separator(tk.Frame):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=BORDER, height=1, **kw)


# ─────────────────────────────────────────────
#  Main App
# ─────────────────────────────────────────────

class VRCChatbox(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("OSC Quest Engine")
        self.configure(bg=DARK)
        self.geometry("980x680")
        self.minsize(780, 540)

        self.config_data = self._load_config()
        self.presets: list[dict] = []
        self._load_presets()
        self._muted = False
        self._send_loop_active = False
        self._send_interval = 5
        self._http_running = False
        self._osc_listener_running = False

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook",       background=PANEL, borderwidth=0)
        style.configure("TNotebook.Tab",   background=PANEL, foreground=MUTED,
                        font=FONT_BOLD, padding=[14, 8])
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT), ("active", HOVER)],
                  foreground=[("selected", "#fff"), ("active", TEXT)])
        style.configure("TScrollbar", background=CARD, troughcolor=DARK,
                        borderwidth=0, arrowsize=12)

        self._build_ui()
        self._show_tab("chatbox")
        self.bind("<Alt-y>", lambda _: self._open_unicode_picker())
        self.bind("<Alt-Y>", lambda _: self._open_unicode_picker())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Start HTTP server
        self._start_http()
        threading.Thread(target=_song_tick, daemon=True).start()

        # Poll song state from HTTP server periodically
        self._poll_song()

    # ── Config / Presets ──────────────────────

    def _load_config(self) -> dict:
        if CONFIG_FILE.exists():
            try:
                return {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text())}
            except Exception:
                pass
        cfg = dict(DEFAULT_CONFIG)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
        return cfg

    def _save_config(self):
        CONFIG_FILE.write_text(json.dumps(self.config_data, indent=2))

    def _load_presets(self):
        if PRESETS_FILE.exists():
            try:
                self.presets = json.loads(PRESETS_FILE.read_text())
                return
            except Exception:
                pass
        self.presets = []
        PRESETS_FILE.write_text(json.dumps(self.presets, indent=2))

    def _save_presets_file(self):
        PRESETS_FILE.write_text(json.dumps(self.presets, indent=2))

    # ── UI Build ──────────────────────────────

    def _build_ui(self):
        # Titlebar stripe
        titlebar = tk.Frame(self, bg=PANEL, height=52)
        titlebar.pack(fill="x", side="top")
        titlebar.pack_propagate(False)
        title_frame = tk.Frame(titlebar, bg=PANEL)
        title_frame.pack(side="left", padx=20)
        tk.Label(title_frame, text="◈  OSC Quest Engine", bg=PANEL, fg=TEXT,
                 font=FONT_TITLE).pack(anchor="w")
        tk.Label(title_frame, text="By -service-", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self._status_dot = tk.Label(titlebar, text="●", bg=PANEL, fg=MUTED,
                                    font=("Segoe UI", 18))
        self._status_dot.pack(side="right", padx=(0, 8))
        self._status_lbl = tk.Label(titlebar, text="Idle", bg=PANEL, fg=MUTED,
                                    font=FONT_SMALL)
        self._status_lbl.pack(side="right")
        tk.Button(titlebar, text="↺ Restart", bg=CARD, fg=MUTED,
                  relief="flat", font=FONT_SMALL, cursor="hand2",
                  activebackground=HOVER, padx=10, pady=4,
                  command=self._restart).pack(side="right", padx=(0, 12))
        Separator(self).pack(fill="x")

        # Body
        body = tk.Frame(self, bg=DARK)
        body.pack(fill="both", expand=True)

        # Sidebar
        self._sidebar = tk.Frame(body, bg=PANEL, width=200)
        self._sidebar.pack(side="left", fill="y")
        self._sidebar.pack_propagate(False)
        Separator(body).pack(side="left", fill="y")

        # Content area
        self._content = tk.Frame(body, bg=DARK)
        self._content.pack(side="left", fill="both", expand=True)

        self._tabs: dict[str, tk.Frame] = {}
        self._sidebar_btns: dict[str, SidebarButton] = {}
        self._build_sidebar()
        self._build_tab_chatbox()
        self._build_tab_presets()
        self._build_tab_macros()
        self._build_tab_song()
        self._build_tab_player()
        self._build_tab_ports()
        self._build_tab_output()

    def _build_sidebar(self):
        tk.Label(self._sidebar, text="NAVIGATION", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=16, pady=(16, 6))

        nav_items = [
            ("chatbox",  "Chatbox",  "💬"),
            ("presets",  "Presets",  "📋"),
            ("macros",   "Macros",   "⚡"),
            ("song",     "Song Sync","🎵"),
            ("player",   "Player",   "▶"),
            ("ports",    "Ports",    "🔌"),
            ("output",   "Output",   "📡"),
        ]
        for key, label, icon in nav_items:
            btn = SidebarButton(self._sidebar, label, icon,
                                command=lambda k=key: self._show_tab(k))
            btn.pack(fill="x")
            self._sidebar_btns[key] = btn

        Separator(self._sidebar).pack(fill="x", pady=12)
        tk.Label(self._sidebar, text="QUICK ACTIONS", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=16, pady=(0, 6))

        self._mute_btn = tk.Button(
            self._sidebar, text="🔊  Toggle Mute", bg=CARD, fg=TEXT,
            relief="flat", font=FONT_SMALL, cursor="hand2",
            activebackground=HOVER, padx=12, pady=6,
            command=self._toggle_mute)
        self._mute_btn.pack(fill="x", padx=10, pady=2)

        tk.Button(
            self._sidebar, text="Alt+Y  Unicode", bg=CARD, fg=TEXT,
            relief="flat", font=FONT_SMALL, cursor="hand2",
            activebackground=HOVER, padx=12, pady=6,
            command=self._open_unicode_picker).pack(fill="x", padx=10, pady=2)

        Separator(self._sidebar).pack(fill="x", pady=10)
        tk.Label(self._sidebar, text="ENGINE TAG", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=16, pady=(0, 4))
        self._engine_var = tk.BooleanVar(value=False)
        self._engine_toggle_btn = tk.Button(
            self._sidebar, text="⚙  Engine Tag: OFF", bg=CARD, fg=MUTED,
            relief="flat", font=FONT_SMALL, cursor="hand2",
            activebackground=HOVER, padx=12, pady=6,
            command=self._toggle_engine)
        self._engine_toggle_btn.pack(fill="x", padx=10, pady=2)
        self._engine_tag_lbl = tk.Label(
            self._sidebar, text="", bg=PANEL, fg=MUTED,
            font=("Segoe UI", 8), wraplength=170, justify="left")
        self._engine_tag_lbl.pack(anchor="w", padx=14, pady=(0, 4))

        # Version footer
        tk.Label(self._sidebar, text="OSC Quest Engine v1.0", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8)).pack(side="bottom", pady=10)

    # ── Tab: Chatbox ──────────────────────────

    def _build_tab_chatbox(self):
        f = tk.Frame(self._content, bg=DARK)
        self._tabs["chatbox"] = f

        # Header
        hdr = tk.Frame(f, bg=DARK, pady=20)
        hdr.pack(fill="x", padx=28)
        tk.Label(hdr, text="Chatbox", bg=DARK, fg=TEXT, font=FONT_HUGE).pack(side="left")
        tk.Label(hdr, text="Send messages via VRChat OSC", bg=DARK, fg=MUTED,
                 font=FONT_SMALL).pack(side="left", padx=(12, 0), pady=(6, 0))

        # Message editor card
        card = tk.Frame(f, bg=CARD, padx=20, pady=20,
                        highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x", padx=28, pady=(0, 16))

        tk.Label(card, text="Message Text", bg=CARD, fg=MUTED,
                 font=FONT_SMALL).pack(anchor="w", pady=(0, 6))

        txt_frame = tk.Frame(card, bg=CARD, highlightthickness=1,
                             highlightbackground=BORDER, highlightcolor=ACCENT)
        txt_frame.pack(fill="x")
        self._chatbox_text = tk.Text(
            txt_frame, bg=CARD, fg=TEXT, insertbackground=TEXT,
            relief="flat", font=FONT_MONO, height=4, wrap="word",
            selectbackground=SEL, selectforeground=TEXT, bd=0)
        self._chatbox_text.pack(fill="both", padx=8, pady=8)
        self._chatbox_text.bind("<Control-Return>", lambda _: self._send_chatbox())
        txt_frame.bind("<FocusIn>", lambda _:
            txt_frame.config(highlightbackground=ACCENT))
        txt_frame.bind("<FocusOut>", lambda _:
            txt_frame.config(highlightbackground=BORDER))

        # Preview
        tk.Label(card, text="Preview", bg=CARD, fg=MUTED,
                 font=FONT_SMALL).pack(anchor="w", pady=(12, 4))
        self._preview_lbl = tk.Label(card, text="", bg=DARK, fg=ACCENT2,
                                     font=FONT_MONO, anchor="w", padx=10, pady=6,
                                     wraplength=700, justify="left")
        self._preview_lbl.pack(fill="x")
        self._chatbox_text.bind("<KeyRelease>", lambda _: self._update_preview())
        self._update_preview_loop()

        # Controls row
        ctrl = tk.Frame(f, bg=DARK)
        ctrl.pack(fill="x", padx=28, pady=(0, 8))

        StyledButton(ctrl, "Send  Ctrl+↵", command=self._send_chatbox).pack(side="left")
        StyledButton(ctrl, "Clear", color=CARD,
                     command=self._clear_chatbox).pack(side="left", padx=(8, 0))

        # Loop controls
        loop_card = tk.Frame(f, bg=CARD, padx=20, pady=16,
                             highlightthickness=1, highlightbackground=BORDER)
        loop_card.pack(fill="x", padx=28)
        lc_hdr = tk.Frame(loop_card, bg=CARD)
        lc_hdr.pack(fill="x")
        tk.Label(lc_hdr, text="Auto-Send Loop", bg=CARD, fg=TEXT,
                 font=FONT_BOLD).pack(side="left")
        self._loop_btn = StyledButton(lc_hdr, "Start Loop", color=SUCCESS,
                                      command=self._toggle_loop)
        self._loop_btn.pack(side="right")

        lc_body = tk.Frame(loop_card, bg=CARD)
        lc_body.pack(fill="x", pady=(10, 0))
        tk.Label(lc_body, text="Interval (s):", bg=CARD, fg=MUTED,
                 font=FONT_SMALL).pack(side="left")
        self._interval_var = tk.StringVar(value="5")
        StyledEntry(lc_body, textvariable=self._interval_var, width=6,
                    ).pack(side="left", padx=(8, 0))
        self._typing_var = tk.BooleanVar(value=self.config_data["typing_indicator"])
        tk.Checkbutton(lc_body, text="Show typing indicator", variable=self._typing_var,
                       bg=CARD, fg=MUTED, selectcolor=DARK, activebackground=CARD,
                       font=FONT_SMALL, relief="flat").pack(side="left", padx=16)

    # ── Tab: Presets ─────────────────────────

    def _build_tab_presets(self):
        f = tk.Frame(self._content, bg=DARK)
        self._tabs["presets"] = f

        hdr = tk.Frame(f, bg=DARK, pady=20)
        hdr.pack(fill="x", padx=28)
        tk.Label(hdr, text="Presets", bg=DARK, fg=TEXT, font=FONT_HUGE).pack(side="left")
        btn_row = tk.Frame(hdr, bg=DARK)
        btn_row.pack(side="right")
        StyledButton(btn_row, "＋ New",   command=self._preset_new).pack(side="left")
        StyledButton(btn_row, "⬆ Import", color=CARD,
                     command=self._preset_import).pack(side="left", padx=6)
        StyledButton(btn_row, "⬇ Export", color=CARD,
                     command=self._preset_export).pack(side="left")

        body = tk.Frame(f, bg=DARK)
        body.pack(fill="both", expand=True, padx=28)

        # List
        list_frame = tk.Frame(body, bg=CARD, width=220,
                              highlightthickness=1, highlightbackground=BORDER)
        list_frame.pack(side="left", fill="y", pady=(0, 16))
        list_frame.pack_propagate(False)
        tk.Label(list_frame, text="SAVED PRESETS", bg=CARD, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=12, pady=(12, 6))
        lb_scroll = tk.Scrollbar(list_frame, orient="vertical")
        lb_scroll.pack(side="right", fill="y")
        self._preset_lb = tk.Listbox(list_frame, bg=CARD, fg=TEXT,
                                     selectbackground=SEL, selectforeground=TEXT,
                                     relief="flat", bd=0, font=FONT_MAIN,
                                     yscrollcommand=lb_scroll.set,
                                     activestyle="none", highlightthickness=0)
        self._preset_lb.pack(fill="both", expand=True, padx=6)
        lb_scroll.config(command=self._preset_lb.yview)
        self._preset_lb.bind("<<ListboxSelect>>", self._preset_select)

        # Editor
        ed = tk.Frame(body, bg=DARK, padx=20)
        ed.pack(side="left", fill="both", expand=True)
        tk.Label(ed, text="Name", bg=DARK, fg=MUTED, font=FONT_SMALL).pack(anchor="w")
        self._preset_name_var = tk.StringVar()
        StyledEntry(ed, textvariable=self._preset_name_var).pack(fill="x", pady=(4, 12))
        tk.Label(ed, text="Message", bg=DARK, fg=MUTED, font=FONT_SMALL).pack(anchor="w")
        txt_wrap = tk.Frame(ed, bg=DARK, highlightthickness=1,
                            highlightbackground=BORDER, highlightcolor=ACCENT)
        txt_wrap.pack(fill="x", pady=4)
        self._preset_text = tk.Text(txt_wrap, bg=CARD, fg=TEXT, insertbackground=TEXT,
                                    relief="flat", font=FONT_MONO, height=5, bd=0,
                                    selectbackground=SEL, wrap="word")
        self._preset_text.pack(fill="both", padx=8, pady=8)

        act_row = tk.Frame(ed, bg=DARK, pady=10)
        act_row.pack(fill="x")
        StyledButton(act_row, "💾 Save",  command=self._preset_save).pack(side="left")
        StyledButton(act_row, "🗑 Delete", color=ERR,
                     command=self._preset_delete).pack(side="left", padx=6)
        StyledButton(act_row, "▶ Send",  color=SUCCESS,
                     command=self._preset_send_selected).pack(side="left")

        self._refresh_preset_list()

    # ── Tab: Macros ───────────────────────────

    def _build_tab_macros(self):
        f = tk.Frame(self._content, bg=DARK)
        self._tabs["macros"] = f

        hdr = tk.Frame(f, bg=DARK, pady=20)
        hdr.pack(fill="x", padx=28)
        tk.Label(hdr, text="Macro Reference", bg=DARK, fg=TEXT,
                 font=FONT_HUGE).pack(side="left")

        rows = [
            ("{mute}",    "Mute status icon",        "🔇Muted / 🔊Live"),
            ("{time}",    "Current coast time",       "01:45 PM PST  /  04:45 PM EST"),
            ("{song}",    "Now playing + time left",  "♪ Song Title [2:31]"),
            ("{engine}",  "OSC Quest Engine tag",     "⚙ OSC Quest Engine"),
            (r" \|\ ",    "Vertical separator gap",   "  |  "),
            ("Alt+Y",     "Open Unicode picker",      "Inserts any Unicode char"),
            ("Ctrl+↵",    "Send chatbox message",     "Keyboard shortcut to send"),
        ]

        for var, desc, ex in rows:
            card = tk.Frame(f, bg=CARD, padx=20, pady=14,
                            highlightthickness=1, highlightbackground=BORDER)
            card.pack(fill="x", padx=28, pady=4)
            tk.Label(card, text=var, bg=CARD, fg=ACCENT, font=FONT_MONO,
                     width=14, anchor="w").pack(side="left")
            tk.Label(card, text=desc, bg=CARD, fg=TEXT, font=FONT_MAIN,
                     width=30, anchor="w").pack(side="left", padx=12)
            tk.Label(card, text=f"→ {ex}", bg=CARD, fg=MUTED, font=FONT_SMALL,
                     anchor="w").pack(side="left")
            tk.Button(card, text="Copy", bg=DARK, fg=MUTED, relief="flat",
                      font=FONT_SMALL, cursor="hand2", activebackground=HOVER,
                      padx=8, pady=2,
                      command=lambda v=var: self._copy_macro(v)).pack(side="right")

    # ── Tab: Song Sync ────────────────────────

    def _build_tab_song(self):
        f = tk.Frame(self._content, bg=DARK)
        self._tabs["song"] = f

        hdr = tk.Frame(f, bg=DARK, pady=20)
        hdr.pack(fill="x", padx=28)
        tk.Label(hdr, text="Song Sync", bg=DARK, fg=TEXT, font=FONT_HUGE).pack(side="left")

        # Status card
        sc = tk.Frame(f, bg=CARD, padx=20, pady=16,
                      highlightthickness=1, highlightbackground=BORDER)
        sc.pack(fill="x", padx=28, pady=(0, 16))
        sc_top = tk.Frame(sc, bg=CARD)
        sc_top.pack(fill="x")
        tk.Label(sc_top, text="HTTP Server Status", bg=CARD, fg=MUTED,
                 font=FONT_SMALL).pack(side="left")
        self._http_status = tk.Label(sc_top, text="●", bg=CARD, fg=ERR, font=("Segoe UI", 16))
        self._http_status.pack(side="right")
        self._http_url_lbl = tk.Label(sc, text="", bg=CARD, fg=ACCENT, font=FONT_MONO)
        self._http_url_lbl.pack(anchor="w", pady=(6, 0))

        # Now playing card
        np = tk.Frame(f, bg=CARD, padx=20, pady=16,
                      highlightthickness=1, highlightbackground=BORDER)
        np.pack(fill="x", padx=28, pady=(0, 16))
        np_hdr = tk.Frame(np, bg=CARD)
        np_hdr.pack(fill="x")
        tk.Label(np_hdr, text="Now Playing", bg=CARD, fg=MUTED, font=FONT_SMALL).pack(side="left")
        self._ext_badge = tk.Label(np_hdr, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 8))
        self._ext_badge.pack(side="right")
        self._np_title = tk.Label(np, text="Nothing Playing", bg=CARD, fg=TEXT,
                                  font=("Segoe UI", 14, "bold"))
        self._np_title.pack(anchor="w", pady=(6, 2))
        self._np_time = tk.Label(np, text="", bg=CARD, fg=MUTED, font=FONT_MONO)
        self._np_time.pack(anchor="w")
        self._np_url_lbl = tk.Label(np, text="", bg=CARD, fg=ACCENT, font=("Segoe UI", 9),
                                    cursor="hand2")
        self._np_url_lbl.pack(anchor="w", pady=(2, 0))

        # Manual set
        man = tk.Frame(f, bg=CARD, padx=20, pady=16,
                       highlightthickness=1, highlightbackground=BORDER)
        man.pack(fill="x", padx=28)
        tk.Label(man, text="Set Manually", bg=CARD, fg=TEXT, font=FONT_BOLD).pack(anchor="w")
        row1 = tk.Frame(man, bg=CARD, pady=8)
        row1.pack(fill="x")
        tk.Label(row1, text="Title:", bg=CARD, fg=MUTED, font=FONT_SMALL, width=8).pack(side="left")
        self._song_title_var = tk.StringVar()
        StyledEntry(row1, textvariable=self._song_title_var).pack(side="left", fill="x", expand=True)
        row2 = tk.Frame(man, bg=CARD, pady=4)
        row2.pack(fill="x")
        tk.Label(row2, text="Duration:", bg=CARD, fg=MUTED, font=FONT_SMALL, width=8).pack(side="left")
        self._song_dur_var = tk.StringVar()
        StyledEntry(row2, textvariable=self._song_dur_var, width=10).pack(side="left")
        tk.Label(row2, text="(mm:ss)", bg=CARD, fg=MUTED, font=FONT_SMALL).pack(side="left", padx=6)
        btn_row = tk.Frame(man, bg=CARD, pady=8)
        btn_row.pack(fill="x")
        StyledButton(btn_row, "▶ Set Playing", color=SUCCESS,
                     command=self._set_song_manual).pack(side="left")
        StyledButton(btn_row, "⏹ Stop", color=ERR,
                     command=self._stop_song).pack(side="left", padx=8)

    # ── Tab: Ports ────────────────────────────

    def _build_tab_ports(self):
        f = tk.Frame(self._content, bg=DARK)
        self._tabs["ports"] = f

        hdr = tk.Frame(f, bg=DARK, pady=20)
        hdr.pack(fill="x", padx=28)
        tk.Label(hdr, text="Ports & Network", bg=DARK, fg=TEXT,
                 font=FONT_HUGE).pack(side="left")

        def section(title, hint=""):
            c = tk.Frame(f, bg=CARD, padx=20, pady=16,
                         highlightthickness=1, highlightbackground=BORDER)
            c.pack(fill="x", padx=28, pady=6)
            hrow = tk.Frame(c, bg=CARD)
            hrow.pack(fill="x")
            tk.Label(hrow, text=title, bg=CARD, fg=TEXT, font=FONT_BOLD).pack(side="left")
            if hint:
                tk.Label(hrow, text=hint, bg=CARD, fg=MUTED, font=FONT_SMALL).pack(side="left", padx=8)
            return c

        # OSC
        osc_card = section("VRChat OSC Target", "Default: 127.0.0.1:9000")
        r1 = tk.Frame(osc_card, bg=CARD, pady=8); r1.pack(fill="x")
        tk.Label(r1, text="IP Address:", bg=CARD, fg=MUTED, font=FONT_SMALL,
                 width=14, anchor="w").pack(side="left")
        self._ip_var = tk.StringVar(value=self.config_data["ip"])
        StyledEntry(r1, textvariable=self._ip_var, width=20).pack(side="left")

        r2 = tk.Frame(osc_card, bg=CARD, pady=4); r2.pack(fill="x")
        tk.Label(r2, text="OSC Port:", bg=CARD, fg=MUTED, font=FONT_SMALL,
                 width=14, anchor="w").pack(side="left")
        self._port_var = tk.StringVar(value=str(self.config_data["port"]))
        StyledEntry(r2, textvariable=self._port_var, width=10).pack(side="left")

        # HTTP
        http_card = section("Song Sync HTTP Server", "Accessible from any device on your network")
        r3 = tk.Frame(http_card, bg=CARD, pady=8); r3.pack(fill="x")
        tk.Label(r3, text="HTTP Port:", bg=CARD, fg=MUTED, font=FONT_SMALL,
                 width=14, anchor="w").pack(side="left")
        self._http_port_var = tk.StringVar(value=str(self.config_data["http_port"]))
        StyledEntry(r3, textvariable=self._http_port_var, width=10).pack(side="left")

        # OSC Receive
        recv_card = section("OSC Receive (from VRChat)", "VRChat sends on port 9001 by default")
        r4 = tk.Frame(recv_card, bg=CARD, pady=8); r4.pack(fill="x")
        tk.Label(r4, text="Listen Port:", bg=CARD, fg=MUTED, font=FONT_SMALL,
                 width=14, anchor="w").pack(side="left")
        self._recv_port_var = tk.StringVar(value="9001")
        StyledEntry(r4, textvariable=self._recv_port_var, width=10).pack(side="left")
        r4b = tk.Frame(recv_card, bg=CARD, pady=6); r4b.pack(fill="x")
        self._recv_toggle_btn = tk.Button(
            r4b, text="▶  Start Listening", bg=SUCCESS, fg="#fff",
            relief="flat", font=FONT_SMALL, cursor="hand2",
            padx=12, pady=5, command=self._toggle_osc_listener)
        self._recv_toggle_btn.pack(side="left")
        self._recv_status = tk.Label(r4b, text="Not listening", bg=CARD, fg=MUTED, font=FONT_SMALL)
        self._recv_status.pack(side="left", padx=10)

        # Test + Save
        act = tk.Frame(f, bg=DARK, padx=28, pady=12)
        act.pack(fill="x")
        StyledButton(act, "💾 Save Settings", command=self._ports_save).pack(side="left")
        StyledButton(act, "🔗 Test OSC", color=CARD,
                     command=self._osc_test).pack(side="left", padx=8)
        self._port_status = tk.Label(act, text="", bg=DARK, fg=SUCCESS, font=FONT_SMALL)
        self._port_status.pack(side="left")

        # Info card
        info = section("Connection Info")
        try:
            import socket as _s
            sock = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            local_ip = sock.getsockname()[0]
            sock.close()
        except Exception:
            local_ip = "127.0.0.1"
        tk.Label(info, text=f"Local IP: {local_ip}", bg=CARD, fg=ACCENT,
                 font=FONT_MONO).pack(anchor="w", pady=2)
        tk.Label(info, text="Set VRChat OSC Receive Port to 9000 in VRC settings.",
                 bg=CARD, fg=MUTED, font=FONT_SMALL).pack(anchor="w")

    def _build_tab_player(self):
        f = tk.Frame(self._content, bg=DARK)
        self._tabs["player"] = f

        hdr = tk.Frame(f, bg=DARK, pady=20)
        hdr.pack(fill="x", padx=28)
        tk.Label(hdr, text="Send to Player", bg=DARK, fg=TEXT,
                 font=FONT_HUGE).pack(side="left")
        tk.Label(hdr, text="Pushes to the web player at your HTTP server",
                 bg=DARK, fg=MUTED, font=FONT_SMALL).pack(side="left", padx=12)

        def section(title, hint=""):
            c = tk.Frame(f, bg=CARD, padx=20, pady=16,
                         highlightthickness=1, highlightbackground=BORDER)
            c.pack(fill="x", padx=28, pady=6)
            hrow = tk.Frame(c, bg=CARD); hrow.pack(fill="x")
            tk.Label(hrow, text=title, bg=CARD, fg=TEXT, font=FONT_BOLD).pack(side="left")
            if hint:
                tk.Label(hrow, text=hint, bg=CARD, fg=MUTED, font=FONT_SMALL).pack(side="left", padx=8)
            return c

        # URL input
        url_card = section("Add to Queue", "YouTube video, playlist, or SoundCloud URL")
        ur = tk.Frame(url_card, bg=CARD, pady=8); ur.pack(fill="x")
        tk.Label(ur, text="URL:", bg=CARD, fg=MUTED, font=FONT_SMALL,
                 width=10, anchor="w").pack(side="left")
        self._player_url_var = tk.StringVar()
        url_entry = StyledEntry(ur, textvariable=self._player_url_var, width=55)
        url_entry.pack(side="left", padx=(0, 8))

        def _send():
            url = self._player_url_var.get().strip()
            if not url:
                return
            self._player_status.config(text="Resolving...", fg=MUTED)
            def _do():
                items = resolve_media_url(url)
                if items:
                    _pending_queue.extend(items)
                    n = len(items)
                    self.after(0, lambda: self._player_status.config(
                        text=f"✓ {n} item{'s' if n>1 else ''} queued", fg=SUCCESS))
                    self.after(0, lambda: self._player_url_var.set(""))
                    self.after(0, self._player_refresh_queue)
                else:
                    self.after(0, lambda: self._player_status.config(
                        text="✗ Could not resolve URL", fg=ERR))
            threading.Thread(target=_do, daemon=True).start()

        url_entry.bind("<Return>", lambda e: _send())
        StyledButton(ur, "▶ Add to Queue", command=_send).pack(side="left")
        self._player_status = tk.Label(url_card, text="", bg=CARD, fg=MUTED, font=FONT_SMALL)
        self._player_status.pack(anchor="w", pady=(4,0))

        # Pending queue display
        q_card = section("Pending Queue", "Items waiting to be picked up by the web player")
        qf = tk.Frame(q_card, bg=CARD); qf.pack(fill="x")
        self._player_queue_frame = tk.Frame(qf, bg=CARD)
        self._player_queue_frame.pack(fill="x")
        tk.Label(q_card, text="Items are consumed when the web player polls /api/queue/poll",
                 bg=CARD, fg=MUTED, font=FONT_SMALL).pack(anchor="w", pady=(6,0))

        act = tk.Frame(f, bg=DARK, padx=28, pady=8); act.pack(fill="x")
        StyledButton(act, "🗑 Clear Queue",
                     command=lambda: (_pending_queue.clear(), self._player_refresh_queue())).pack(side="left")

        self._player_refresh_queue()

    def _player_refresh_queue(self):
        for w in self._player_queue_frame.winfo_children():
            w.destroy()
        if not _pending_queue:
            tk.Label(self._player_queue_frame, text="Queue is empty",
                     bg=CARD, fg=MUTED, font=FONT_SMALL).pack(anchor="w", pady=4)
            return
        for i, item in enumerate(_pending_queue[:12]):
            row = tk.Frame(self._player_queue_frame, bg=CARD)
            row.pack(fill="x", pady=2)
            src_icon = "🎵" if item.get("source") == "soundcloud" else "▶"
            tk.Label(row, text=f"{src_icon}  {item.get('title','?')}",
                     bg=CARD, fg=TEXT, font=FONT_SMALL).pack(side="left")
            if item.get("artist"):
                tk.Label(row, text=f"— {item['artist']}",
                         bg=CARD, fg=MUTED, font=FONT_SMALL).pack(side="left", padx=6)
        if len(_pending_queue) > 12:
            tk.Label(self._player_queue_frame,
                     text=f"... and {len(_pending_queue)-12} more",
                     bg=CARD, fg=MUTED, font=FONT_SMALL).pack(anchor="w")

    def _build_tab_output(self):
        f = tk.Frame(self._content, bg=DARK)
        self._tabs["output"] = f

        hdr = tk.Frame(f, bg=DARK, pady=20)
        hdr.pack(fill="x", padx=28)
        tk.Label(hdr, text="OSC Output", bg=DARK, fg=TEXT,
                 font=FONT_HUGE).pack(side="left")
        tk.Button(hdr, text="🗑 Clear", bg=CARD, fg=MUTED,
                  relief="flat", font=FONT_SMALL, cursor="hand2",
                  activebackground=HOVER, padx=10, pady=4,
                  command=self._clear_output).pack(side="right")

        log_frame = tk.Frame(f, bg=CARD, highlightthickness=1,
                             highlightbackground=BORDER)
        log_frame.pack(fill="both", expand=True, padx=28, pady=(0, 20))

        self._output_text = tk.Text(
            log_frame, bg=CARD, fg=TEXT, font=FONT_MONO,
            relief="flat", state="disabled", wrap="word",
            selectbackground=ACCENT, padx=12, pady=10)
        self._output_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(log_frame, command=self._output_text.yview)
        sb.pack(side="right", fill="y")
        self._output_text.config(yscrollcommand=sb.set)
        self._output_text.tag_configure("addr", foreground=ACCENT, font=FONT_BOLD)
        self._output_text.tag_configure("val",  foreground=ACCENT2)
        self._output_text.tag_configure("time", foreground=MUTED, font=FONT_SMALL)

    def _clear_output(self):
        self._output_text.config(state="normal")
        self._output_text.delete("1.0", tk.END)
        self._output_text.config(state="disabled")

    def _append_output(self, address: str, args: list):
        def _do():
            self._output_text.config(state="normal")
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self._output_text.insert(tk.END, f"[{ts}]  ", "time")
            self._output_text.insert(tk.END, address, "addr")
            if args:
                self._output_text.insert(tk.END,
                    "  →  " + "  ".join(str(a) for a in args), "val")
            self._output_text.insert(tk.END, "\n")
            self._output_text.see(tk.END)
            self._output_text.config(state="disabled")
        self.after(0, _do)

    def _toggle_osc_listener(self):
        if self._osc_listener_running:
            self._osc_listener_running = False
            self._recv_toggle_btn.config(text="▶  Start Listening", bg=SUCCESS)
            self._recv_status.config(text="Not listening", fg=MUTED)
        else:
            try:
                port = int(self._recv_port_var.get().strip())
            except ValueError:
                port = 9001
            self._osc_listener_running = True
            self._recv_toggle_btn.config(text="⏹  Stop Listening", bg=ERR)
            self._recv_status.config(text=f"Listening on :{port}…", fg=SUCCESS)
            threading.Thread(target=self._osc_listener_worker,
                             args=(port,), daemon=True).start()

    def _osc_listener_worker(self, port: int):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            sock.settimeout(0.5)
        except Exception as e:
            self.after(0, lambda: self._recv_status.config(
                text=f"Error: {e}", fg=ERR))
            self._osc_listener_running = False
            self.after(0, lambda: self._recv_toggle_btn.config(
                text="▶  Start Listening", bg=SUCCESS))
            return
        while self._osc_listener_running:
            try:
                data, _ = sock.recvfrom(4096)
                address, args = parse_osc_message(data)
                if address:
                    self._append_output(address, args)
            except socket.timeout:
                continue
            except Exception:
                break
        sock.close()

    def _restart(self):
        self.destroy()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ── Tab Navigation ────────────────────────

    def _show_tab(self, key: str):
        for k, f in self._tabs.items():
            f.pack_forget()
            self._sidebar_btns[k].set_active(False)
        self._tabs[key].pack(fill="both", expand=True)
        self._sidebar_btns[key].set_active(True)

    # ── OSC / Send ─────────────────────────────

    def _get_ip_port(self):
        return self.config_data["ip"], int(self.config_data["port"])

    def _send_osc_message(self, text: str):
        ip, port = self._get_ip_port()
        resolved = resolve_vars(text, self._muted, self._engine_var.get())
        try:
            if self._typing_var.get():
                send_osc(ip, port, "/chatbox/typing", True)
                time.sleep(0.5)
            send_osc(ip, port, "/chatbox/input", resolved, True, False)
            self._set_status("Sent", SUCCESS)
        except Exception as e:
            self._set_status(f"Error: {e}", ERR)

    def _clear_chatbox(self):
        self._chatbox_text.delete("1.0", tk.END)
        self._update_preview()

    def _send_chatbox(self):
        text = self._chatbox_text.get("1.0", "end-1c")
        if not text.strip():
            return
        threading.Thread(target=self._send_osc_message, args=(text,), daemon=True).start()

    def _toggle_loop(self):
        if self._send_loop_active:
            self._send_loop_active = False
            self._loop_btn.config(text="Start Loop", bg=SUCCESS)
            self._set_status("Loop stopped", MUTED)
        else:
            try:
                self._send_interval = int(self._interval_var.get())
            except ValueError:
                self._send_interval = 5
            self._send_loop_active = True
            self._loop_btn.config(text="Stop Loop", bg=ERR)
            threading.Thread(target=self._loop_worker, daemon=True).start()

    def _loop_worker(self):
        while self._send_loop_active:
            text = self._chatbox_text.get("1.0", "end-1c")
            if text.strip():
                self._send_osc_message(text)
            for _ in range(self._send_interval * 10):
                if not self._send_loop_active:
                    return
                time.sleep(0.1)

    def _osc_test(self):
        def _test():
            try:
                ip, port = self._get_ip_port()
                send_osc(ip, port, "/chatbox/input", "VRC Chatbox — Connection Test ✓", True, False)
                self.after(0, lambda: self._port_status.config(text="✓ OSC packet sent!", fg=SUCCESS))
            except Exception as e:
                self.after(0, lambda: self._port_status.config(text=f"✗ {e}", fg=ERR))
        threading.Thread(target=_test, daemon=True).start()

    # ── Status ────────────────────────────────

    def _set_status(self, msg: str, color: str = MUTED):
        self.after(0, lambda: self._status_lbl.config(text=msg, fg=color))
        self.after(0, lambda: self._status_dot.config(fg=color))

    # ── Preview ───────────────────────────────

    def _update_preview(self):
        text = self._chatbox_text.get("1.0", "end-1c")
        resolved = resolve_vars(text, self._muted, self._engine_var.get())
        self._preview_lbl.config(text=resolved if resolved else "Preview will appear here…")

    def _update_preview_loop(self):
        self._update_preview()
        self.after(2000, self._update_preview_loop)

    # ── Mute ─────────────────────────────────

    def _toggle_mute(self):
        self._muted = not self._muted
        label = "🔇  Muted" if self._muted else "🔊  Toggle Mute"
        color = ERR if self._muted else CARD
        self._mute_btn.config(text=label, bg=color)
        self._update_preview()

    def _toggle_engine(self):
        new_val = not self._engine_var.get()
        self._engine_var.set(new_val)
        if new_val:
            self._engine_toggle_btn.config(
                text="⚙  Engine Tag: ON", bg=ACCENT, fg=TEXT)
            self._engine_tag_lbl.config(
                text='Appends "⚙ OSC Quest Engine" to every message', fg=ACCENT2)
        else:
            self._engine_toggle_btn.config(
                text="⚙  Engine Tag: OFF", bg=CARD, fg=MUTED)
            self._engine_tag_lbl.config(text="", fg=MUTED)
        self._update_preview()

    # ── Presets ───────────────────────────────

    def _refresh_preset_list(self):
        self._preset_lb.delete(0, "end")
        for p in self.presets:
            self._preset_lb.insert("end", f"  {p['name']}")

    def _preset_select(self, _=None):
        sel = self._preset_lb.curselection()
        if not sel:
            return
        idx = sel[0]
        p = self.presets[idx]
        self._preset_name_var.set(p["name"])
        self._preset_text.delete("1.0", "end")
        self._preset_text.insert("1.0", p["text"])

    def _preset_new(self):
        self._preset_lb.selection_clear(0, "end")
        self._preset_name_var.set("New Preset")
        self._preset_text.delete("1.0", "end")

    def _preset_save(self):
        name = self._preset_name_var.get().strip()
        text = self._preset_text.get("1.0", "end-1c")
        if not name:
            messagebox.showwarning("Warning", "Preset must have a name.")
            return
        sel = self._preset_lb.curselection()
        if sel:
            idx = sel[0]
            self.presets[idx] = {"name": name, "text": text}
        else:
            self.presets.append({"name": name, "text": text})
        self._save_presets_file()
        self._refresh_preset_list()

    def _preset_delete(self):
        sel = self._preset_lb.curselection()
        if not sel:
            return
        idx = sel[0]
        if messagebox.askyesno("Confirm", f"Delete '{self.presets[idx]['name']}'?"):
            del self.presets[idx]
            self._save_presets_file()
            self._refresh_preset_list()

    def _preset_send_selected(self):
        text = self._preset_text.get("1.0", "end-1c")
        if text.strip():
            threading.Thread(target=self._send_osc_message, args=(text,), daemon=True).start()

    def _preset_import(self):
        path = filedialog.askopenfilename(
            title="Import Presets", filetypes=[("JSON Files", "*.json"), ("All", "*.*")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text())
            if isinstance(data, list):
                self.presets.extend(data)
            elif isinstance(data, dict) and "presets" in data:
                self.presets.extend(data["presets"])
            self._save_presets_file()
            self._refresh_preset_list()
            messagebox.showinfo("Imported", f"Imported {len(data)} presets.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _preset_export(self):
        path = filedialog.asksaveasfilename(
            title="Export Presets", defaultextension=".json",
            filetypes=[("JSON Files", "*.json")])
        if not path:
            return
        Path(path).write_text(json.dumps(self.presets, indent=2))
        messagebox.showinfo("Exported", f"Saved to {path}")

    # ── Macros ────────────────────────────────

    def _copy_macro(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)
        self._set_status(f"Copied: {text}", ACCENT)

    # ── Song ─────────────────────────────────

    def _set_song_manual(self):
        title = self._song_title_var.get().strip()
        dur_str = self._song_dur_var.get().strip()
        try:
            parts = dur_str.split(":")
            dur = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else int(parts[0])
        except Exception:
            dur = 0
        song_state.update({"title": title, "duration": dur, "elapsed": 0, "playing": True})

    def _stop_song(self):
        song_state.update({"title": "", "duration": 0, "elapsed": 0, "playing": False})

    def _poll_song(self):
        if song_state["title"]:
            self._np_title.config(text=song_state["title"])
            rem = song_state["duration"] - song_state["elapsed"]
            self._np_time.config(
                text=f"⏱ {fmt_duration(song_state['elapsed'])} / {fmt_duration(song_state['duration'])}  —  {fmt_duration(rem)} remaining")
            # Show URL if pushed by extension
            url = song_state.get("url", "")
            if url:
                short = url[:60] + ("…" if len(url) > 60 else "")
                self._np_url_lbl.config(text=f"🔗 {short}")
                self._ext_badge.config(text="⚙ via Chrome Extension", fg=ACCENT2)
            else:
                self._np_url_lbl.config(text="")
                self._ext_badge.config(text="manual", fg=MUTED)
        else:
            self._np_title.config(text="Nothing Playing")
            self._np_time.config(text="")
            self._np_url_lbl.config(text="")
            self._ext_badge.config(text="", fg=MUTED)
        self.after(1000, self._poll_song)

    # ── HTTP ─────────────────────────────────

    def _start_http(self):
        port = self.config_data["http_port"]
        ok = start_http_server(port)
        self._http_running = ok
        if ok:
            try:
                import socket as _s
                s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                s.close()
            except Exception:
                ip = "localhost"
            url = f"http://{ip}:{port}"
            self.after(0, lambda: self._http_status.config(fg=SUCCESS))
            self.after(0, lambda: self._http_url_lbl.config(
                text=f"🌐 {url}  (accessible on your local network)"))
        else:
            self.after(0, lambda: self._http_status.config(fg=ERR))
            self.after(0, lambda: self._http_url_lbl.config(
                text="Failed to start — port may be in use"))

    # ── Ports Save ────────────────────────────

    def _ports_save(self):
        self.config_data["ip"]        = self._ip_var.get().strip()
        self.config_data["port"]      = int(self._port_var.get().strip())
        self.config_data["http_port"] = int(self._http_port_var.get().strip())
        self._save_config()
        self._port_status.config(text="✓ Settings saved", fg=SUCCESS)

    # ── Unicode Picker ────────────────────────

    def _open_unicode_picker(self):
        def insert(ch):
            try:
                self._chatbox_text.insert("insert", ch)
            except Exception:
                pass
        UnicodePicker(self, insert)

    # ── Close ─────────────────────────────────

    def _on_close(self):
        self._send_loop_active = False
        self._save_config()
        self.destroy()


# ─────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    app = VRCChatbox()
    app.mainloop()
