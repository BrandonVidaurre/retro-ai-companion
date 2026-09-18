#!/usr/bin/env python3
"""
Retro AI Companion — GPi Case 2W prototype
==========================================

A controller-driven companion for a RetroPie handheld, rendered at the
GPi Case 2W's exact panel resolution (640x480, 3.0" IPS).

Design constraints this app is built around:
  * No keyboard and no mouse. Every interaction is D-pad + face buttons.
  * No microphone in the case, so nothing here is voice-driven.
  * 512MB of RAM shared with EmulationStation, so the "brain" is an
    offline SQLite + FTS5 retrieval index, not a local language model.
  * Wi-Fi is sometimes there and sometimes not, so the cloud provider is
    optional and the app must be fully useful without it.

Runs identically on a Windows/Linux desktop and on the Pi, so the whole
thing can be built before the case ships.

    pip install pygame
    python companion.py --scale 2      # desktop dev, 1280x960 window
    python companion.py                # on the Pi, native 640x480
    python companion.py --reseed       # rebuild the knowledge base

Controls (desktop keyboard -> handheld button):
    Arrows = D-pad      Z = A      X = B      A = X      S = Y
    Enter  = START      RShift = SELECT       Esc = quit
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import pygame

# ──────────────────────────────────────────────────────────────────────
#  Config
# ──────────────────────────────────────────────────────────────────────

WIDTH, HEIGHT = 640, 480          # GPi Case 2W panel, do not change
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "companion.db")
FPS = 30                          # plenty for a menu, and kind to the battery

# Logical buttons. The GPi's physical buttons arrive as a standard SDL
# joystick under RetroPie, so desktop keys and handheld buttons both fold
# into this one vocabulary and the screens never know the difference.
UP, DOWN, LEFT, RIGHT = "UP", "DOWN", "LEFT", "RIGHT"
A, B, X, Y, L, R = "A", "B", "X", "Y", "L", "R"
START, SELECT = "START", "SELECT"

KEYMAP = {
    pygame.K_UP: UP, pygame.K_DOWN: DOWN, pygame.K_LEFT: LEFT, pygame.K_RIGHT: RIGHT,
    pygame.K_z: A, pygame.K_x: B, pygame.K_a: X, pygame.K_s: Y,
    pygame.K_q: L, pygame.K_w: R,
    pygame.K_RETURN: START, pygame.K_RSHIFT: SELECT, pygame.K_BACKSPACE: Y,
}

# Joystick button indices. Verify on the real hardware with `jstest` and
# adjust — RetroPie's GPi driver ordering has changed between releases.
   # Verified on GPi Case 2W hardware with `jstest --event` (xpad, "X-Box 360 pad").
   # 6/7 are TL2/TR2, which the GPi doesn't have; Select/Start are 8/9.
   PADMAP = {0: A, 1: B, 2: X, 3: Y, 4: L, 5: R, 8: SELECT, 9: START}

REPEAT_DELAY_MS = 380     # hold-to-scroll: first repeat
REPEAT_RATE_MS = 90       # hold-to-scroll: subsequent repeats


# ──────────────────────────────────────────────────────────────────────
#  Theme
# ──────────────────────────────────────────────────────────────────────

BG        = (18, 20, 28)
PANEL     = (27, 31, 43)
PANEL_HI  = (38, 44, 60)
TEXT      = (230, 232, 239)
DIM       = (139, 147, 167)
ACCENT    = (125, 216, 143)     # green — selection, primary
AMBER     = (255, 204, 102)     # amber — headings, kinds
RED       = (255, 107, 107)     # warnings (perf notes)
LINE      = (48, 54, 72)

HEADER_H = 46
FOOTER_H = 40
BODY_TOP = HEADER_H + 8
BODY_BOT = HEADER_H - HEADER_H + (HEIGHT - FOOTER_H) - 8

KIND_COLORS = {
    "tip": ACCENT, "cheat": AMBER, "controls": (137, 180, 250),
    "lore": (203, 166, 247), "perf": RED,
}


def load_font(size: int, bold: bool = False) -> pygame.font.Font:
    """Monospace where available, default font otherwise. Never fails."""
    return pygame.font.SysFont(
        "consolas,dejavusansmono,menlo,couriernew,monospace", size, bold=bold
    )


class Fonts:
    def __init__(self) -> None:
        self.title = load_font(26, bold=True)
        self.item = load_font(21)
        self.item_b = load_font(21, bold=True)
        self.body = load_font(19)
        self.small = load_font(16)
        self.tiny = load_font(14)


# ──────────────────────────────────────────────────────────────────────
#  Knowledge base
# ──────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE systems (
    id     INTEGER PRIMARY KEY,
    slug   TEXT UNIQUE NOT NULL,
    name   TEXT NOT NULL,
    perf   TEXT NOT NULL,          -- how it actually runs on a Zero 2 W
    rating INTEGER NOT NULL        -- 3 = great, 2 = playable, 1 = don't
);

CREATE TABLE games (
    id        INTEGER PRIMARY KEY,
    system_id INTEGER NOT NULL REFERENCES systems(id),
    title     TEXT NOT NULL,
    year      INTEGER,
    summary   TEXT
);

CREATE TABLE entries (
    id      INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id),
    kind    TEXT NOT NULL,         -- tip | cheat | controls | lore | perf
    title   TEXT NOT NULL,
    body    TEXT NOT NULL
);

CREATE INDEX idx_games_system ON games(system_id);
CREATE INDEX idx_entries_game ON entries(game_id);

-- Full-text search is what makes this feel smart with zero ML and zero
-- network. FTS5 ships in the stock sqlite3 on Raspberry Pi OS.
-- A regular (not contentless) FTS5 table: it keeps its own copy of the
-- text, which costs a few hundred KB and means search results can be
-- rendered straight from the index without a second lookup.
-- `system` carries the alias soup ("Nintendo 64 n64 nintendo64 ultra64") so
-- shorthand queries hit; `sysname` is the clean name and is UNINDEXED, so it
-- costs nothing to match and is what gets shown to the player.
CREATE VIRTUAL TABLE entries_fts USING fts5(
    title, body, game, system, sysname UNINDEXED,
    tokenize='porter unicode61'
);
"""

