#!/usr/bin/env python3
"""
brain.py — in-game companion service (Phase 2, v0.1)
====================================================

RetroArch's AI Service POSTs the current frame here when you press the
hotkey. This service asks Gemini to react to it in character, draws the
reply as a speech bubble, and hands the image back as an overlay.

    python3 brain.py            # foreground, logs to /tmp/brain.log
    curl http://127.0.0.1:4404/ # health check -> "ok"

Stdlib + pygame only (pygame is already installed for companion.py).
Python 3.7 compatible — this runs on RetroPie 4.8.

The API key is read from ~/.config/companion/gemini.key (chmod 600).
It never goes in the repo, in env vars, or in a URL.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")   # no display needed
import pygame  # noqa: E402

HOST, PORT = "127.0.0.1", 4404          # localhost only — never exposed to the LAN
KEY_FILE = os.path.expanduser("~/.config/companion/gemini.key")
MODEL = os.environ.get("COMPANION_MODEL", "gemini-2.5-flash")
API = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
LOG_PATH = "/tmp/brain.log"
TIMEOUT_S = 15
MAX_WORDS = 45

PERSONA = (
    "You are a snarky retro gaming sidekick who lives inside a handheld console. "
    "You are looking at a screenshot of the game the player is in right now. "
    "Work out what is happening, then react in character: tease if they look like "
    "they are struggling, hype them up if they are doing well, and slip in one short, "
    "genuinely useful hint when you can. Keep it under 35 words. Plain text only, "
    "no markdown, no emojis. Never refuse to comment on a game."
)

BG = (18, 20, 28, 225)
BORDER = (125, 216, 143, 255)
TEXT = (230, 232, 239)
_seen_first_request = False


# ──────────────────────────────────────────────────────────────────────
#  Logging
# ──────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    line = "{} {}".format(time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


# ──────────────────────────────────────────────────────────────────────
#  Gemini
# ──────────────────────────────────────────────────────────────────────

def load_key() -> str | None:
    try:
        with open(KEY_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _post(key: str, body: dict) -> dict:
    req = urllib.request.Request(
        API.format(MODEL),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read())


def ask_gemini(key: str, img_b64: str, mime: str, label: str) -> str:
    prompt = PERSONA + ("\nThe game is: {}".format(label) if label else "")
    body = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime, "data": img_b64}},
        ]}],
        # Thinking adds seconds of latency; a quip doesn't need it.
        "generationConfig": {"temperature": 0.9, "thinkingConfig": {"thinkingBudget": 0}},
    }
    try:
        data = _post(key, body)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        body.pop("generationConfig")        # model doesn't support thinkingConfig
        data = _post(key, body)
    parts = data["candidates"][0]["content"]["parts"]
    text = " ".join(p.get("text", "") for p in parts).strip()
    words = text.split()
    return " ".join(words[:MAX_WORDS]) + ("..." if len(words) > MAX_WORDS else "")


# ──────────────────────────────────────────────────────────────────────
#  Speech bubble
# ──────────────────────────────────────────────────────────────────────

def _wrap(text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if font.size(trial)[0] <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def bubble_png(text: str, w: int, h: int) -> bytes:
    """Transparent overlay the size of the game frame, bubble along the bottom."""
    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    font = pygame.font.Font(None, max(14, h // 13))
    pad = max(4, h // 40)
    max_bw = int(w * 0.92)
    lines = _wrap(text, font, max_bw - 2 * pad)
    lh = font.get_linesize()
    max_lines = max(1, int(h * 0.6) // lh)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".") + "..."
    bw = min(max_bw, max(font.size(l)[0] for l in lines) + 2 * pad)
    bh = lh * len(lines) + 2 * pad
    x, y = (w - bw) // 2, h - bh - max(4, h // 30)
    pygame.draw.rect(surf, BG, (x, y, bw, bh), border_radius=8)
    pygame.draw.rect(surf, BORDER, (x, y, bw, bh), 2, border_radius=8)
    for i, line in enumerate(lines):
        surf.blit(font.render(line, True, TEXT), (x + pad, y + pad + i * lh))
    tmp = "/tmp/brain_overlay.png"
    pygame.image.save(surf, tmp)
    with open(tmp, "rb") as f:
        return f.read()


def frame_size(raw: bytes) -> tuple[int, int]:
    try:
        return pygame.image.load(io.BytesIO(raw)).get_size()
    except Exception:
        return 320, 240


def sniff_mime(raw: bytes) -> str:
    return "image/png" if raw[:4] == b"\x89PNG" else "image/bmp"


# ──────────────────────────────────────────────────────────────────────
#  HTTP
# ──────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):     # silence default stderr logging
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, b"ok\n", "text/plain")

    def do_POST(self):
        global _seen_first_request
        t0 = time.time()
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except Exception as e:
            log("bad request: {}".format(e))
            self._send(400, b"{}", "application/json")
            return

        if not _seen_first_request:        # record what RetroArch actually sends
            shape = {k: ("<{} chars>".format(len(v)) if k == "image" else v)
                     for k, v in payload.items()}
            log("FIRST REQUEST path={} fields={}".format(self.path, json.dumps(shape)[:600]))
            _seen_first_request = True

        img_b64 = payload.get("image", "")
        raw = base64.b64decode(img_b64) if img_b64 else b""
        w, h = frame_size(raw)
        label = str(payload.get("label", "") or "")

        key = load_key()
        if not key:
            reply = "No API key found. I'm just a pretty overlay until you give me one."
        elif not raw:
            reply = "RetroArch sent me nothing to look at. Rude."
        else:
            try:
                reply = ask_gemini(key, img_b64, sniff_mime(raw), label)
            except (urllib.error.URLError, OSError, TimeoutError):
                reply = "No signal. Guess you're on your own, hero."
            except (KeyError, IndexError, ValueError) as e:
                log("unexpected Gemini response: {}".format(e))
                reply = "My brain returned static. Try again."

        out = json.dumps({"image": base64.b64encode(bubble_png(reply, w, h)).decode()})
        self._send(200, out.encode(), "application/json")
        log("{:.2f}s  {}x{}  label={!r}  reply={!r}".format(time.time() - t0, w, h, label, reply))


def main() -> None:
    pygame.font.init()
    log("brain up on http://{}:{}  model={}  key={}".format(
        HOST, PORT, MODEL, "found" if load_key() else "MISSING"))
    HTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
