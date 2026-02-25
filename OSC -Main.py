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

# Config and presets are stored in-memory only (no files)

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
    engine_tag = "⚙ OSC Quest Engine"
    text = text.replace("{engine}", engine_tag if engine_on else "")
    if engine_on and "{engine}" not in text:
        text = text.rstrip() + "\n" + engine_tag
    return text


# ─────────────────────────────────────────────
#  HTTP Server for YouTube Song Sync
# ─────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OSC Quest Engine — Player</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --dark:#0d0d0f;--panel:#16161a;--card:#1e1e24;--border:#2a2a35;
  --accent:#7c6aff;--accent2:#5eead4;--text:#e8e8f0;--muted:#6b6b80;
  --success:#22c55e;--err:#ef4444;--hover:#252530;--sel:#2d2b50;
}
html,body{height:100%;background:var(--dark);color:var(--text);
  font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;overflow:hidden}
.app{display:grid;grid-template-rows:52px 1fr;height:100vh}

/* ── Topbar ── */
.topbar{background:var(--panel);border-bottom:1px solid var(--border);
  display:flex;align-items:center;padding:0 20px;gap:12px}
.topbar h1{font-size:14px;font-weight:700}
.topbar-sub{font-size:11px;color:var(--muted)}
.pill{font-size:11px;font-weight:600;padding:4px 11px;border-radius:99px;
  background:var(--card);color:var(--muted);border:1px solid var(--border);
  transition:all .2s;white-space:nowrap;margin-left:auto}