SYSTEMS = [
    # slug, name, perf note, rating
    ("nes",      "NES",            "Full speed, no tweaks. The Zero 2W has headroom to spare here.", 3),
    ("snes",     "Super Nintendo", "Great on snes9x2005/2010. SuperFX games (Star Fox, Yoshi's Island) stutter — the chip has to be emulated too.", 3),
    ("gb",       "Game Boy",       "Flawless. Also the best fit for the 3in screen.", 3),
    ("gbc",      "Game Boy Color", "Flawless.", 3),
    ("gba",      "Game Boy Advance", "Good on gpSP. mGBA is more accurate but heavier — use it only if a game misbehaves.", 3),
    ("genesis",  "Sega Genesis",   "Full speed on picodrive.", 3),
    ("sms",      "Master System",  "Full speed.", 3),
    ("pcengine", "PC Engine",      "Full speed on the standard core.", 3),
    ("arcade",   "Arcade",         "Pre-Neo Geo boards run well on fbneo/mame2003. Neo Geo often needs frameskip.", 2),
    ("psx",      "PlayStation",    "Marginal. 2D games are mostly playable on pcsx-rearmed with frameskip; 3D titles struggle. Expect compromises.", 2),
    ("n64",      "Nintendo 64",    "Don't. The Zero 2W has neither the CPU nor the RAM headroom. This is the one system to skip.", 1),
]

# (system_slug, title, year, summary, [(kind, title, body), ...])
GAMES = [
    ("nes", "Contra", 1988,
     "Run-and-gun co-op shooter. Brutally hard, endlessly replayed.", [
         ("cheat", "Konami Code — 30 lives",
          "At the title screen press Up, Up, Down, Down, Left, Right, Left, Right, B, A, then Start. "
          "You begin with 30 lives instead of 3. Entering it on the 2-player start gives both players 30."),
         ("tip", "The Spread Gun is the only weapon that matters",
          "S (Spread) trivialises most of the game. If you die and lose it, the Laser and Flame are "
          "actively worse than the default rifle in tight corridors — consider dying deliberately to "
          "reset to a stage where S appears early."),
         ("tip", "You can shoot diagonally while running",
          "Hold a diagonal on the D-pad and fire. Most of the difficulty spike in stage 3 onward comes "
          "from players only firing on the cardinal axes."),
     ]),
    ("nes", "Super Mario Bros. 3", 1990,
     "The high point of NES platforming. Suits, warp whistles, secret worlds.", [
         ("cheat", "Warp Whistle locations",
          "World 1-3: at the white block near the end, duck on it for ~5 seconds to fall behind the "
          "scenery, then run right to a hidden pipe. World 1 fortress: crouch on the white block in the "
          "top-left room. World 2: buy from the Hammer Bros. after clearing the desert."),
         ("tip", "P-Wing trivialises the auto-scroll levels",
          "The P-Wing gives permanent flight for one stage. Save them for World 8's airships rather than "
          "burning them early."),
         ("tip", "White Toad Houses",
          "Beat a level without ever letting the timer's ones digit hit 1, 3 or 6 and a white Toad House "
          "appears — those hold the Warp Whistle and the anchor."),
     ]),
    ("nes", "Mega Man 2", 1989,
     "The one that defined the series. Eight bosses, weapon rock-paper-scissors.", [
         ("tip", "Optimal boss order",
          "Metal Man -> Air Man -> Crash Man -> Flash Man -> Quick Man -> Bubble Man -> Heat Man -> "
          "Wood Man. Each boss is weak to the weapon from the one before."),
         ("cheat", "Metal Blade breaks the game",
          "Metal Blade fires in eight directions, costs almost no energy, and one-shots several bosses "
          "it isn't even supposed to be strong against. If a section feels impossible, try Metal Blade."),
     ]),
    ("nes", "The Legend of Zelda", 1987,
     "The original open-world action-adventure.", [
         ("cheat", "Start the Second Quest immediately",
          "Enter ZELDA as your character name on a new file and the harder Second Quest unlocks straight "
          "away, with completely different dungeon layouts."),
         ("tip", "Bomb upgrades are hidden in dungeons",
          "Old men in burnable/bombable rooms raise your max bomb count. You will run out constantly "
          "until you find two or three of them."),
     ]),
    ("snes", "Super Metroid", 1994,
     "Atmospheric exploration, still the genre's benchmark.", [
         ("controls", "The wall jump",
          "Jump into a wall, and the instant Samus turns away from it, press the D-pad toward the wall "
          "and jump together. The timing window is roughly 5 frames. Practise in the room where the "
          "Etecoons demonstrate it — that room exists purely to teach you this."),
         ("controls", "Shinespark",
          "Run until Samus flashes, then press Down to store the charge. You have a few seconds to press "
          "a direction + Jump to launch. Required for several major item pickups."),
         ("tip", "Bomb jumping",
          "Lay a bomb, and as it detonates lay another at the apex. Chained correctly this climbs "
          "vertical shafts indefinitely and skips a lot of intended progression."),
     ]),
    ("snes", "The Legend of Zelda: A Link to the Past", 1991,
     "Light world / dark world. The template most 2D Zeldas still follow.", [
         ("tip", "All four bottles",
          "Kakariko village (buy for 100 rupees), the dying man in the cave north-east of the village, "
          "the Dark World merchant, and buried in the graveyard's north-east corner. Bottles of fairies "
          "are effectively extra lives — never travel without them."),
         ("tip", "Pegasus Boots dash cancels",
          "Tapping the dash and releasing early lets you reposition faster than walking. Also breaks "
          "certain cracked walls the game never explicitly tells you about."),
     ]),
    ("snes", "Super Mario World", 1990,
     "Cape feather, Yoshi, and a map full of secret exits.", [
         ("tip", "Star Road is the fast route",
          "Secret exits in Donut Plains 1, Vanilla Secret 1, and the Star World levels chain together "
          "into a warp network that reaches Bowser in under an hour."),
         ("controls", "Infinite cape flight",
          "With the cape, build a full run then tap Down as you begin to descend and Up as you rise — "
          "a rhythmic pump that keeps you airborne indefinitely over open stages."),
         ("cheat", "Yoshi wings",
          "Certain ? blocks while riding Yoshi grant wings and a bonus coin stage. Donut Plains 2 and "
          "Vanilla Dome 2 both hide one."),
     ]),
    ("snes", "Chrono Trigger", 1995,
     "Time-travel JRPG with thirteen endings and no random encounters.", [
         ("tip", "New Game+ is the point",
          "Finish once, then start New Game+ with all levels and gear intact. Most of the alternate "
          "endings are only reachable by fighting the final boss unusually early."),
         ("tip", "Dual and triple techs",
          "Party composition changes which combo techs exist. Crono/Marle/Lucca unlocks Delta Force, "
          "which carries most of the mid-game."),
     ]),
    ("snes", "Final Fantasy VI", 1994,
     "Fourteen playable characters and an antagonist who actually wins.", [
         ("tip", "Espers decide your stat growth",
          "Level-up bonuses come from the Esper equipped, not the character. If you want a physical "
          "Sabin, equip a +Strength Esper well before he levels."),
         ("tip", "Don't sell the Ultima weapon early",
          "The Cursed Shield becomes the Paladin Shield after 255 battles — annoying, but it is the best "
          "defensive item in the game."),
     ]),
    ("snes", "Street Fighter II Turbo", 1993,
     "The definitive SNES version of the arcade standard.", [
         ("controls", "Core special move inputs",
          "Hadouken: Down, Down-Forward, Forward + Punch. Shoryuken: Forward, Down, Down-Forward + "
          "Punch. Hurricane Kick: Down, Down-Back, Back + Kick. Sonic Boom: hold Back ~2s, then Forward "
          "+ Punch."),
         ("tip", "Turbo speed setting",
          "Hold Down + L + R + Select at the licence screen to unlock the full ten-star speed range. "
          "Star 4 is roughly arcade speed."),
     ]),
    ("genesis", "Sonic the Hedgehog", 1991,
     "Sega's answer to Mario, built entirely around momentum.", [
         ("cheat", "Level select",
          "At the title screen press Up, Down, Left, Right, then hold A and press Start. A chime "
          "confirms it. Works on the original release, not on every later compilation."),
         ("tip", "Keep your rings for the special stages",
          "50 rings at the end of an act opens the giant ring. The Chaos Emeralds are only obtainable "
          "in Act 1 and Act 2 of each zone — Act 3 has no special stage."),
     ]),
    ("genesis", "Sonic the Hedgehog 2", 1992,
     "Faster, longer, and the debut of Super Sonic.", [
         ("cheat", "Level select and debug mode",
          "Sound test at the options screen: play 19, 65, 09, 17, then hold A and press Start. For debug "
          "mode, play 01, 09, 09, 02, 01, 01, 02, 04 first."),
         ("tip", "Super Sonic",
          "All seven Chaos Emeralds, then 50 rings and a double jump. The half-pipe special stages are "
          "far easier with Tails following — he collects rings you miss."),
     ]),
    ("genesis", "Streets of Rage 2", 1992,
     "The high-water mark for the side-scrolling brawler, and that soundtrack.", [
         ("controls", "Blitz attacks beat star moves",
          "Tap Forward, Forward + Attack for a character-specific rush that costs no health, unlike the "
          "special. Axel's Grand Upper is the strongest move in the game."),
         ("tip", "Back attack",
          "Press Attack while an enemy is behind you for a free hit. Essential from stage 4 onward when "
          "you get surrounded constantly."),
     ]),
    ("gb", "Pokemon Red", 1996,
     "The one that started it. 151 Pokemon, two versions, endless playground bugs.", [
         ("tip", "Starter matchups",
          "Bulbasaur trivialises the first two gyms. Charmander makes them genuinely hard and then coasts "
          "from gym 3 onward. Squirtle is the even choice throughout."),
         ("cheat", "The Mew glitch",
          "Trigger a Trainer's line of sight, then escape via the Teleport/Fly menu before the battle "
          "starts. The interrupted encounter can be manipulated into a level 7 Mew near Nugget Bridge. "
          "It is a genuine bug in the retail cartridge, not a ROM hack."),
         ("tip", "Missingno. corrupts your save",
          "The item duplication trick works, but it also has a real chance of damaging your Hall of Fame "
          "data. Back up your save state first."),
     ]),
    ("gba", "Metroid: Zero Mission", 2004,
     "A remake of the 1986 original, with a whole extra act bolted on.", [
         ("tip", "Speed Booster shinesparks open the map",
          "Most of the optional missile expansions are behind stored shinespark charges. If a room has a "
          "long flat corridor and a wall you cannot break, that is the answer."),
         ("controls", "The Zero Suit stealth section",
          "You cannot fight during it. Use the ceiling grapple points and wait for patrol gaps — running "
          "the ground route directly will fail every time."),
     ]),
    ("psx", "Castlevania: Symphony of the Night", 1997,
     "The other half of the word 'Metroidvania'.", [
         ("perf", "Performance on a Zero 2 W",
          "This is a 2D PS1 game, so it is one of the more achievable PSX titles — but pcsx-rearmed on a "
          "Zero 2W will still need frameskip and will drop audio in busy rooms. Playable, not pristine. "
          "If you want it flawless, the Saturn or a stronger Pi is the honest answer."),
         ("cheat", "Alternate starting stats",
          "Enter AXEARMOR as your name for a harder start with better gear, or X-X!V''Q for Richter's "
          "stat profile. Name your file RICHTER to play as him outright."),
         ("tip", "Don't miss the inverted castle",
          "Equip the Holy Glasses and defeat Richter without killing him — otherwise you get the bad "
          "ending at roughly 50 percent map completion."),
     ]),
]