.pill.ok {background:#14532d;color:var(--success);border-color:#166534}
.pill.err{background:#450a0a;color:var(--err);border-color:#7f1d1d}

/* ── Body: player | search | queue ── */
.body{display:grid;grid-template-columns:1fr 260px 220px;overflow:hidden}

/* ── Player pane ── */
.player-pane{display:flex;flex-direction:column;background:#000;overflow:hidden}
.player-wrap{position:relative;flex:1;min-height:0;background:#000}
#yt-player{position:absolute;inset:0;width:100%;height:100%;border:none}
.placeholder{position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:10px;color:var(--muted);
  font-size:13px;pointer-events:none}
.placeholder-icon{font-size:52px;opacity:.15;line-height:1}

/* ── Controls ── */
.controls{background:var(--panel);border-top:1px solid var(--border);
  padding:12px 18px 14px;flex-shrink:0}
.np-row{display:flex;align-items:center;gap:7px;margin-bottom:3px}
.live-dot{width:7px;height:7px;border-radius:50%;background:var(--muted);
  flex-shrink:0;transition:background .3s}
.live-dot.on{background:var(--success);box-shadow:0 0 7px var(--success);
  animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.np-label{font-size:10px;font-weight:700;letter-spacing:.8px;
  color:var(--muted);text-transform:uppercase}
.np-title{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;color:var(--text);margin-bottom:9px}
.np-title.empty{color:var(--muted);font-weight:400;font-style:italic}
.prog-row{display:flex;align-items:center;gap:10px;margin-bottom:9px}
.prog-track{flex:1;height:5px;background:var(--border);border-radius:99px;
  overflow:hidden;cursor:pointer;transition:height .1s}
.prog-track:hover{height:7px}
.prog-fill{height:100%;width:0%;
  background:linear-gradient(90deg,var(--accent),var(--accent2));
  border-radius:99px;pointer-events:none;transition:width .5s linear}
.prog-time{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;
  white-space:nowrap;min-width:88px;text-align:right}
.btn-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.btn{background:var(--card);border:1px solid var(--border);color:var(--text);
  border-radius:8px;padding:6px 12px;font-size:12px;font-weight:500;
  cursor:pointer;white-space:nowrap;transition:background .12s,border-color .12s}
.btn:hover{background:var(--hover);border-color:var(--accent)}
.btn:active{transform:scale(.97)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{background:#6a55e8}
.btn.danger{background:#1a0808;border-color:var(--err);color:var(--err)}
.btn.danger:hover{background:#2d0d0d}
.vol-group{margin-left:auto;display:flex;align-items:center;gap:7px}
.vol-icon{font-size:15px;color:var(--muted);cursor:pointer}
.vol-icon:hover{color:var(--text)}
input[type=range]{-webkit-appearance:none;appearance:none;width:80px;height:4px;
  border-radius:99px;background:var(--border);outline:none;cursor:pointer}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;
  width:13px;height:13px;border-radius:50%;background:var(--accent);cursor:pointer}

/* ── Shared sidebar column styles ── */
.col{display:flex;flex-direction:column;overflow:hidden;border-left:1px solid var(--border)}
.col-head{padding:10px 12px 8px;border-bottom:1px solid var(--border);flex-shrink:0;background:var(--panel)}
.col-title{font-size:10px;font-weight:700;letter-spacing:.9px;
  color:var(--muted);text-transform:uppercase;margin-bottom:8px}
.col-scroll{flex:1;overflow-y:auto;padding:8px}
.col-empty{text-align:center;color:var(--muted);font-size:12px;padding:24px 8px;line-height:1.6}

/* ── Search column ── */
.search-row{display:flex;gap:6px}
.search-field{flex:1;min-width:0;background:var(--card);border:1px solid var(--border);
  color:var(--text);border-radius:8px;padding:7px 9px;font-size:12px;
  outline:none;transition:border-color .2s}
.search-field:focus{border-color:var(--accent)}
.search-field::placeholder{color:var(--muted)}
.search-btn{background:var(--accent);border:none;color:#fff;border-radius:8px;
  padding:7px 11px;font-size:13px;font-weight:700;cursor:pointer;flex-shrink:0;
  transition:background .15s}
.search-btn:hover{background:#6a55e8}

/* ── Result card (3-line) ── */
.r-card{display:flex;flex-direction:column;gap:0;
  background:var(--card);border:1px solid var(--border);border-radius:9px;
  overflow:hidden;margin-bottom:8px;cursor:pointer;
  transition:border-color .15s,box-shadow .15s}
.r-card:hover{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.r-thumb-wrap{position:relative;width:100%;aspect-ratio:16/9;
  background:var(--border);overflow:hidden;flex-shrink:0}
.r-thumb-wrap img{width:100%;height:100%;object-fit:cover;display:block}
.r-thumb-wrap .r-dur{position:absolute;bottom:5px;right:6px;
  background:rgba(0,0,0,.78);color:#fff;font-size:10px;font-weight:700;
  padding:2px 5px;border-radius:4px;font-variant-numeric:tabular-nums}
.r-body{padding:7px 9px 8px}
.r-title{font-size:11px;font-weight:600;color:var(--text);
  line-height:1.35;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden;margin-bottom:4px}
.r-meta-row{display:flex;align-items:center;justify-content:space-between;gap:6px}
.r-channel{font-size:10px;color:var(--muted);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;flex:1}
.r-add-btn{background:var(--accent);border:none;color:#fff;border-radius:6px;
  padding:3px 9px;font-size:10px;font-weight:700;cursor:pointer;
  flex-shrink:0;transition:background .12s}
.r-add-btn:hover{background:#6a55e8}

/* ── Spinner ── */
.spinner{text-align:center;color:var(--muted);font-size:12px;padding:28px 0}
.spin{display:inline-block;width:18px;height:18px;border:2px solid var(--border);
  border-top-color:var(--accent);border-radius:50%;
  animation:spin .7s linear infinite;margin-bottom:6px}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── Queue column ── */
.q-col-head{display:flex;align-items:center;justify-content:space-between}
.q-clear{background:none;border:1px solid var(--border);color:var(--muted);
  border-radius:6px;padding:3px 8px;font-size:10px;cursor:pointer;
  transition:all .12s}
.q-clear:hover{border-color:var(--err);color:var(--err)}

/* ── Queue card (3-line) ── */
.q-card{display:flex;gap:8px;background:var(--card);border:1px solid var(--border);
  border-radius:9px;padding:8px;margin-bottom:6px;cursor:pointer;
  transition:border-color .15s;position:relative}
.q-card:hover{border-color:var(--accent)}
.q-card.current{border-color:var(--accent);background:var(--sel)}
.q-card.current::before{content:'';position:absolute;left:0;top:0;bottom:0;
  width:3px;background:var(--accent);border-radius:9px 0 0 9px}
.q-num{font-size:10px;color:var(--muted);font-weight:700;
  width:14px;text-align:center;flex-shrink:0;padding-top:1px}
.q-card.current .q-num{color:var(--accent)}
.q-thumb-sm{width:52px;height:34px;border-radius:5px;background:var(--border);
  overflow:hidden;flex-shrink:0;display:flex;align-items:center;
  justify-content:center;color:var(--muted);font-size:13px}
.q-thumb-sm img{width:100%;height:100%;object-fit:cover;display:block}
.q-info{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.q-title-sm{font-size:11px;font-weight:600;color:var(--text);
  line-height:1.3;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden}
.q-card.current .q-title-sm{color:var(--accent2)}
.q-meta-sm{font-size:10px;color:var(--muted)}
.q-rm{position:absolute;top:5px;right:6px;background:none;border:none;
  color:var(--muted);cursor:pointer;font-size:12px;opacity:0;
  transition:opacity .12s,color .12s;line-height:1}
.q-card:hover .q-rm{opacity:1}
.q-rm:hover{color:var(--err)}

/* ── Toast ── */
.toast{position:fixed;bottom:20px;left:50%;
  transform:translateX(-50%) translateY(50px);
  background:var(--card);border:1px solid var(--border);color:var(--text);
  padding:9px 18px;border-radius:8px;font-size:12px;font-weight:600;
  transition:transform .22s,opacity .22s;opacity:0;z-index:200;
  white-space:nowrap;pointer-events:none}
.toast.show{transform:translateX(-50%) translateY(0);opacity:1}
.toast.ok {border-color:var(--success);color:var(--success)}
.toast.err{border-color:var(--err);color:var(--err)}

::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:99px}
</style>
</head>
<body>
<div class="app">

  <!-- Topbar -->
  <div class="topbar">
    <span style="font-size:20px">&#9881;</span>
    <div>
      <h1>OSC Quest Engine</h1>
      <div class="topbar-sub">YouTube Player &amp; Chatbox Sync</div>
    </div>
    <div class="pill" id="sync-pill">Not synced</div>
  </div>

  <div class="body">

    <!-- ── Player pane ── -->
    <div class="player-pane">
      <div class="player-wrap">
        <div class="placeholder" id="placeholder">
          <div class="placeholder-icon">&#9654;</div>
          <div>Search for a video in the sidebar to begin</div>
        </div>
        <div id="yt-player"></div>
      </div>
      <div class="controls">
        <div class="np-row">
          <div class="live-dot" id="live-dot"></div>
          <div class="np-label">Now Playing</div>
        </div>
        <div class="np-title empty" id="np-title">Nothing loaded</div>
        <div class="prog-row">
          <div class="prog-track" id="prog-track">
            <div class="prog-fill" id="prog-fill"></div>
          </div>
          <div class="prog-time" id="prog-time">0:00 / 0:00</div>
        </div>
        <div class="btn-row">
          <button class="btn primary" id="play-btn" onclick="togglePlay()">&#9654; Play</button>
          <button class="btn" onclick="skipRel(-10)">&#8592; 10s</button>
          <button class="btn" onclick="skipRel(10)">10s &#8594;</button>
          <button class="btn" onclick="prevTrack()">&#9664;&#9664;</button>
          <button class="btn" onclick="nextTrack()">&#9654;&#9654;</button>
          <button class="btn danger" onclick="stopAll()">&#9632; Stop</button>
          <div class="vol-group">
            <span class="vol-icon" id="vol-icon" onclick="toggleMute()">&#128266;</span>
            <input type="range" id="vol-slider" min="0" max="100" value="80" oninput="setVol(this.value)">
          </div>
        </div>
      </div>
    </div>

    <!-- ── Search column ── -->
    <div class="col">
      <div class="col-head">
        <div class="col-title">Search YouTube</div>
        <div class="search-row">
          <input class="search-field" id="search-input" type="text"
            placeholder="Song, artist, or paste URL..."
            onkeydown="if(event.key==='Enter') handleInput()">
          <button class="search-btn" onclick="handleInput()">&#9654;</button>
        </div>
      </div>
      <div class="col-scroll" id="results-scroll">
        <div class="col-empty" id="results-empty">
          &#128269; Search above to find videos<br>
          <span style="font-size:10px;opacity:.6">or paste a YouTube URL</span>
        </div>
        <div id="results-list"></div>
      </div>
    </div>

    <!-- ── Queue column ── -->
    <div class="col">
      <div class="col-head">
        <div class="col-title">
          <div class="q-col-head">
            Queue <span id="queue-count" style="color:var(--accent);margin-left:4px"></span>
            <button class="q-clear" onclick="clearQueue()">Clear</button>
          </div>
        </div>
      </div>
      <div class="col-scroll" id="queue-scroll">
        <div id="queue-list">
          <div class="col-empty">
            &#127911; Add videos from search<br>
            <span style="font-size:10px;opacity:.6">They'll play in order</span>
          </div>
        </div>
      </div>
    </div>

  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ── State ─────────────────────────────────────────────────────────────────
var player=null, playerReady=false;
var queue=[], currentIdx=-1, muted=false;
var lastPushed={}, syncTimer=null, progTimer=null;
var SERVER = window.location.origin;

// ── YouTube IFrame API ────────────────────────────────────────────────────
window.onYouTubeIframeAPIReady = function(){
  player = new YT.Player('yt-player', {
    height:'100%', width:'100%',
    playerVars:{autoplay:0,controls:1,rel:0,modestbranding:1,iv_load_policy:3},
    events:{onReady:onPlayerReady,onStateChange:onPlayerState,onError:onPlayerError}
  });
};

function onPlayerReady(){
  playerReady=true;
  player.setVolume(parseInt(document.getElementById('vol-slider').value,10));
  startProgTimer();
  startSyncTimer();
}

function onPlayerState(e){
  var playing=e.data===YT.PlayerState.PLAYING;
  document.getElementById('play-btn').textContent=playing?'\u23F8 Pause':'\u25B6 Play';
  document.getElementById('live-dot').classList.toggle('on',playing);
  lastPushed={};pushSync();
  if(e.data===YT.PlayerState.ENDED) nextTrack();
}

function onPlayerError(e){
  var m={2:'Invalid ID',5:'HTML5 error',100:'Not found',101:'Embeds disabled',150:'Embeds disabled'};
  toast('\u26A0 '+(m[e.data]||'Error '+e.data),'err');
}

// ── Input: URL or search ──────────────────────────────────────────────────
function handleInput(){
  var raw=document.getElementById('search-input').value.trim();
  if(!raw) return;
  var id=extractId(raw);
  if(id){
    // Direct URL
    var item={id:id,title:'Loading\u2026',duration:'',channel:'',
              thumb:'https://i.ytimg.com/vi/'+id+'/mqdefault.jpg'};
    addToQueue(item);
    document.getElementById('search-input').value='';
    fetch('https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v='+id+'&format=json')
      .then(function(r){return r.ok?r.json():Promise.reject()})
      .then(function(d){
        var q=queue.find(function(x){return x.id===id});
        if(q){q.title=d.title||q.title;renderQueue();}
      }).catch(function(){});
  } else {
    doSearch(raw);
  }
}

function extractId(raw){
  var m=raw.match(/[?&]v=([a-zA-Z0-9_-]{11})/)||
        raw.match(/youtu\.be\/([a-zA-Z0-9_-]{11})/)||
        raw.match(/embed\/([a-zA-Z0-9_-]{11})/)||
        raw.match(/shorts\/([a-zA-Z0-9_-]{11})/);
  if(m) return m[1];
  if(/^[a-zA-Z0-9_-]{11}$/.test(raw)) return raw;
  return null;
}

// ── Search ────────────────────────────────────────────────────────────────
function doSearch(query){
  document.getElementById('results-empty').style.display='none';
  document.getElementById('results-list').innerHTML=
    '<div class="spinner"><div class="spin"></div><br>Searching\u2026</div>';
  fetch(SERVER+'/api/search?q='+encodeURIComponent(query))
    .then(function(r){return r.json()})
    .then(function(d){renderResults(d.results||[])})
    .catch(function(){
      document.getElementById('results-list').innerHTML=
        '<div class="col-empty" style="color:var(--err)">\u2715 Search failed.<br>Is the server running?</div>';
    });
}

function renderResults(results){
  var el=document.getElementById('results-list');
  if(!results.length){
    el.innerHTML='<div class="col-empty">No results found</div>';
    return;
  }
  el.innerHTML=results.map(function(r){
    // Encode for onclick attribute
    var safe=JSON.stringify(r).replace(/'/g,"&#39;");
    return '<div class="r-card" onclick=\'addResult('+safe+')\'>'
      +'<div class="r-thumb-wrap">'
        +'<img src="'+r.thumb+'" alt="" loading="lazy" onerror="this.style.display=\'none\'">'
        +(r.duration?'<div class="r-dur">'+esc(r.duration)+'</div>':'')
      +'</div>'
      +'<div class="r-body">'
        +'<div class="r-title">'+esc(r.title)+'</div>'
        +'<div class="r-meta-row">'
          +'<div class="r-channel">'+esc(r.channel||'')+'</div>'
          +'<button class="r-add-btn" onclick="event.stopPropagation();addResult('+safe+')">+ Add</button>'
        +'</div>'
      +'</div>'
      +'</div>';
  }).join('');
}

function addResult(r){
  addToQueue(r);
  toast('\u2713 '+r.title.slice(0,28)+'\u2026 added','ok');
}

// ── Queue ─────────────────────────────────────────────────────────────────
function addToQueue(item){
  if(queue.some(function(q){return q.id===item.id})){toast('Already in queue');return;}
  queue.push(item);
  renderQueue();
  if(currentIdx===-1) playAt(queue.length-1);
}

function playAt(idx){
  if(!playerReady||idx<0||idx>=queue.length) return;
  currentIdx=idx;
  var item=queue[idx];
  document.getElementById('placeholder').style.display='none';
  player.loadVideoById(item.id);
  setNPTitle(item.title);
  renderQueue();
  updateQueueCount();
  lastPushed={};
}

function prevTrack(){if(currentIdx>0) playAt(currentIdx-1);}
function nextTrack(){
  if(currentIdx+1<queue.length) playAt(currentIdx+1);
  else stopAll();
}

function removeFromQueue(idx,e){
  if(e) e.stopPropagation();
  var was=idx===currentIdx;
  queue.splice(idx,1);
  if(was){
    currentIdx=-1;
    if(queue.length) playAt(Math.min(idx,queue.length-1));
    else stopAll();
  } else if(idx<currentIdx) currentIdx--;
  renderQueue();
  updateQueueCount();
}

function clearQueue(){queue=[];currentIdx=-1;stopAll();renderQueue();updateQueueCount();}

function renderQueue(){
  var el=document.getElementById('queue-list');
  if(!queue.length){
    el.innerHTML='<div class="col-empty">&#127911; Add videos from search<br><span style="font-size:10px;opacity:.6">They\'ll play in order</span></div>';
    return;
  }
  el.innerHTML=queue.map(function(item,i){
    return '<div class="q-card'+(i===currentIdx?' current':'')+'" onclick="playAt('+i+')">'
      +'<div class="q-num">'+(i===currentIdx?'&#9654;':(i+1))+'</div>'
      +'<div class="q-thumb-sm">'
        +(item.thumb?'<img src="'+item.thumb+'" alt="" onerror="this.style.display=\'none\'">':'&#9654;')
      +'</div>'
      +'<div class="q-info">'
        +'<div class="q-title-sm">'+esc(item.title)+'</div>'
        +'<div class="q-meta-sm">'+(item.duration||item.channel||'&nbsp;')+'</div>'
      +'</div>'
      +'<button class="q-rm" onclick="removeFromQueue('+i+',event)" title="Remove">&#x2715;</button>'
      +'</div>';
  }).join('');
  // Auto-scroll current item into view
  if(currentIdx>=0){
    var card=el.querySelectorAll('.q-card')[currentIdx];
    if(card) card.scrollIntoView({block:'nearest',behavior:'smooth'});
  }
}

function updateQueueCount(){
  var el=document.getElementById('queue-count');
  el.textContent=queue.length?'('+queue.length+')':'';
}

// ── Player controls ───────────────────────────────────────────────────────
function togglePlay(){
  if(!playerReady) return;
  if(player.getPlayerState()===YT.PlayerState.PLAYING) player.pauseVideo();
  else player.playVideo();
}

function skipRel(d){
  if(!playerReady) return;
  player.seekTo(Math.max(0,player.getCurrentTime()+d),true);
  lastPushed={};
}

function stopAll(){
  if(playerReady) player.stopVideo();
  currentIdx=-1;
  document.getElementById('play-btn').textContent='\u25B6 Play';
  document.getElementById('live-dot').classList.remove('on');
  setNPTitle('');
  document.getElementById('prog-fill').style.width='0%';
  document.getElementById('prog-time').textContent='0:00 / 0:00';
  renderQueue();
  pushStop();
}

function setVol(v){
  if(playerReady) player.setVolume(parseInt(v));
  localStorage.setItem('oqe_vol',v);
  document.getElementById('vol-icon').textContent=
    v==0?'\uD83D\uDD07':v<50?'\uD83D\uDD09':'\uD83D\uDD0A';
  if(muted&&v>0){muted=false;if(playerReady) player.unMute();}
}

function toggleMute(){
  if(!playerReady) return;
  muted=!muted;
  if(muted){player.mute();document.getElementById('vol-icon').textContent='\uD83D\uDD07';}
  else{player.unMute();var v=document.getElementById('vol-slider').value;
    document.getElementById('vol-icon').textContent=v<50?'\uD83D\uDD09':'\uD83D\uDD0A';}
}

document.getElementById('prog-track').addEventListener('click',function(e){
  if(!playerReady) return;
  var rect=this.getBoundingClientRect();
  var pct=Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width));
  player.seekTo(pct*(player.getDuration()||0),true);lastPushed={};
});

// ── Progress timer ────────────────────────────────────────────────────────
function startProgTimer(){
  if(progTimer) clearInterval(progTimer);
  progTimer=setInterval(function(){
    if(!playerReady) return;
    var el=player.getCurrentTime()||0, dur=player.getDuration()||0;
    document.getElementById('prog-fill').style.width=(dur>0?(el/dur*100):0)+'%';
    document.getElementById('prog-time').textContent=fmt(el)+' / '+fmt(dur);
    if(currentIdx>=0&&dur>0&&queue[currentIdx]&&!queue[currentIdx]._ds){
      queue[currentIdx].duration=fmt(dur);queue[currentIdx]._ds=true;renderQueue();
    }
  },500);
}

// ── Sync ──────────────────────────────────────────────────────────────────
function startSyncTimer(){
  if(syncTimer) clearInterval(syncTimer);
  syncTimer=setInterval(pushSync,2000);
}

async function pushSync(){
  if(!playerReady) return;
  var playing=player.getPlayerState()===YT.PlayerState.PLAYING;
  var elapsed=Math.floor(player.getCurrentTime()||0);
  var dur=Math.floor(player.getDuration()||0);
  var item=queue[currentIdx];
  var title=item?item.title:'', url=item?'https://www.youtube.com/watch?v='+item.id:'';
  if(lastPushed.title===title&&lastPushed.playing===playing&&
     Math.abs((lastPushed.elapsed||0)-elapsed)<3) return;
  lastPushed={title,playing,elapsed};
  try{
    var r=await fetch(SERVER+'/api/song',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({title,url,elapsed,duration:dur,playing}),
      signal:AbortSignal.timeout(2000)});
    var j=await r.json();
    setPill(j.ok?'ok':'err',j.ok?'\u2713 Synced':'\u2715 Error');
  }catch(e){setPill('err','\u2715 Offline');}
}

async function pushStop(){
  lastPushed={};
  try{await fetch(SERVER+'/api/song',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:'',url:'',elapsed:0,duration:0,playing:false}),
    signal:AbortSignal.timeout(2000)});
    setPill('','Not synced');
  }catch(_){}
}

// ── Helpers ───────────────────────────────────────────────────────────────
function setNPTitle(t){
  var el=document.getElementById('np-title');
  if(t&&t!=='Loading\u2026'){el.textContent=t;el.classList.remove('empty');}
  else{el.textContent='Nothing loaded';el.classList.add('empty');}
}
function setPill(cls,text){
  var el=document.getElementById('sync-pill');
  el.className='pill'+(cls?' '+cls:'');el.textContent=text;
}
function fmt(s){
  if(!s||isNaN(s)||s<0) return '0:00';
  var m=Math.floor(s/60),ss=Math.floor(s%60);
  return m+':'+String(ss).padStart(2,'0');
}
function esc(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
var _tt;
function toast(msg,type){
  var el=document.getElementById('toast');
  el.textContent=msg;el.className='toast show'+(type?' '+type:'');
  clearTimeout(_tt);_tt=setTimeout(function(){el.classList.remove('show');},2600);
}

// Keyboard shortcuts
document.addEventListener('keydown',function(e){
  if(['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) return;
  if(e.code==='Space'){e.preventDefault();togglePlay();}
  if(e.code==='ArrowLeft') skipRel(-10);
  if(e.code==='ArrowRight') skipRel(10);
  if(e.code==='ArrowUp'){var s=document.getElementById('vol-slider');s.value=Math.min(100,+s.value+10);setVol(s.value);}
  if(e.code==='ArrowDown'){var s=document.getElementById('vol-slider');s.value=Math.max(0,+s.value-10);setVol(s.value);}
  if(e.code==='KeyM') toggleMute();
  if(e.code==='KeyN') nextTrack();
  if(e.code==='KeyP') prevTrack();
});

// Boot
document.getElementById('vol-slider').value=parseInt(localStorage.getItem('oqe_vol')||'80',10);
(function(){var t=document.createElement('script');t.src='https://www.youtube.com/iframe_api';document.head.appendChild(t);})();
</script>
</body>
</html>"""



def youtube_search(query: str, max_results: int = 10) -> list:
    """Scrape YouTube search results without an API key."""
    try:
        q   = urllib.parse.quote_plus(query)
        url = f"https://www.youtube.com/results?search_query={q}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # YouTube embeds initial data as a JS variable
        marker = "var ytInitialData = "
        idx = html.find(marker)
        if idx == -1:
            return []
        start = idx + len(marker)
        # Find the end of the JSON object by counting braces
        depth = 0
        end = start
        for i, ch in enumerate(html[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        data = json.loads(html[start:end])

        results = []
        try:
            contents = (
                data["contents"]["twoColumnSearchResultsRenderer"]
                    ["primaryContents"]["sectionListRenderer"]
                    ["contents"][0]["itemSectionRenderer"]["contents"]
            )
        except (KeyError, IndexError):
            return []

        for item in contents:
            v = item.get("videoRenderer")
            if not v:
                continue
            video_id = v.get("videoId", "")
            if not video_id:
                continue
            # Title
            title = ""
            try:
                title = v["title"]["runs"][0]["text"]
            except Exception:
                pass
            # Duration
            duration_str = ""
            try:
                duration_str = v["lengthText"]["simpleText"]
            except Exception:
                pass
            # Channel
            channel = ""
            try:
                channel = v["ownerText"]["runs"][0]["text"]
            except Exception:
                pass
            # View count
            views = ""
            try:
                views = v["viewCountText"]["simpleText"]
            except Exception:
                pass

            results.append({
                "id":       video_id,
                "title":    title,
                "duration": duration_str,
                "channel":  channel,
                "views":    views,
                "thumb":    f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
            })
            if len(results) >= max_results:
                break

        return results
    except Exception as e:
        print(f"[Search] Error: {e}")
        return []


class SongHTTPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # silence

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/song":
            self._json(song_state)
        elif self.path.startswith("/api/search"):
            self._handle_search()
        elif self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._cors()
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode())
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/api/song":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            song_state.update(body)
            self._json({"ok": True})

    def _handle_search(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            query  = params.get("q", [""])[0].strip()
            if not query:
                self._json({"results": [], "error": "No query"})
                return
            results = youtube_search(query)
            self._json({"results": results})
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
        return dict(DEFAULT_CONFIG)

    def _save_config(self):
        pass  # in-memory only

    def _load_presets(self):
        self.presets = []  # in-memory only

    def _save_presets_file(self):
        pass  # in-memory only

    # ── UI Build ──────────────────────────────

    def _build_ui(self):
        # Titlebar stripe
        titlebar = tk.Frame(self, bg=PANEL, height=52)
        titlebar.pack(fill="x", side="top")
        titlebar.pack_propagate(False)
        tk.Label(titlebar, text="◈  OSC Quest Engine", bg=PANEL, fg=TEXT,
                 font=FONT_TITLE).pack(side="left", padx=20)
        self._status_dot = tk.Label(titlebar, text="●", bg=PANEL, fg=MUTED,
                                    font=("Segoe UI", 18))
        self._status_dot.pack(side="right", padx=(0, 8))
        self._status_lbl = tk.Label(titlebar, text="Idle", bg=PANEL, fg=MUTED,
                                    font=FONT_SMALL)
        self._status_lbl.pack(side="right")
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
        self._build_tab_ports()

    def _build_sidebar(self):
        tk.Label(self._sidebar, text="NAVIGATION", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=16, pady=(16, 6))

        nav_items = [
            ("chatbox",  "Chatbox",  "💬"),
            ("presets",  "Presets",  "📋"),
            ("macros",   "Macros",   "⚡"),
            ("song",     "Song Sync","🎵"),
            ("ports",    "Ports",    "🔌"),
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