# People type "n64", not "Nintendo 64". Folding shorthand into the indexed
# system text is the cheapest possible synonym handling.
ALIASES = {
    "nes": "nes famicom nintendo 8bit",
    "snes": "snes superfamicom supernintendo 16bit",
    "gb": "gb gameboy",
    "gbc": "gbc gameboycolor",
    "gba": "gba gameboyadvance advance",
    "genesis": "genesis megadrive sega md",
    "sms": "sms mastersystem sega",
    "pcengine": "pcengine turbografx tg16",
    "arcade": "arcade mame neogeo fbneo",
    "psx": "psx ps1 playstation psone",
    "n64": "n64 nintendo64 ultra64",
}


def _system_text(slug: str, name: str) -> str:
    return f"{name} {ALIASES.get(slug, slug)}"


def seed(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    sys_ids: dict[str, int] = {}
    for slug, name, perf, rating in SYSTEMS:
        cur = conn.execute(
            "INSERT INTO systems (slug, name, perf, rating) VALUES (?,?,?,?)",
            (slug, name, perf, rating),
        )
        sys_ids[slug] = cur.lastrowid

    for sys_slug, title, year, summary, entries in GAMES:
        sid = sys_ids[sys_slug]
        cur = conn.execute(
            "INSERT INTO games (system_id, title, year, summary) VALUES (?,?,?,?)",
            (sid, title, year, summary),
        )
        gid = cur.lastrowid
        sys_name = next(n for s, n, _, _ in SYSTEMS if s == sys_slug)
        for kind, etitle, body in entries:
            cur2 = conn.execute(
                "INSERT INTO entries (game_id, kind, title, body) VALUES (?,?,?,?)",
                (gid, kind, etitle, body),
            )
            conn.execute(
                "INSERT INTO entries_fts (rowid, title, body, game, system, sysname) "
                "VALUES (?,?,?,?,?,?)",
                (cur2.lastrowid, etitle, body, title,
                 _system_text(sys_slug, sys_name), sys_name),
            )

    # Per-system performance notes are searchable too — "does n64 work" is
    # the single most common question this thing will be asked.
    for slug, name, perf, _rating in SYSTEMS:
        conn.execute(
            "INSERT INTO entries_fts (rowid, title, body, game, system, sysname) "
            "VALUES (?,?,?,?,?,?)",
            (100000 + sys_ids[slug], f"{name} performance",
             f"{perf} Does {name} run on the Pi Zero 2 W?", "",
             _system_text(slug, name), name),
        )
    conn.commit()


def _stale(conn: sqlite3.Connection) -> bool:
    """True when an existing companion.db predates the current schema. The DB
    is a rebuildable cache, so an outdated one is thrown away rather than
    migrated — a silently empty search is far worse than a 200ms reseed."""
    try:
        conn.execute("SELECT sysname FROM entries_fts LIMIT 1").fetchone()
        return False
    except sqlite3.OperationalError:
        return True


def open_db(reseed: bool = False) -> sqlite3.Connection:
    if reseed and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    fresh = not os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not fresh and _stale(conn):
        conn.close()
        os.remove(DB_PATH)
        fresh = True
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    if fresh:
        seed(conn)
    return conn


# ──────────────────────────────────────────────────────────────────────
#  Brain — offline retrieval, with an optional cloud step on top
# ──────────────────────────────────────────────────────────────────────

@dataclass
class Answer:
    text: str
    source: str                      # "offline" | "cloud" | "none"
    hits: list = field(default_factory=list)


class CloudProvider:
    """Swappable so the offline path is never coupled to a vendor."""
    name = "none"

    def available(self) -> bool:
        return False

    def ask(self, question: str, context: str) -> str:
        raise NotImplementedError


class GroqProvider(CloudProvider):
    """Free tier, OpenAI-compatible. Set GROQ_API_KEY to enable."""
    name = "groq"
    URL = "https://api.groq.com/openai/v1/chat/completions"
    MODEL = "llama-3.1-8b-instant"

    def available(self) -> bool:
        return bool(os.environ.get("GROQ_API_KEY"))

    def ask(self, question: str, context: str) -> str:
        payload = {
            "model": self.MODEL,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content":
                 "You are a retro game companion on a handheld console. Answer in under 90 words, "
                 "plainly, no markdown. Prefer the supplied context when it is relevant."},
                {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
            ],
        }
        req = urllib.request.Request(
            self.URL,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {os.environ['GROQ_API_KEY']}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = json.loads(resp.read())
        return body["choices"][0]["message"]["content"].strip()


class GeminiProvider(CloudProvider):
    """Free tier. Set GEMINI_API_KEY to enable."""
    name = "gemini"
    URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.0-flash:generateContent")

    def available(self) -> bool:
        return bool(os.environ.get("GEMINI_API_KEY"))

    def ask(self, question: str, context: str) -> str:
        prompt = (
            "You are a retro game companion on a handheld console. Answer in under 90 words, "
            "plainly, no markdown. Prefer the supplied context when it is relevant.\n\n"
            f"Context:\n{context}\n\nQuestion: {question}"
        )
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        req = urllib.request.Request(
            f"{self.URL}?key={os.environ['GEMINI_API_KEY']}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = json.loads(resp.read())
        return body["candidates"][0]["content"]["parts"][0]["text"].strip()


def pick_provider() -> CloudProvider:
    for p in (GroqProvider(), GeminiProvider()):
        if p.available():
            return p
    return CloudProvider()


class Brain:
    """Offline first. Cloud only when the local index comes up short."""

    GOOD_ENOUGH = 1          # >=1 solid FTS hit means don't spend a network call

    def __init__(self, conn: sqlite3.Connection, cloud: CloudProvider) -> None:
        self.conn = conn
        self.cloud = cloud

    # Function words only. Anything with real meaning stays in the query.
    STOPWORDS = {
        "how", "do", "does", "did", "is", "are", "was", "the", "a", "an", "to",
        "in", "on", "for", "what", "whats", "my", "me", "it", "of", "and",
        "you", "can", "with", "at", "be", "there", "any", "am",
    }

    def _terms(self, query: str) -> list[str]:
        raw = "".join(c if c.isalnum() else " " for c in query.lower()).split()
        kept = [t for t in raw if len(t) > 1 and t not in self.STOPWORDS]
        return kept or [t for t in raw if len(t) > 1]

    def _match(self, expr: str, limit: int) -> list[sqlite3.Row]:
        try:
            return self.conn.execute(
                "SELECT rowid, title, body, game, system, sysname, rank "
                "FROM entries_fts WHERE entries_fts MATCH ? ORDER BY rank LIMIT ?",
                (expr, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    def search(self, query: str, limit: int = 8) -> list[sqlite3.Row]:
        """Narrow before wide: every term, then any term. AND keeps precise
        queries precise; the OR fallback stops a single odd word from
        returning nothing at all."""
        terms = self._terms(query)
        if not terms:
            return []
        if len(terms) > 1:
            hits = self._match(" AND ".join(terms), limit)
            if hits:
                return hits
        return self._match(" OR ".join(terms), limit)

    def ask(self, question: str) -> Answer:
        hits = self.search(question)
        # Offline first, and that means first: a solid local hit ends the
        # question here whether or not a key is configured. Falling through to
        # the network when the index already answered would trade 0.04ms for
        # a second of radio on battery, and would make the handheld behave
        # differently depending on whether Wi-Fi happened to be up.
        if len(hits) >= self.GOOD_ENOUGH:
            return self._offline(hits)
        if not self.cloud.available():
            return Answer(
                "No match in the offline library, and no cloud provider is configured.\n\n"
                "Set GROQ_API_KEY or GEMINI_API_KEY before launching to enable open-ended "
                "questions when Wi-Fi is available.",
                "none", hits)
        context = "\n\n".join(f"[{h['game'] or h['system']}] {h['title']}: {h['body']}" for h in hits[:4])
        try:
            return Answer(self.cloud.ask(question, context), "cloud", hits)
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, TimeoutError, OSError):
            if hits:
                return self._offline(hits, prefix="Cloud unreachable — offline library instead.\n\n")
            return Answer("Cloud unreachable and nothing matched offline.", "none", [])

    @staticmethod
    def _offline(hits: list[sqlite3.Row], prefix: str = "") -> Answer:
        top = hits[0]
        where = top["game"] or top["sysname"]
        rest = "".join(f"\n\n* {h['title']} ({h['game'] or h['sysname']})" for h in hits[1:4])
        more = f"\n\nAlso in the library:{rest}" if rest else ""
        return Answer(f"{prefix}{top['title']} — {where}\n\n{top['body']}{more}", "offline", hits)


# ──────────────────────────────────────────────────────────────────────
#  Drawing helpers
# ──────────────────────────────────────────────────────────────────────

def wrap(text: str, font: pygame.font.Font, width: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        approx = max(8, width // max(1, font.size("m")[0]))
        for chunk in textwrap.wrap(para, approx) or [""]:
            while font.size(chunk)[0] > width and " " in chunk:
                chunk = chunk.rsplit(" ", 1)[0]
            lines.append(chunk)
    return lines


def header(surf, fonts: Fonts, title: str, right: str = "") -> None:
    pygame.draw.rect(surf, PANEL, (0, 0, WIDTH, HEADER_H))
    pygame.draw.line(surf, ACCENT, (0, HEADER_H - 2), (WIDTH, HEADER_H - 2), 2)
    surf.blit(fonts.title.render(title, True, TEXT), (16, 11))
    if right:
        r = fonts.small.render(right, True, DIM)
        surf.blit(r, (WIDTH - r.get_width() - 16, 16))


def footer(surf, fonts: Fonts, hints: list[tuple[str, str]]) -> None:
    top = HEIGHT - FOOTER_H
    pygame.draw.rect(surf, PANEL, (0, top, WIDTH, FOOTER_H))
    pygame.draw.line(surf, LINE, (0, top), (WIDTH, top), 1)
    x = 16
    for btn, label in hints:
        bt = fonts.small.render(btn, True, BG)
        w = bt.get_width() + 14
        pygame.draw.rect(surf, AMBER, (x, top + 11, w, 20), border_radius=10)
        surf.blit(bt, (x + 7, top + 13))
        x += w + 7
        lt = fonts.small.render(label, True, DIM)
        surf.blit(lt, (x, top + 13))
        x += lt.get_width() + 18


def scroll_bar(surf, total: int, visible: int, offset: int, top: int, height: int) -> None:
    if total <= visible:
        return
    track_x = WIDTH - 7
    pygame.draw.rect(surf, PANEL_HI, (track_x, top, 4, height), border_radius=2)
    h = max(24, int(height * visible / total))
    y = top + int((height - h) * offset / max(1, total - visible))
    pygame.draw.rect(surf, DIM, (track_x, y, 4, h), border_radius=2)


# ──────────────────────────────────────────────────────────────────────
#  Screens
# ──────────────────────────────────────────────────────────────────────

class Screen:
    def handle(self, app: "App", btn: str) -> None: ...
    def draw(self, app: "App", surf) -> None: ...


class ListScreen(Screen):
    """Vertical list with hold-to-scroll, used by most of the app."""
    ROW_H = 34
    title = ""
    subtitle = ""

    def __init__(self) -> None:
        self.index = 0
        self.offset = 0

    def rows(self) -> list[tuple[str, str]]:
        """[(primary, secondary)]"""
        return []

    def on_select(self, app: "App") -> None: ...

    @property
    def visible(self) -> int:
        return (HEIGHT - FOOTER_H - BODY_TOP) // self.ROW_H

    def handle(self, app: "App", btn: str) -> None:
        n = len(self.rows())
        if btn == DOWN and n:
            self.index = (self.index + 1) % n
        elif btn == UP and n:
            self.index = (self.index - 1) % n
        elif btn == RIGHT and n:
            self.index = min(n - 1, self.index + self.visible)
        elif btn == LEFT and n:
            self.index = max(0, self.index - self.visible)
        elif btn == A and n:
            self.on_select(app)
        elif btn == B:
            app.pop()
        if self.index < self.offset:
            self.offset = self.index
        elif self.index >= self.offset + self.visible:
            self.offset = self.index - self.visible + 1

    def draw(self, app: "App", surf) -> None:
        rows = self.rows()
        header(surf, app.fonts, self.title, self.subtitle)
        y = BODY_TOP
        for i in range(self.offset, min(len(rows), self.offset + self.visible)):
            primary, secondary = rows[i]
            sel = i == self.index
            if sel:
                pygame.draw.rect(surf, PANEL_HI, (8, y - 3, WIDTH - 24, self.ROW_H - 2), border_radius=6)
                pygame.draw.rect(surf, ACCENT, (8, y - 3, 3, self.ROW_H - 2), border_radius=2)
            f = app.fonts.item_b if sel else app.fonts.item
            surf.blit(f.render(primary, True, TEXT if sel else (200, 205, 218)), (22, y + 2))
            if secondary:
                st = app.fonts.small.render(secondary, True, ACCENT if sel else DIM)
                surf.blit(st, (WIDTH - st.get_width() - 26, y + 6))
            y += self.ROW_H
        scroll_bar(surf, len(rows), self.visible, self.offset, BODY_TOP, HEIGHT - FOOTER_H - BODY_TOP)
        footer(surf, app.fonts, self.footer_hints())

    def footer_hints(self) -> list[tuple[str, str]]:
        return [("A", "Select"), ("B", "Back")]


class HomeScreen(ListScreen):
    title = "RETRO COMPANION"

    ITEMS = [
        ("Browse by system", "systems"),
        ("Search the library", "search"),
        ("Ask anything", "ask"),
        ("What runs on this Pi?", "perf"),
        ("About", "about"),
    ]

    def __init__(self, app: "App") -> None:
        super().__init__()
        self.subtitle = f"{app.stats['games']} games / {app.stats['entries']} entries"

    def rows(self):
        return [(label, "") for label, _ in self.ITEMS]

    def on_select(self, app):
        dest = self.ITEMS[self.index][1]
        if dest == "systems":
            app.push(SystemsScreen(app))
        elif dest == "search":
            app.push(KeyboardScreen("Search", lambda q: app.push(ResultsScreen(app, q))))
        elif dest == "ask":
            app.push(KeyboardScreen("Ask", lambda q: app.push(AnswerScreen(app, q))))
        elif dest == "perf":
            app.push(PerfScreen(app))
        elif dest == "about":
            app.push(AboutScreen(app))

    def handle(self, app, btn):
        if btn == B:
            return          # home is the root; B must not exit the app
        super().handle(app, btn)

    def footer_hints(self):
        return [("A", "Select"), ("START", "Quit")]


class SystemsScreen(ListScreen):
    title = "SYSTEMS"

    def __init__(self, app):
        super().__init__()
        self.data = app.conn.execute(
            "SELECT s.*, COUNT(g.id) AS n FROM systems s "
            "LEFT JOIN games g ON g.system_id = s.id GROUP BY s.id ORDER BY s.rating DESC, s.name"
        ).fetchall()

    def rows(self):
        return [(r["name"], f"{r['n']} games" if r["n"] else "—") for r in self.data]

    def on_select(self, app):
        app.push(GamesScreen(app, self.data[self.index]))


class GamesScreen(ListScreen):
    def __init__(self, app, system):
        super().__init__()
        self.system = system
        self.title = system["name"].upper()
        self.subtitle = {3: "runs great", 2: "playable", 1: "not recommended"}[system["rating"]]
        self.data = app.conn.execute(
            "SELECT * FROM games WHERE system_id = ? ORDER BY title", (system["id"],)
        ).fetchall()

    def rows(self):
        if not self.data:
            return [("No games catalogued yet", "")]
        return [(g["title"], str(g["year"] or "")) for g in self.data]

    def on_select(self, app):
        if self.data:
            app.push(GameScreen(app, self.data[self.index]))


class GameScreen(ListScreen):
    def __init__(self, app, game):
        super().__init__()
        self.game = game
        self.title = game["title"][:26]
        self.subtitle = str(game["year"] or "")
        self.data = app.conn.execute(
            "SELECT * FROM entries WHERE game_id = ? ORDER BY kind, id", (game["id"],)
        ).fetchall()

    def rows(self):
        return [(e["title"], e["kind"]) for e in self.data]

    def on_select(self, app):
        e = self.data[self.index]
        app.push(TextScreen(e["title"], e["body"], kind=e["kind"], sub=self.game["title"]))

    def draw(self, app, surf):
        super().draw(app, surf)


class PerfScreen(ListScreen):
    title = "WHAT RUNS HERE"
    subtitle = "Pi Zero 2 W"

    def __init__(self, app):
        super().__init__()
        self.data = app.conn.execute(
            "SELECT * FROM systems ORDER BY rating DESC, name"
        ).fetchall()

    def rows(self):
        mark = {3: "great", 2: "playable", 1: "skip it"}
        return [(r["name"], mark[r["rating"]]) for r in self.data]

    def on_select(self, app):
        r = self.data[self.index]
        kind = {3: "tip", 2: "tip", 1: "perf"}[r["rating"]]
        app.push(TextScreen(f"{r['name']}", r["perf"], kind=kind, sub="performance"))

    def draw(self, app, surf):
        rows = self.rows()
        header(surf, app.fonts, self.title, self.subtitle)
        y = BODY_TOP
        for i in range(self.offset, min(len(rows), self.offset + self.visible)):
            r = self.data[i]
            sel = i == self.index
            if sel:
                pygame.draw.rect(surf, PANEL_HI, (8, y - 3, WIDTH - 24, self.ROW_H - 2), border_radius=6)
            col = {3: ACCENT, 2: AMBER, 1: RED}[r["rating"]]
            pygame.draw.rect(surf, col, (16, y + 4, 8, 14), border_radius=2)
            f = app.fonts.item_b if sel else app.fonts.item
            surf.blit(f.render(r["name"], True, TEXT if sel else (200, 205, 218)), (34, y + 2))
            st = app.fonts.small.render(rows[i][1], True, col)
            surf.blit(st, (WIDTH - st.get_width() - 26, y + 6))
            y += self.ROW_H
        scroll_bar(surf, len(rows), self.visible, self.offset, BODY_TOP, HEIGHT - FOOTER_H - BODY_TOP)
        footer(surf, app.fonts, [("A", "Details"), ("B", "Back")])


class TextScreen(Screen):
    """Scrollable body text — entries, answers, about."""
    STEP = 3

    def __init__(self, title: str, body: str, kind: str = "", sub: str = "") -> None:
        self.title, self.body, self.kind, self.sub = title, body, kind, sub
        self.scroll = 0
        self._lines: list[str] | None = None

    def lines(self, fonts: Fonts) -> list[str]:
        if self._lines is None:
            self._lines = wrap(self.body, fonts.body, WIDTH - 44)
        return self._lines

    def handle(self, app, btn):
        vis = self.visible_lines()
        total = len(self.lines(app.fonts))
        if btn == DOWN:
            self.scroll = min(max(0, total - vis), self.scroll + 1)
        elif btn == UP:
            self.scroll = max(0, self.scroll - 1)
        elif btn == RIGHT:
            self.scroll = min(max(0, total - vis), self.scroll + self.STEP)
        elif btn == LEFT:
            self.scroll = max(0, self.scroll - self.STEP)
        elif btn == B:
            app.pop()

    @staticmethod
    def visible_lines() -> int:
        return (HEIGHT - FOOTER_H - BODY_TOP - 34) // 24

    def draw(self, app, surf):
        header(surf, app.fonts, self.title[:28], self.sub)
        y = BODY_TOP
        if self.kind:
            col = KIND_COLORS.get(self.kind, DIM)
            label = app.fonts.tiny.render(self.kind.upper(), True, BG)
            w = label.get_width() + 14
            pygame.draw.rect(surf, col, (22, y, w, 19), border_radius=9)
            surf.blit(label, (29, y + 3))
            y += 30
        lines = self.lines(app.fonts)
        vis = self.visible_lines()
        for ln in lines[self.scroll:self.scroll + vis]:
            surf.blit(app.fonts.body.render(ln, True, TEXT), (22, y))
            y += 24
        scroll_bar(surf, len(lines), vis, self.scroll, BODY_TOP, HEIGHT - FOOTER_H - BODY_TOP)
        hints = [("B", "Back")]
        if len(lines) > vis:
            hints.insert(0, ("D-PAD", "Scroll"))
        footer(surf, app.fonts, hints)


class KeyboardScreen(Screen):
    """On-screen keyboard. There is no physical keyboard on the handheld,
    so every text input in the app has to come through this."""

    ROWS = ["ABCDEFGHIJ", "KLMNOPQRST", "UVWXYZ0123", "456789 -'?"]
    CELL_W, CELL_H = 54, 44

    def __init__(self, title: str, on_submit) -> None:
        self.title = title
        self.on_submit = on_submit
        self.text = ""
        self.cx = self.cy = 0

    def handle(self, app, btn):
        if btn == UP:
            self.cy = (self.cy - 1) % len(self.ROWS)
        elif btn == DOWN:
            self.cy = (self.cy + 1) % len(self.ROWS)
        elif btn == LEFT:
            self.cx = (self.cx - 1) % len(self.ROWS[self.cy])
        elif btn == RIGHT:
            self.cx = (self.cx + 1) % len(self.ROWS[self.cy])
        elif btn == A:
            if len(self.text) < 38:
                self.text += self.ROWS[self.cy][self.cx]
        elif btn == Y:
            self.text = self.text[:-1]
        elif btn == X:
            if len(self.text) < 38:
                self.text += " "
        elif btn == START:
            if self.text.strip():
                q = self.text.strip()
                app.pop()
                self.on_submit(q)
        elif btn == B:
            app.pop()

    def draw(self, app, surf):
        header(surf, app.fonts, self.title.upper())
        # input field
        pygame.draw.rect(surf, PANEL, (22, BODY_TOP, WIDTH - 44, 40), border_radius=6)
        pygame.draw.rect(surf, ACCENT if self.text else LINE, (22, BODY_TOP, WIDTH - 44, 40), 2, border_radius=6)
        shown = self.text if self.text else "type with the D-pad"
        col = TEXT if self.text else DIM
        surf.blit(app.fonts.item.render(shown, True, col), (34, BODY_TOP + 10))

        grid_top = BODY_TOP + 58
        grid_left = (WIDTH - self.CELL_W * 10) // 2
        for ry, row in enumerate(self.ROWS):
            for rx, ch in enumerate(row):
                x = grid_left + rx * self.CELL_W
                y = grid_top + ry * self.CELL_H
                sel = (rx == self.cx and ry == self.cy)
                pygame.draw.rect(surf, ACCENT if sel else PANEL,
                                 (x + 3, y + 3, self.CELL_W - 6, self.CELL_H - 6), border_radius=6)
                label = "SPC" if ch == " " else ch
                f = app.fonts.small if ch == " " else app.fonts.item_b
                t = f.render(label, True, BG if sel else TEXT)
                surf.blit(t, (x + (self.CELL_W - t.get_width()) // 2,
                              y + (self.CELL_H - t.get_height()) // 2))
        footer(surf, app.fonts, [("A", "Type"), ("Y", "Del"), ("X", "Space"), ("START", "Go"), ("B", "Back")])


class ResultsScreen(ListScreen):
    title = "RESULTS"

    def __init__(self, app, query: str):
        super().__init__()
        self.query = query
        self.subtitle = f'"{query[:18]}"'
        self.data = app.brain.search(query, limit=40)

    @staticmethod
    def _clip(s: str, n: int) -> str:
        return s if len(s) <= n else s[: n - 1].rstrip() + "~"

    def rows(self):
        if not self.data:
            return [("Nothing matched", "")]
        return [(self._clip(h["title"], 30), self._clip(h["game"] or h["sysname"], 15))
                for h in self.data]

    def on_select(self, app):
        if self.data:
            h = self.data[self.index]
            app.push(TextScreen(h["title"], h["body"], sub=h["game"] or h["sysname"]))


class AnswerScreen(TextScreen):
    def __init__(self, app, question: str):
        ans = app.brain.ask(question)
        badge = {"offline": "tip", "cloud": "lore", "none": "perf"}[ans.source]
        sub = {"offline": "offline", "cloud": f"via {app.brain.cloud.name}", "none": "no answer"}[ans.source]
        super().__init__(question[:28], ans.text, kind=badge, sub=sub)


class AboutScreen(TextScreen):
    def __init__(self, app):
        cloud = app.brain.cloud
        status = f"connected ({cloud.name})" if cloud.available() else "not configured — offline only"
        body = (
            "Retro AI Companion, prototype build.\n"
            "\n"
            f"Library: {app.stats['games']} games, {app.stats['entries']} entries, "
            f"{app.stats['systems']} systems.\n"
            f"Cloud: {status}\n"
            "\n"
            "Rendered at 640x480 to match the GPi Case 2W panel exactly, so what you see on "
            "the desktop is what lands on the handheld.\n"
            "\n"
            "The library is a local SQLite database with an FTS5 full-text index. It answers "
            "instantly, works on battery with no Wi-Fi, and costs nothing to run. When a "
            "question falls outside it and a network is available, it hands off to a free-tier "
            "model and comes straight back down when that is gone.\n"
            "\n"
            "Add content by editing the GAMES list at the top of companion.py, then run with "
            "--reseed."
        )
        super().__init__("ABOUT", body, sub="v0.1")


# ──────────────────────────────────────────────────────────────────────
#  App
# ──────────────────────────────────────────────────────────────────────

class App:
    def __init__(self, conn, brain, fonts) -> None:
        self.conn, self.brain, self.fonts = conn, brain, fonts
        self.stats = {
            "games": conn.execute("SELECT COUNT(*) c FROM games").fetchone()["c"],
            "entries": conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"],
            "systems": conn.execute("SELECT COUNT(*) c FROM systems").fetchone()["c"],
        }
        self.stack: list[Screen] = [HomeScreen(self)]
        self.running = True

    @property
    def top(self) -> Screen:
        return self.stack[-1]

    def push(self, s: Screen) -> None:
        self.stack.append(s)

    def pop(self) -> None:
        if len(self.stack) > 1:
            self.stack.pop()

    def handle(self, btn: str) -> None:
        if btn == START and isinstance(self.top, HomeScreen):
            self.running = False
            return
        self.top.handle(self, btn)

    def draw(self, surf) -> None:
        surf.fill(BG)
        self.top.draw(self, surf)


def main() -> int:
    ap = argparse.ArgumentParser(description="Retro AI Companion prototype")
    ap.add_argument("--scale", type=int, default=1, help="window scale for desktop dev (try 2)")
    ap.add_argument("--reseed", action="store_true", help="rebuild the knowledge base")
    ap.add_argument("--shots", metavar="DIR", help="render every screen to PNGs and exit")
    args = ap.parse_args()

    conn = open_db(reseed=args.reseed)

    if args.shots:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

    pygame.init()
    pygame.display.set_caption("Retro AI Companion — GPi Case 2W")
    fonts = Fonts()
    app = App(conn, Brain(conn, pick_provider()), fonts)

    canvas = pygame.Surface((WIDTH, HEIGHT))

    if args.shots:
        return dump_shots(app, canvas, args.shots)

    scale = max(1, args.scale)
    screen = pygame.display.set_mode((WIDTH * scale, HEIGHT * scale))
    clock = pygame.time.Clock()

    pygame.joystick.init()
    pads = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
    for p in pads:
        p.init()

    held: str | None = None
    held_at = 0
    last_repeat = 0

    while app.running:
        now = pygame.time.get_ticks()
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                app.running = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    app.running = False
                elif ev.key in KEYMAP:
                    btn = KEYMAP[ev.key]
                    app.handle(btn)
                    if btn in (UP, DOWN, LEFT, RIGHT):
                        held, held_at, last_repeat = btn, now, 0
            elif ev.type == pygame.KEYUP:
                if ev.key in KEYMAP and KEYMAP[ev.key] == held:
                    held = None
            elif ev.type == pygame.JOYBUTTONDOWN:
                if ev.button in PADMAP:
                    app.handle(PADMAP[ev.button])
            elif ev.type == pygame.JOYHATMOTION:
                hx, hy = ev.value
                btn = {(0, 1): UP, (0, -1): DOWN, (-1, 0): LEFT, (1, 0): RIGHT}.get((hx, hy))
                if btn:
                    app.handle(btn)
                    held, held_at, last_repeat = btn, now, 0
                else:
                    held = None

        # hold-to-scroll
        if held and now - held_at > REPEAT_DELAY_MS and now - last_repeat > REPEAT_RATE_MS:
            app.handle(held)
            last_repeat = now

        app.draw(canvas)
        if scale == 1:
            screen.blit(canvas, (0, 0))
        else:
            pygame.transform.scale(canvas, screen.get_size(), screen)
        pygame.display.flip()
        clock.tick(FPS)

    pygame.quit()
    return 0


def dump_shots(app: App, canvas, out_dir: str) -> int:
    """Render each screen to a PNG so the layout can be checked at real size."""
    os.makedirs(out_dir, exist_ok=True)

    def shot(name: str) -> None:
        app.draw(canvas)
        pygame.image.save(canvas, os.path.join(out_dir, f"{name}.png"))

    shot("01_home")
    app.handle(DOWN); app.handle(A); shot("02_search_keyboard")
    for _ in range(4):
        app.handle(RIGHT)
    app.handle(A); app.handle(DOWN); app.handle(A); shot("03_keyboard_typed")
    app.pop()

    app.stack = [HomeScreen(app)]
    app.handle(A); shot("04_systems")
    app.handle(A); shot("05_games")
    app.handle(A); shot("06_game_entries")
    app.handle(A); shot("07_entry_text")
    app.stack = [HomeScreen(app)]
    for _ in range(3):
        app.handle(DOWN)
    app.handle(A); shot("08_performance")
    app.handle(A); shot("09_perf_detail")
    app.stack = [HomeScreen(app)]
    app.push(ResultsScreen(app, "wall jump metroid"))
    shot("10_search_results")
    app.stack = [HomeScreen(app)]
    app.push(AnswerScreen(app, "HOW DO I WALL JUMP"))
    shot("11_answer_offline")
    app.stack = [HomeScreen(app)]
    app.push(AboutScreen(app))
    shot("12_about")
    print(f"wrote screenshots to {out_dir}")
    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
