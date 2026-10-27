#!/usr/bin/env python3
"""
ASCII multiplayer tanks over SSH.

Round flow: the first player in is the admin: picks the map size (small/medium/large) and view (top-down or 2.5D isometric), then edits the map (arrows/WASD move the
'#' cursor, Space toggles a wall, Enter starts). Others wait in a lobby. After a 3-2-1
countdown, last player with lives wins, then everyone returns to the map editor.
Esc quits. 2.5D mode: E rotates the camera, Q toggles straight/lob, hold Space to power a lob.

Architecture
  Game        - pure, deterministic game engine. No I/O, injectable clock and RNG,
                so it is fully unit-testable.
  KeyParser   - stateful terminal input parser (handles split escape sequences).
  Session     - one connected player: input loop, diff-based rendering, backpressure.
  TankServer  - SSH front end: connection limits, game loop at a fixed timestep.

Run:   pip install asyncssh && python3 tanks.py --port 1111 [--public-host YOUR.PUBLIC.IP]
Join:  ssh -p 1111 <host>          (optional password: export TANKS_PASSWORD=...)
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
import random
import re
import signal
import socket
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

try:
    import asyncssh
except ImportError:          # engine + tests still work without it
    asyncssh = None

log = logging.getLogger("tanks")


class _Never(Exception):
    pass


TerminalSizeChanged = getattr(asyncssh, "TerminalSizeChanged", _Never)

HUD_ROWS = 4

# Selectable map sizes: name -> (width, height). Large is the original arena.
SIZES = [("Small", 30, 12), ("Medium", 44, 16), ("Large", 60, 20)]
DEFAULT_SIZE = 2
MAX_W, MAX_H = SIZES[-1][1], SIZES[-1][2]     # lobby screens / terminal minimum use the largest

# View modes the admin can pick between
VIEWS = [("TOP", "Top-down", "Classic overhead arena"),
         ("ISO", "2.5D isometric", "Diagonal 3D camera + wall heights")]

# Wall heights: editor / top-down character and colour
WALL_CH = {1: "#", 2: "$", 3: "@"}
WALL_COL = {1: "35", 2: "38;5;208", 3: "31"}
# 256-colour faces for the isometric renderer: (top, left, right)
ISO_FACES = {0: (245, 241, 238), 1: (250, 245, 240), 2: (214, 172, 130), 3: (203, 160, 88)}
# Camera sits above the (0,0) corner looking at the far corner, so world axes run on
# the screen diagonals. Remap the 4 arrow keys onto those diagonals:
#   UP=up-right  RIGHT=down-right  DOWN=down-left  LEFT=up-left
ISO_REMAP = {"UP": "RIGHT", "RIGHT": "UP", "DOWN": "LEFT", "LEFT": "DOWN"}
ISO_FACE = {(1, 0): "NE", (0, -1): "SE", (-1, 0): "SW", (0, 1): "NW"}


def rot(dx: int, dy: int, k: int) -> Tuple[int, int]:
    """World offset -> camera frame for camera corner k (0=NW, 1=NE, 2=SE, 3=SW; clockwise)."""
    for _ in range(k % 4):
        dx, dy = dy, -dx
    return dx, dy


def unrot(rx: int, ry: int, k: int) -> Tuple[int, int]:
    """Inverse of rot(): camera-frame offset -> world offset."""
    for _ in range(k % 4):
        rx, ry = -ry, rx
    return rx, ry


_ISO_SCREEN = {"UP": (1, 0), "RIGHT": (0, -1), "DOWN": (-1, 0), "LEFT": (0, 1)}
_WORLD_KEY = {(1, 0): "RIGHT", (-1, 0): "LEFT", (0, 1): "DOWN", (0, -1): "UP"}


def iso_key(key: str, cam: int) -> str:
    """Arrow key -> world direction so each key always moves along its screen diagonal."""
    v = _ISO_SCREEN.get(key)
    return key if v is None else _WORLD_KEY[unrot(v[0], v[1], cam)]

# 5-row block digits for the full-screen countdown
BIG = {
    "3": [" ##### ", "     ##", "  #### ", "     ##", " ##### "],
    "2": [" ##### ", "     ##", " ##### ", "##     ", "#######"],
    "1": ["   ##  ", " ####  ", "   ##  ", "   ##  ", " ######"],
}
MOUSE_ON = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"   # touch/mouse: press, drag, SGR coords
MOUSE_OFF = "\x1b[?1006l\x1b[?1002l\x1b[?1000l"
ENTER = "\x1b[?1049h\x1b[2J\x1b[?25l" + MOUSE_ON   # alt screen, clear, hide cursor, touch on
LEAVE = MOUSE_OFF + "\x1b[?25h\x1b[?1049l"             # show cursor, leave alt screen
BACKLOG_LIMIT = 64 * 1024                  # skip frames for clients this far behind


# --------------------------------------------------------------------------- config
@dataclass
class Config:
    host: str = ""
    port: int = 1111
    width: int = 60
    height: int = 20
    tick: float = 0.08
    respawn_delay: float = 10
    shield_time: float = 3
    shot_cooldown: float = 0.3
    rapid_cooldown: float = 0.08
    bullet_speed: int = 2
    start_lives: int = 3
    win_display: float = 6            # seconds the winner screen shows
    low_walls_pass: bool = True       # 2.5D mode: height-1 walls block tanks but not bullets
    lob_cooldown: float = 0.8         # seconds between lob shots
    lob_first_gap: float = 0.55       # no key-repeat after the first Space -> released (a tap)
    lob_repeat_gap: float = 0.18      # gap between repeats that counts as "let go"
    lob_max_charge: float = 1.2       # seconds of holding for a full-power lob
    lob_min_range: int = 4            # tiles flown by a tap
    lob_max_range: int = 14           # tiles flown by a full charge
    lob_launch_h: float = 1.0         # shell leaves the barrel at this height (wall heights)
    lob_apex_min: float = 2.9         # arc peak at minimum power: clears 1 and 2, never 3
    lob_apex_max: float = 4.4         # arc peak at full power: clears 3 in the middle of its flight
    powerup_time: float = 10
    powerup_every: float = 8
    powerup_max: int = 3
    max_players: int = 30
    max_per_ip: int = 4
    idle_timeout: float = 120
    input_rate: float = 30.0          # sustained keys/second per player
    input_burst: int = 20
    host_key: str = "ssh_host_key"
    password: Optional[str] = None
    advertise: List[str] = field(default_factory=list)   # addresses shown to players


POWERUPS = {
    "T": "Triple Shot",
    "R": "Rapid Fire",
    "P": "Piercing Round",
    "M": "Twin Tank",
    "S": "Speed Boost",
}

MOVES = {"UP": (0, -1), "DOWN": (0, 1), "LEFT": (-1, 0), "RIGHT": (1, 0)}


# --------------------------------------------------------------------------- addresses
def detect_addresses() -> List[str]:
    """Best-effort list of this machine's non-loopback IPv4 addresses (LAN first)."""
    found: List[str] = []
    try:                                  # UDP connect sends no packets; just picks the route
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out: List[str] = []
    for ip in found:
        if ip not in out and not ip.startswith("127.") and not ip.startswith("0."):
            out.append(ip)
    return out


def connect_hint(cfg: "Config") -> str:
    hosts = cfg.advertise[:2] or ["<server-ip>"]
    return " or ".join(f"ssh -p {cfg.port} {h}" for h in hosts)


# --------------------------------------------------------------------------- input
class KeyParser:
    """Turns raw terminal bytes into UP/DOWN/LEFT/RIGHT/FIRE/QUIT tokens (keys, plus touch swipes/taps)."""
    ARROWS = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}
    PLAIN = {" ": "FIRE", "w": "UP", "s": "DOWN", "a": "LEFT", "d": "RIGHT",
             "W": "UP", "S": "DOWN", "A": "LEFT", "D": "RIGHT",
             "\r": "ENTER", "\n": "ENTER",
             "1": "H1", "2": "H2", "3": "H3", "x": "ERASE", "X": "ERASE",
             "c": "CLEAR", "C": "CLEAR", "v": "PREVIEW", "V": "PREVIEW",
             "q": "TOGGLE", "Q": "TOGGLE", "e": "ROTATE", "E": "ROTATE",
             "\x03": "QUIT", "\x04": "QUIT"}      # Esc -> QUIT is handled by Session (lone ESC)

    SWIPE_X = 3          # columns of finger travel that count as a horizontal swipe
    SWIPE_Y = 2          # rows of finger travel that count as a vertical swipe

    def __init__(self):
        self.buf = ""
        self.origin: Optional[Tuple[int, int]] = None   # where the current touch started
        self.swiped = False                              # did this touch already steer?

    @property
    def pending_escape(self) -> bool:
        """True when the only buffered byte is ESC: either a lone Esc key or a split sequence."""
        return self.buf == "\x1b"

    def _mouse(self, params: str, final: str) -> Optional[str]:
        """SGR mouse report (touch screens send these). Tap -> FIRE, swipe -> direction."""
        try:
            b, x, y = (int(p) for p in params.split(";"))
        except ValueError:
            return None
        if b & 64 or (b & 3) != 0:                       # wheel / non-primary button
            return None
        if final == "M" and not (b & 32):                # touch down
            self.origin, self.swiped = (x, y), False
            return None
        if self.origin is None:
            return None
        dx, dy = x - self.origin[0], y - self.origin[1]
        tok = None
        if abs(dx) >= self.SWIPE_X and abs(dx) >= 2 * abs(dy):   # cells are ~2x taller than wide
            tok = "RIGHT" if dx > 0 else "LEFT"
        elif abs(dy) >= self.SWIPE_Y and 2 * abs(dy) > abs(dx):
            tok = "DOWN" if dy > 0 else "UP"
        if final == "M":                                 # dragging: steer, then re-anchor
            if tok:
                self.origin, self.swiped = (x, y), True
            return tok
        tok = tok or (None if self.swiped else "FIRE")   # lift: swipe or tap
        self.origin = None
        return tok

    def feed(self, data: str) -> List[str]:
        buf = self.buf + data
        keys: List[str] = []
        i, n = 0, len(buf)
        while i < n:
            c = buf[i]
            if c != "\x1b":
                tok = self.PLAIN.get(c)
                if tok:
                    keys.append(tok)
                i += 1
                continue
            if i + 1 >= n:
                break                                   # lone ESC: wait for more
            if buf[i + 1] in "[O":
                if i + 2 >= n:
                    break
                final = buf[i + 2]
                if final == "<" and buf[i + 1] == "[":     # SGR mouse / touch report
                    j = i + 3
                    while j < n and buf[j] not in "Mm" and j - i < 24:
                        j += 1
                    if j >= n:
                        break                              # incomplete: wait for more
                    if buf[j] in "Mm":
                        tok = self._mouse(buf[i + 3:j], buf[j])
                        if tok:
                            keys.append(tok)
                        i = j + 1
                    else:
                        i += 3                             # garbage, resync
                    continue
                if final in self.ARROWS:
                    keys.append(self.ARROWS[final])
                    i += 3
                    continue
                j = i + 2                                # skip any other CSI/SS3 sequence
                while j < n and not ("@" <= buf[j] <= "~"):
                    j += 1
                if j >= n:
                    break
                i = j + 1
            else:
                i += 2                                   # Alt+key: ignore
        self.buf = buf[i:] if (n - i) <= 32 else ""      # never grow unbounded
        return keys


# --------------------------------------------------------------------------- engine
@dataclass
class Bullet:
    x: int
    y: int
    dx: int
    dy: int
    owner: int
    pierce: bool
    speed: int
    lob: bool = False          # artillery shell: arcs over walls, explodes where it lands
    steps: int = 0
    dist: int = 0
    apex: float = 0.0
    launch: float = 1.0


@dataclass
class Tank:
    id: int
    name: str
    x: int = 0
    y: int = 0
    dx: int = 1
    dy: int = 0
    alive: bool = False
    respawn_at: float = 0.0
    shield_until: float = 0.0
    last_shot: float = 0.0
    kills: int = 0
    deaths: int = 0
    lives: int = 3
    powerup: Optional[str] = None
    powerup_until: float = 0.0
    clone: Optional[Tuple[int, int]] = None
    inbox: deque = field(default_factory=lambda: deque(maxlen=64))
    tokens: float = 0.0
    last_refill: float = 0.0
    dropped: int = 0
    cam: int = 0                   # 2.5D camera corner: 0=NW 1=NE 2=SE 3=SW (press E, clockwise)
    lob_mode: bool = False         # 2.5D: Space fires a lob (True) or a straight shot (False)
    charge_n: int = 0              # Space presses/repeats seen in the current charge
    charge_first: float = 0.0
    charge_last: float = 0.0
    charge_lock: float = 0.0

    @property
    def vertical(self) -> bool:
        return self.dy != 0

    @property
    def char(self) -> str:
        return "H" if self.vertical else "\u2336"

    def shielded(self, now: float) -> bool:
        return self.alive and now < self.shield_until


class Canvas:
    """Small styled character buffer used by the isometric renderer."""

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.ch = [[" "] * w for _ in range(h)]
        self.st = [[""] * w for _ in range(h)]

    def put(self, x: int, y: int, c: str, style: str = "") -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self.ch[y][x] = c
            self.st[y][x] = style

    def rows(self) -> List[str]:
        out = []
        for y in range(self.h):
            parts, cur, run = [], None, []
            for x in range(self.w):
                st = self.st[y][x]
                if st != cur and run:
                    parts.append((cur, "".join(run)))
                    run = []
                cur = st
                run.append(self.ch[y][x])
            parts.append((cur, "".join(run)))
            out.append("".join(f"\x1b[{st}m{t}\x1b[0m" if st else t for st, t in parts))
        return out


def cheb(ax, ay, bx, by) -> int:
    return max(abs(ax - bx), abs(ay - by))


class Game:
    def __init__(self, cfg: Config, clock: Callable[[], float] = time.monotonic,
                 rng: Optional[random.Random] = None):
        self.cfg = cfg
        self.clock = clock
        self.rng = rng or random.Random()
        self.tanks: Dict[int, Tank] = {}
        self.bullets: List[Bullet] = []
        self.powerups: List[list] = []        # [x, y, kind]
        self._next_id = 0
        self._next_powerup = 0.0
        self.walls: Dict[Tuple[int, int], int] = {}   # (x, y) -> height 1..3                     # {(x, y)}
        self.phase = "SIZE"                         # SIZE | EDIT | COUNTDOWN | PLAY | WIN
        self.size_idx = DEFAULT_SIZE                # size currently applied to the arena
        self.pick_idx = DEFAULT_SIZE                # size highlighted in the admin menu
        self.view = "TOP"                           # TOP | ISO
        self.pick_view = 0
        self.brush = 1                              # wall height the editor places (2.5D)
        self.preview = False                        # editor shows the 2.5D preview
        self._clear_armed = 0.0
        self.blasts: List[list] = []                # [x, y, expires_at] lob explosions
        self._apply_size(DEFAULT_SIZE)
        self.admin_id: Optional[int] = None
        self.phase_until = 0.0
        self.round_size = 0
        self.winner = ""

    # ---- players
    def add_player(self, name: str) -> Tank:
        self._next_id += 1
        now = self.clock()
        joinable = self.phase in ("SIZE", "EDIT", "COUNTDOWN")   # mid-round joiners spectate
        t = Tank(self._next_id, name or f"tank{self._next_id}",
                 lives=self.cfg.start_lives if joinable else 0,
                 respawn_at=now, tokens=float(self.cfg.input_burst), last_refill=now)
        self.tanks[t.id] = t
        if self.admin_id is None:
            self.admin_id = t.id
        return t

    def remove_player(self, tank: Tank) -> None:
        self.tanks.pop(tank.id, None)
        self.bullets = [b for b in self.bullets if b.owner != tank.id]
        if self.admin_id == tank.id:
            self.admin_id = min(self.tanks, default=None)   # earliest joiner takes over
        if not self.tanks:
            self._enter_edit(clear_walls=True)

    # ---- phase transitions
    def _apply_size(self, idx: int) -> None:
        """Set the arena dimensions. Walls/cursor reset if the size actually changed."""
        _, w, h = SIZES[idx]
        changed = (w, h) != (self.cfg.width, self.cfg.height)
        self.size_idx = self.pick_idx = idx
        self.cfg.width, self.cfg.height = w, h
        if changed or not hasattr(self, "walls"):
            self.walls = {}
        if changed or not hasattr(self, "cursor"):
            self.cursor = [w // 2, h // 2]

    def _enter_edit(self, clear_walls: bool = False) -> None:
        """Back to the start of setup: admin picks the map size, then edits walls."""
        self.phase = "SIZE"
        self.pick_idx = self.size_idx
        self.pick_view = 0 if self.view == "TOP" else 1
        self.preview, self.brush = False, 1
        self.bullets, self.powerups, self.blasts = [], [], []
        if clear_walls:
            self._apply_size(DEFAULT_SIZE)
            self.walls.clear()
            self.cursor = [self.cfg.width // 2, self.cfg.height // 2]
            self.view, self.pick_view = "TOP", 0
        for t in self.tanks.values():
            t.alive, t.clone, t.powerup = False, None, None
            t.lives = self.cfg.start_lives
            t.inbox.clear()

    def _start_round(self, now: float) -> None:
        self.phase = "PLAY"
        self.round_size = len(self.tanks)
        self.bullets, self.powerups = [], []
        self._next_powerup = now + self.cfg.powerup_every
        for t in self.tanks.values():
            t.lives, t.kills, t.deaths = self.cfg.start_lives, 0, 0
            t.alive, t.clone, t.powerup, t.respawn_at = False, None, None, now
            t.lob_mode, t.charge_n = False, 0
            t.inbox.clear()

    def push_input(self, t: Tank, key: str) -> bool:
        """Token-bucket rate limited, bounded input queue (anti-flood)."""
        now = self.clock()
        t.tokens = min(self.cfg.input_burst,
                       t.tokens + (now - t.last_refill) * self.cfg.input_rate)
        t.last_refill = now
        if t.tokens < 1:
            t.dropped += 1
            return False
        t.tokens -= 1
        t.inbox.append(key)
        return True

    # ---- helpers
    def in_bounds(self, x, y) -> bool:
        return 1 <= x <= self.cfg.width - 2 and 1 <= y <= self.cfg.height - 2

    def occupied(self, x, y, ignore: Optional[Tank] = None) -> bool:
        if (x, y) in self.walls:
            return True
        for t in self.tanks.values():
            if t.alive and ((t.x, t.y) == (x, y) or t.clone == (x, y)):
                if t is not ignore:
                    return True
        return ignore is not None and ignore.clone == (x, y)

    def spawn_point(self) -> Tuple[int, int]:
        W, H = self.cfg.width, self.cfg.height
        best, best_d = (W // 2, H // 2), -1
        others = [t for t in self.tanks.values() if t.alive]
        for _ in range(60):
            x, y = self.rng.randint(2, W - 3), self.rng.randint(2, H - 3)
            if (x, y) in self.walls:
                continue
            d = min((cheb(t.x, t.y, x, y) for t in others), default=99)
            if d > 4:
                return x, y
            if d > best_d:
                best, best_d = (x, y), d
        if best in self.walls:
            best = next(((x, y) for y in range(1, H - 1) for x in range(1, W - 1)
                         if (x, y) not in self.walls), best)
        return best                                  # crowded arena: farthest candidate

    # ---- actions
    def _give_powerup(self, t: Tank, kind: str, now: float) -> None:
        t.powerup, t.powerup_until, t.clone = kind, now + self.cfg.powerup_time, None
        if kind == "M":
            for ox, oy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1)):
                cx, cy = t.x + ox, t.y + oy
                if self.in_bounds(cx, cy) and not self.occupied(cx, cy, t):
                    t.clone = (cx, cy)
                    break

    def _fire(self, t: Tank, now: float) -> None:
        cd = self.cfg.rapid_cooldown if t.powerup == "R" else self.cfg.shot_cooldown
        if not t.alive or now - t.last_shot < cd:
            return
        t.last_shot = now
        pierce = t.powerup == "P"
        speed = self.cfg.bullet_speed + (1 if t.powerup == "S" else 0)
        if t.powerup == "T":
            dirs = ([(t.dx, -1), (t.dx, 0), (t.dx, 1)] if t.dx
                    else [(-1, t.dy), (0, t.dy), (1, t.dy)])
        else:
            dirs = [(t.dx, t.dy)]
        origins = [(t.x, t.y)] + ([t.clone] if t.clone else [])
        for ox, oy in origins:
            for dx, dy in dirs:
                self.bullets.append(Bullet(ox + dx, oy + dy, dx, dy, t.id, pierce, speed))

    def _move(self, t: Tank, key: str) -> None:
        dx, dy = MOVES[key]
        t.dx, t.dy = dx, dy
        steps = 2 if t.powerup == "S" else 1
        for _ in range(steps):
            nx, ny = t.x + dx, t.y + dy
            if self.in_bounds(nx, ny) and not self.occupied(nx, ny, t):
                t.x, t.y = nx, ny
            else:
                break
        if t.clone:                                  # clone mirrors movement
            cx, cy = t.clone
            for _ in range(steps):
                nx, ny = cx + dx, cy + dy
                if self.in_bounds(nx, ny) and (nx, ny) != (t.x, t.y) and not self.occupied(nx, ny, t):
                    cx, cy = nx, ny
                else:
                    break
            t.clone = (cx, cy)

    def _damage(self, t: Tank, killer_id: int, now: float) -> None:
        if t.clone:                                  # twin tank absorbs one hit
            t.clone = None
            return
        t.alive = False
        t.deaths += 1
        t.lives -= 1
        t.powerup, t.clone = None, None
        t.respawn_at = now + (self.cfg.respawn_delay if t.lives > 0 else 0)
        killer = self.tanks.get(killer_id)
        if killer and killer is not t:
            killer.kills += 1

    def _bullet_hits(self, b: Bullet, now: float) -> bool:
        """Apply hits for bullet at its current cell. True -> bullet is consumed."""
        for t in list(self.tanks.values()):
            if not t.alive or t.id == b.owner:
                continue
            if t.shielded(now):                      # shield: invulnerable, blocks normal rounds
                if not b.pierce and cheb(t.x, t.y, b.x, b.y) <= 1:
                    return True
                continue
            hit = False
            if (t.x, t.y) == (b.x, b.y):
                self._damage(t, b.owner, now)
                hit = True
            elif t.clone == (b.x, b.y):
                t.clone = None
                hit = True
            if hit and not b.pierce:
                return True
        return False

    # ---- simulation step
    def tick(self) -> None:
        now = self.clock()
        if self.phase == "PLAY":
            self._tick_play(now)
        elif self.phase == "SIZE":
            self._tick_size(now)
        elif self.phase == "VIEW":
            self._tick_view(now)
        elif self.phase == "EDIT":
            self._tick_edit(now)
        else:                                        # COUNTDOWN / WIN ignore input
            for t in self.tanks.values():
                t.inbox.clear()
            if now >= self.phase_until:
                if self.phase == "COUNTDOWN":
                    self._start_round(now)
                else:
                    self._enter_edit()

    def _tick_size(self, now: float) -> None:
        for t in self.tanks.values():
            while t.inbox:
                key = t.inbox.popleft()
                if t.id != self.admin_id:
                    continue
                if key in ("LEFT", "UP"):
                    self.pick_idx = max(0, self.pick_idx - 1)
                elif key in ("RIGHT", "DOWN"):
                    self.pick_idx = min(len(SIZES) - 1, self.pick_idx + 1)
                elif key in ("ENTER", "FIRE"):          # Enter (or a tap) confirms
                    self._apply_size(self.pick_idx)
                    self.phase = "VIEW"
                    for o in self.tanks.values():
                        o.inbox.clear()
                    return

    def _tick_view(self, now: float) -> None:
        for t in self.tanks.values():
            while t.inbox:
                key = t.inbox.popleft()
                if t.id != self.admin_id:
                    continue
                if key in ("LEFT", "UP"):
                    self.pick_view = max(0, self.pick_view - 1)
                elif key in ("RIGHT", "DOWN"):
                    self.pick_view = min(len(VIEWS) - 1, self.pick_view + 1)
                elif key in ("ENTER", "FIRE"):
                    self.view = VIEWS[self.pick_view][0]
                    if self.view == "TOP":               # top-down has one wall height
                        self.walls = {p: 1 for p in self.walls}
                    self.phase, self.brush, self.preview = "EDIT", 1, False
                    for o in self.tanks.values():
                        o.inbox.clear()
                    return

    def _tick_edit(self, now: float) -> None:
        iso = self.view == "ISO"
        for t in self.tanks.values():
            while t.inbox:
                key = t.inbox.popleft()
                if t.id != self.admin_id:
                    continue
                if key == "ROTATE":
                    if iso and self.preview:
                        t.cam = (t.cam + 1) % 4
                    continue
                if self.preview:                         # preview pans along screen diagonals
                    key = iso_key(key, t.cam)
                pos = (self.cursor[0], self.cursor[1])
                if key in MOVES:
                    dx, dy = MOVES[key]
                    nx, ny = pos[0] + dx, pos[1] + dy
                    if self.in_bounds(nx, ny):
                        self.cursor = [nx, ny]
                elif key == "FIRE":
                    h = self.brush if iso else 1
                    if self.walls.get(pos) == h:
                        del self.walls[pos]              # same wall again removes it
                    else:
                        self.walls[pos] = h
                elif iso and key in ("H1", "H2", "H3"):
                    self.brush = int(key[1])
                elif iso and key == "ERASE":
                    self.walls.pop(pos, None)
                elif iso and key == "CLEAR":             # press twice within 3s
                    if now < self._clear_armed:
                        self.walls.clear()
                        self._clear_armed = 0.0
                    else:
                        self._clear_armed = now + 3
                elif iso and key == "PREVIEW":
                    self.preview = not self.preview
                elif key == "ENTER":
                    self.phase, self.phase_until, self.preview = "COUNTDOWN", now + 3, False
                    for o in self.tanks.values():
                        o.inbox.clear()
                    return

    def _tick_play(self, now: float) -> None:
        cfg = self.cfg
        for t in list(self.tanks.values()):
            while t.inbox:
                key = t.inbox.popleft()
                if self.view == "ISO":
                    key = iso_key(key, t.cam)
                    if key == "ROTATE":
                        t.cam = (t.cam + 1) % 4
                        continue
                    if key == "TOGGLE":
                        t.lob_mode = not t.lob_mode
                        t.charge_n = 0
                        continue
                if key == "ENTER" and t.id == self.admin_id and self.round_size < 2:
                    self._enter_edit()               # solo round: admin ends it
                    return
                if not t.alive:
                    continue
                if key == "FIRE":
                    if self.view == "ISO" and t.lob_mode:
                        self._charge(t, now)
                    else:
                        self._fire(t, now)
                elif key in MOVES:
                    self._move(t, key)
        for t in self.tanks.values():
            if not t.alive and t.lives > 0 and now >= t.respawn_at:
                t.x, t.y = self.spawn_point()
                t.alive = True
                t.shield_until = now + cfg.shield_time
            if t.powerup and now >= t.powerup_until:
                t.powerup, t.clone = None, None
            if t.alive:
                for p in self.powerups[:]:
                    if (p[0], p[1]) == (t.x, t.y):
                        self.powerups.remove(p)
                        self._give_powerup(t, p[2], now)
        for t in self.tanks.values():
            self._release_lob(t, now)
        if now >= self._next_powerup:
            self._next_powerup = now + cfg.powerup_every
            if self.tanks and len(self.powerups) < cfg.powerup_max:
                for _ in range(100):
                    x = self.rng.randint(2, cfg.width - 3)
                    y = self.rng.randint(2, cfg.height - 3)
                    if not self.occupied(x, y) and all((p[0], p[1]) != (x, y) for p in self.powerups):
                        self.powerups.append([x, y, self.rng.choice(list(POWERUPS))])
                        break
        survivors = []
        self.blasts = [e for e in self.blasts if e[2] > now]
        for b in self.bullets:
            if b.lob:
                if self._step_lob(b, now):
                    survivors.append(b)
                continue
            dead = False
            for _ in range(b.speed):
                if (not self.in_bounds(b.x, b.y) or self._blocks_bullet(b.x, b.y)
                        or self._bullet_hits(b, now)):
                    dead = True
                    break
                b.x += b.dx
                b.y += b.dy
            if not dead:
                survivors.append(b)
        self.bullets = survivors
        if self.round_size >= 2:                     # last player with lives wins
            alive = [t for t in self.tanks.values() if t.lives > 0]
            if len(alive) <= 1:
                self.winner = alive[0].name if alive else ""
                self.phase, self.phase_until = "WIN", now + cfg.win_display

    # ---- lob shots (2.5D only). Terminals send no key-release events, so "holding Space"
    # is detected from the OS key-repeat: the charge runs from the first Space to the last
    # repeat, and the shell is released once the repeats stop.
    def _charge(self, t: Tank, now: float) -> None:
        if not t.alive:
            return
        if now < t.charge_lock:                      # still holding after an auto-fired max charge
            t.charge_lock = now + self.cfg.lob_repeat_gap * 1.5
            return
        if t.charge_n == 0:
            t.charge_first = now
        t.charge_n += 1
        t.charge_last = now

    def _release_lob(self, t: Tank, now: float) -> None:
        if t.charge_n == 0:
            return
        if not t.alive:
            t.charge_n = 0
            return
        cfg = self.cfg
        held = t.charge_last - t.charge_first
        limit = cfg.lob_first_gap if t.charge_n == 1 else cfg.lob_repeat_gap
        capped = held >= cfg.lob_max_charge
        if now - t.charge_last < limit and not capped:
            return
        t.charge_n = 0
        if capped:
            t.charge_lock = now + cfg.lob_repeat_gap * 1.5
        if now - t.last_shot < cfg.lob_cooldown:
            return
        t.last_shot = now
        frac = min(1.0, held / cfg.lob_max_charge)
        dist = cfg.lob_min_range + round(frac * (cfg.lob_max_range - cfg.lob_min_range))
        apex = cfg.lob_apex_min + frac ** 3 * (cfg.lob_apex_max - cfg.lob_apex_min)   # height ramps up late
        self.bullets.append(Bullet(t.x, t.y, t.dx, t.dy, t.id, False, 1, True, 0, dist, apex,
                                   cfg.lob_launch_h))

    @staticmethod
    def lob_height(b: Bullet, i: Optional[int] = None) -> float:
        i = b.steps if i is None else i
        u = i / b.dist
        h0 = b.launch
        return h0 + (b.apex - h0) * 4 * u * (1 - u)

    def _step_lob(self, b: Bullet, now: float) -> bool:
        """Advance a shell one tile. It clears a wall only if its arc is higher than the wall."""
        i = b.steps + 1
        nx, ny = b.x + b.dx, b.y + b.dy
        if not self.in_bounds(nx, ny):
            self._explode(b.x, b.y, b, now)
            return False
        if i >= b.dist:                              # landed
            self._explode(nx, ny, b, now)
            return False
        wall = self.walls.get((nx, ny), 0)
        if wall and self.lob_height(b, i) <= wall:   # too low: smacks into the wall
            self._explode(b.x, b.y, b, now)
            return False
        b.x, b.y, b.steps = nx, ny, i
        return True

    def _explode(self, x: int, y: int, b: Bullet, now: float) -> None:
        """3x3 blast. Shields absorb it; a twin tank absorbs one hit; the shooter is safe."""
        self.blasts.append([x, y, now + 0.35])
        for t in list(self.tanks.values()):
            if not t.alive or t.id == b.owner:
                continue
            near = cheb(t.x, t.y, x, y) <= 1 or (t.clone and cheb(t.clone[0], t.clone[1], x, y) <= 1)
            if near and not t.shielded(now):
                self._damage(t, b.owner, now)

    def _blocks_bullet(self, x: int, y: int) -> bool:
        h = self.walls.get((x, y), 0)
        if not h:
            return False
        return not (self.view == "ISO" and h == 1 and self.cfg.low_walls_pass)

    # ---- rendering
    @staticmethod
    def _bullet_char(dx, dy) -> str:
        if dx and dy:
            return "\\" if dx == dy else "/"
        return "|" if dy else "-"

    def _grid(self) -> List[List[str]]:
        W, H = self.cfg.width, self.cfg.height
        g = [[" "] * W for _ in range(H)]
        for x in range(W):
            g[0][x] = g[H - 1][x] = "#"
        for y in range(H):
            g[y][0] = g[y][W - 1] = "#"
        for (x, y), h in self.walls.items():
            g[y][x] = f"\x1b[{WALL_COL[h]}m{WALL_CH[h]}\x1b[0m"
        return g

    def _screen(self, lines: List[str]) -> List[str]:
        """Centered full-screen text frame with exactly height+HUD_ROWS rows."""
        W, total = MAX_W, MAX_H + HUD_ROWS
        rows = [""] * max(0, (total - len(lines)) // 2) + [l.center(W) for l in lines]
        rows = rows[:total] + [""] * (total - len(rows))
        return [r + "\x1b[K" for r in rows]

    def render(self, me: Tank, now: Optional[float] = None) -> List[str]:
        now = self.clock() if now is None else now
        if self.phase in ("SIZE", "VIEW", "EDIT"):
            return self._render_edit(me, now)
        if self.phase == "COUNTDOWN":
            n = min(3, int(max(0.0, self.phase_until - now)) + 1)
            art = [("".join(c * 3 for c in row)) for row in BIG[str(n)] for _ in (0, 1)]
            return self._screen(["GET READY", ""] + art + ["", "Arrows/WASD or swipe to move  Space or tap to shoot"]
                                 + (["2.5D: each arrow moves along its screen diagonal",
                                     "Q lob/straight   E rotate camera   hold Space = longer lob"]
                                    if self.view == "ISO" else []))
        if self.phase == "WIN":
            who = self.winner or "Nobody - it's a draw"
            return self._screen(["*** ROUND OVER ***", "", f"WINNER: {who}", "",
                                 "Back to the map editor shortly..."])
        return self._render_play(me, now)

    def _render_edit(self, me: Tank, now: float) -> List[str]:
        admin = self.tanks.get(self.admin_id)
        doing = {"SIZE": "choosing the map size",
                 "VIEW": "choosing the view mode"}.get(self.phase, "editing the map")
        if me.id != self.admin_id:                   # waiting room
            lines = ["=== WAITING ROOM ===", "",
                     f"{admin.name if admin else '?'} (admin) is {doing}...", "",
                     f"Players ({len(self.tanks)}):"]
            for t in list(self.tanks.values())[:12]:
                tag = " (admin)" if t.id == self.admin_id else (" (you)" if t is me else "")
                lines.append("- " + t.name + tag)
            lines += ["", "The game starts when the admin presses Enter.  Esc quit"]
            return self._screen(lines)
        if self.phase == "SIZE":                     # admin: size picker
            lines = ["=== CHOOSE MAP SIZE ===", "",
                     "Arrows/WASD/swipe to choose  -  Enter or tap to confirm", ""]
            for i, (name, w, h) in enumerate(SIZES):
                mark = ">" if i == self.pick_idx else " "
                lines.append(f"{mark} {name:<7} {w}x{h}  " + ("<" if i == self.pick_idx else " "))
            _, pw, ph = SIZES[self.pick_idx]
            lines += ["", "+" + "-" * (pw // 3) + "+"]
            lines += ["|" + " " * (pw // 3) + "|" for _ in range(ph // 3)]
            lines += ["+" + "-" * (pw // 3) + "+", "", f"Players: {len(self.tanks)}   Esc quit"]
            return self._screen(lines)
        if self.phase == "VIEW":                     # admin: view-mode picker
            lines = ["=== CHOOSE VIEW MODE ===", "",
                     "Arrows/WASD/swipe to choose  -  Enter or tap to confirm", ""]
            opts = [f"{'>' if i == self.pick_view else ' '} {name:<15} {desc}"
                    for i, (_, name, desc) in enumerate(VIEWS)]
            lines += [o.ljust(max(len(x) for x in opts)) for o in opts]
            art = (["##########", "#   H    #", "#        #", "##########"]
                   if self.pick_view == 0 else
                   ["  ______   ", " /     /|  ", "/_____/ |  ", "|     | /  ", "|_____|/   "])
            w = max(len(r) for r in art)
            lines += [""] + [r.ljust(w) for r in art]
            if self.pick_view == 1:
                lines += ["", "Wall heights:  # = 1 (low)   $ = 2   @ = 3"]
            lines += ["", f"Players: {len(self.tanks)}   Esc quit"]
            return self._screen(lines)
        iso = self.view == "ISO"
        if iso and self.preview:
            rows = self._iso_canvas(me, now, (self.cursor[0], self.cursor[1]), cursor=True, cam=me.cam).rows()
            return rows + self._edit_hud(now)
        g = self._grid()
        cx, cy = self.cursor
        ch = WALL_CH[self.brush] if iso else "#"
        g[cy][cx] = f"\x1b[1;33;7m{ch}\x1b[0m"
        return ["".join(r) for r in g] + self._edit_hud(now)

    def _edit_hud(self, now: float) -> List[str]:
        names = ", ".join(t.name for t in self.tanks.values())
        plist = f"Players: {names}"[:MAX_W] + "\x1b[K"
        if self.view != "ISO":
            return [f"MAP EDITOR - you are the admin | Walls: {len(self.walls)}\x1b[K",
                    "Arrows/WASD/swipe move | Space/tap wall | Enter start\x1b[K",
                    plist, "Esc quit\x1b[K"]
        brush = " ".join(f"\x1b[1;{WALL_COL[h]};7m{h}:{WALL_CH[h]}\x1b[0m" if h == self.brush
                         else f"{h}:{WALL_CH[h]}" for h in (1, 2, 3))
        title = "2.5D PREVIEW" if self.preview else "MAP EDITOR 2.5D"
        if now < self._clear_armed:
            l2 = "Press C again within 3s to CLEAR ALL walls"
        elif self.preview:
            l2 = "Arrows follow screen diagonals  Space place  X erase"
        else:
            l2 = "Arrows/WASD move  Space place  X erase  C,C clear all"
        return [f"{title} | Walls: {len(self.walls)} | Brush: {brush}\x1b[K", l2 + "\x1b[K", plist,
                "1/2/3 height  V preview  E rotate (preview)  Enter start  Esc quit\x1b[K"]

    def _render_play(self, me: Tank, now: float) -> List[str]:
        rows = self._iso_rows(me, now) if self.view == "ISO" else self._topdown_rows(me, now)
        return rows + self._hud(me, now)

    # ---- 2.5D isometric renderer
    @staticmethod
    def _iso_xy(x: int, y: int, tx: int, ty: int, cam: int = 0) -> Tuple[int, int]:
        """Screen (col,row) of a tile's top-left cell. Camera over corner (0,0)."""
        dx, dy = rot(x - tx, y - ty, cam)
        return MAX_W // 2 - 2 + 2 * (dx - dy), MAX_H // 2 - 1 - (dx + dy)

    @staticmethod
    def _iso_cube(cv: Canvas, sx: int, sy: int, h: int, faces: Tuple[int, int, int], ch: str) -> None:
        top, left, right = faces
        ty = sy - h
        ts = f"48;5;{top};38;5;16;1"
        for r in (0, 1):
            for c in range(4):
                cv.put(sx + c, ty + r, ch if (r == 0 and c in (1, 2)) else " ", ts)
        for r in range(ty + 2, sy + 2):
            for c in (0, 1):
                cv.put(sx + c, r, " ", f"48;5;{left}")
                cv.put(sx + 2 + c, r, " ", f"48;5;{right}")

    @staticmethod
    def _iso_tank(cv: Canvas, sx: int, sy: int, t: Tank, mine: bool, clone: bool, now: float,
                  cam: int = 0) -> None:
        """Tank sprite: 4x2 hull + turret with the barrel pointing along its facing."""
        r0, r1 = list(" () "), list("[==]")
        face = ISO_FACE.get(rot(t.dx, t.dy, cam), "NE")
        if face == "NE":
            r0[3] = "/"
        elif face == "NW":
            r0[0] = "\\"
        elif face == "SE":
            r1[3] = "\\"
        else:
            r1[0] = "/"
        style = "2;37" if clone else ("1;96" if t.shielded(now) else ("1;92" if mine else "1;97"))
        for i in range(4):
            if r0[i] != " ":
                cv.put(sx + i, sy, r0[i], style)
            cv.put(sx + i, sy + 1, r1[i], style)

    def _iso_canvas(self, me: Optional[Tank], now: float, target: Tuple[int, int],
                    cursor: bool = False, cam: int = 0) -> Canvas:
        cv = Canvas(MAX_W, MAX_H)
        W, H = self.cfg.width, self.cfg.height
        tx, ty = target
        ents: Dict[Tuple[int, int], list] = {}
        for t in self.tanks.values():
            if t.alive:
                ents.setdefault((t.x, t.y), []).append(("T", t, False))
                if t.clone:
                    ents.setdefault(t.clone, []).append(("T", t, True))
        for b in self.bullets:
            if not b.lob:
                ents.setdefault((b.x, b.y), []).append(("B", b, False))
        for p in self.powerups:
            ents.setdefault((p[0], p[1]), []).append(("P", p, False))
        for s in range(14, -15, -1):                 # far tiles first (painter's algorithm)
            for d in range(-17, 18):
                if (s + d) % 2:
                    continue
                wx, wy = unrot((s + d) // 2, (s - d) // 2, cam)
                x, y = tx + wx, ty + wy
                if not (0 <= x < W and 0 <= y < H):
                    continue
                sx, sy = MAX_W // 2 - 2 + 2 * d, MAX_H // 2 - 1 - s
                h = self.walls.get((x, y), 0)
                if x in (0, W - 1) or y in (0, H - 1):
                    self._iso_cube(cv, sx, sy, 1, ISO_FACES[0], "#")
                elif h:
                    self._iso_cube(cv, sx, sy, h, ISO_FACES[h], WALL_CH[h])
                else:
                    cv.put(sx + 1, sy, "\u00b7", "38;5;238")
                for kind, obj, clone in ents.get((x, y), ()):
                    if kind == "T":
                        if obj is me and not clone:
                            continue                 # own tank is drawn last, always visible
                        self._iso_tank(cv, sx, sy, obj, obj is me, clone, now, cam)
                    elif kind == "B":
                        cv.put(sx + 1, sy, "*", "1;36" if (me and obj.owner == me.id) else "1;31")
                    else:
                        cv.put(sx, sy, "<", "33")
                        cv.put(sx + 1, sy, obj[2], "1;33")
                        cv.put(sx + 2, sy, ">", "33")
        for b in self.bullets:                       # lob shells fly above everything else
            if b.lob:
                sx, sy = self._iso_xy(b.x, b.y, tx, ty, cam)
                lift = int(round(self.lob_height(b)))
                cv.put(sx + 1, sy, "\u00b7", "1;30")                       # ground shadow
                cv.put(sx + 1, sy - lift - 1, "O", "1;96" if (me and b.owner == me.id) else "1;91")
        for bx, by, _ in self.blasts:
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    if self.in_bounds(bx + ox, by + oy):
                        sx, sy = self._iso_xy(bx + ox, by + oy, tx, ty, cam)
                        cv.put(sx + 1, sy, "*", "1;93")
        if cursor:                                   # editor preview: floating brush marker
            sx, sy = self._iso_xy(self.cursor[0], self.cursor[1], tx, ty, cam)
            top = sy - self.brush
            for r in (0, 1):
                for c in range(4):
                    ch = ("[" if c == 0 else "]" if c == 3 else WALL_CH[self.brush]) if r == 0 else " "
                    cv.put(sx + c, top + r, ch, "48;5;226;38;5;16;1")
        elif me is not None and me.alive:            # x-ray: own tank shows through walls
            sx, sy = self._iso_xy(me.x, me.y, tx, ty, cam)
            self._iso_tank(cv, sx, sy, me, True, False, now, cam)
        return cv

    def _iso_rows(self, me: Tank, now: float) -> List[str]:
        if me.alive or (me.lives > 0 and (me.x, me.y) != (0, 0)):
            target = (me.x, me.y)
        else:                                        # spectate the first living tank
            alive = [t for t in self.tanks.values() if t.alive]
            target = ((alive[0].x, alive[0].y) if alive
                      else (self.cfg.width // 2, self.cfg.height // 2))
        return self._iso_canvas(me, now, target, cam=me.cam).rows()

    def _topdown_rows(self, me: Tank, now: float) -> List[str]:
        W, H = self.cfg.width, self.cfg.height
        g = self._grid()
        for t in self.tanks.values():
            if t.shielded(now):
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        if max(abs(dx), abs(dy)) == 1:
                            x, y = t.x + dx, t.y + dy
                            if 0 < x < W - 1 and 0 < y < H - 1:
                                g[y][x] = "+" if abs(dx) == abs(dy) else ("-" if dy else "|")
        for px, py, kind in self.powerups:
            g[py][px] = f"\x1b[1;33m{kind}\x1b[0m"
        for b in self.bullets:
            if 0 < b.x < W - 1 and 0 < b.y < H - 1:
                color = "36" if b.owner == me.id else "31"
                g[b.y][b.x] = f"\x1b[{color}m{self._bullet_char(b.dx, b.dy)}\x1b[0m"
        for t in self.tanks.values():
            if t.alive:
                if t.clone:
                    cx, cy = t.clone
                    g[cy][cx] = f"\x1b[2m{t.char}\x1b[0m"
                g[t.y][t.x] = f"\x1b[1;32m{t.char}\x1b[0m" if t is me else t.char
        return ["".join(r) for r in g]

    def _hud(self, me: Tank, now: float) -> List[str]:
        rows: List[str] = []
        hearts = "\u2665" * max(me.lives, 0) or "-"
        if me.alive:
            status = "SHIELD UP" if me.shielded(now) else "Fighting"
            if me.powerup:
                status += f" | {POWERUPS[me.powerup]} {max(0, me.powerup_until - now):.0f}s"
        elif me.lives <= 0:
            status = "OUT - spectating"
        else:
            status = f"Destroyed! Respawn in {max(0, me.respawn_at - now):.0f}s"
        top = sorted(self.tanks.values(), key=lambda t: (-t.kills, t.deaths, t.id))[:3]
        board = "  ".join(f"{i + 1}.{t.name} {t.kills}" for i, t in enumerate(top))
        rows.append(f"Lives: {hearts} | Kills: {me.kills} | Players: {len(self.tanks)} | {status}\x1b[K")
        rows.append(f"Top: {board}\x1b[K")
        solo = " | Enter: back to editor" if (me.id == self.admin_id and self.round_size < 2) else ""
        if self.view == "ISO":
            mode = "LOB" if me.lob_mode else "STRAIGHT"
            rows.append(f"WASD move  Space shoot (hold = lob power)  Q mode:{mode}  E rotate  Esc quit"
                        f" | T R P M S{solo}\x1b[K")
            if me.charge_n and me.alive:
                cfg = self.cfg
                frac = min(1.0, (me.charge_last - me.charge_first) / cfg.lob_max_charge)
                n = int(round(frac * 10))
                tiles = cfg.lob_min_range + round(frac * (cfg.lob_max_range - cfg.lob_min_range))
                bar = "#" * n + " " * (10 - n)
                rows.append(f"Lob power [{bar}] ~{tiles} tiles - release Space to fire\x1b[K")
            else:
                rows.append(f"Invite friends: {connect_hint(self.cfg)}\x1b[K")
        else:
            rows.append("Arrows/WASD/swipe move  Space/tap shoot  Esc quit | "
                        f"T=triple R=rapid P=pierce M=twin S=speed{solo}\x1b[K")
            rows.append(f"Invite friends: {connect_hint(self.cfg)}\x1b[K")
        return rows


# --------------------------------------------------------------------------- network
def sanitize_name(raw: Optional[str], fallback: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]", "", raw or "")[:12]
    return name or fallback


class Session:
    """One player's connection: reads keys, writes only the screen rows that changed."""

    def __init__(self, process, tank: Tank, cfg: Config):
        self.process, self.tank, self.cfg = process, tank, cfg
        self.out = process.stdout
        self.parser = KeyParser()
        self.last_rows: Optional[List[str]] = None
        self.warned = False
        self.size_ok = True
        self.closed = False
        self.update_size()

    def update_size(self) -> None:
        try:
            cols, rows = self.process.get_terminal_size()[:2]
        except Exception:
            cols = rows = 0
        self.cols, self.rows = cols, rows
        self.size_ok = cols == 0 or (cols >= MAX_W and rows >= MAX_H + HUD_ROWS)
        self.last_rows, self.warned = None, False

    def write(self, s: str) -> None:
        if self.closed:
            return
        try:
            self.out.write(s)
        except Exception:
            self.closed = True

    def backlog(self) -> int:
        try:
            return self.out.channel.get_write_buffer_size()
        except Exception:
            return 0

    def send(self, rows: List[str]) -> None:
        if not self.size_ok:
            if not self.warned:
                self.warned = True
                self.write(f"\x1b[2J\x1b[HTerminal too small: need {MAX_W}x"
                           f"{MAX_H + HUD_ROWS}, have {self.cols}x{self.rows}. Resize the window.")
            return
        if self.backlog() > BACKLOG_LIMIT:           # slow client: drop frame, never queue
            self.last_rows = None
            return
        full = self.last_rows is None or len(self.last_rows) != len(rows)   # size changed -> redraw
        parts = ["\x1b[2J"] if full else []
        for i, row in enumerate(rows):
            if full or self.last_rows[i] != row:
                parts.append(f"\x1b[{i + 1};1H{row}")
        if parts:
            self.write("".join(parts))
        self.last_rows = rows

    async def read_loop(self, game: Game) -> None:
        while not self.closed:
            esc = self.parser.pending_escape
            try:
                data = await asyncio.wait_for(self.process.stdin.read(256),
                                              0.08 if esc else self.cfg.idle_timeout)
            except TerminalSizeChanged:
                self.update_size()
                continue
            except asyncio.TimeoutError:
                if esc:                              # nothing followed the ESC: it was the Esc key
                    self.parser.buf = ""
                    return
                self.write("\x1b[2J\x1b[HDisconnected: idle too long.\r\n")
                return
            if not data:
                return
            for key in self.parser.feed(data):
                if key == "QUIT":
                    return
                game.push_input(self.tank, key)


class TankServer:
    def __init__(self, cfg: Config, game: Optional[Game] = None):
        self.cfg = cfg
        self.game = game or Game(cfg)
        self.sessions: Dict[int, Session] = {}
        self.ip_counts: Counter = Counter()

    # connection admission (called from SSHServer.connection_made)
    def admit(self, ip: str) -> bool:
        if self.ip_counts[ip] >= self.cfg.max_per_ip:
            log.warning("rejecting %s: too many connections", ip)
            return False
        self.ip_counts[ip] += 1
        return True

    def release(self, ip: str) -> None:
        self.ip_counts[ip] -= 1
        if self.ip_counts[ip] <= 0:
            del self.ip_counts[ip]

    async def handle(self, process) -> None:
        peer = process.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        if process.get_terminal_type() is None:
            process.stdout.write("This game needs a terminal. Connect with: ssh -t -p "
                                 f"{self.cfg.port} <host>\r\n")
            process.exit(1)
            return
        if len(self.sessions) >= self.cfg.max_players:
            process.stdout.write("Server full, try again soon.\r\n")
            process.exit(1)
            return
        tank = self.game.add_player("")
        tank.name = sanitize_name(process.get_extra_info("username"), f"tank{tank.id}")
        sess = Session(process, tank, self.cfg)
        self.sessions[tank.id] = sess
        log.info("join: %s from %s (%d online)", tank.name, ip, len(self.sessions))
        sess.write(ENTER)
        try:
            await sess.read_loop(self.game)
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("session error for %s", tank.name)
        finally:
            sess.closed = True
            self.sessions.pop(tank.id, None)
            self.game.remove_player(tank)
            log.info("leave: %s (kills=%d deaths=%d dropped_inputs=%d)",
                     tank.name, tank.kills, tank.deaths, tank.dropped)
            try:
                process.stdout.write(LEAVE + "Thanks for playing!\r\n")
                process.exit(0)
            except Exception:
                pass

    def broadcast(self) -> None:
        now = self.game.clock()
        for s in list(self.sessions.values()):
            s.send(self.game.render(s.tank, now))

    async def game_loop(self) -> None:
        """Fixed timestep with drift correction; resyncs instead of spiralling if overloaded."""
        step = self.cfg.tick
        next_t = time.monotonic()
        while True:
            self.game.tick()
            self.broadcast()
            next_t += step
            delay = next_t - time.monotonic()
            if delay < -1.0:
                log.warning("server overloaded, skipping %.1fs", -delay)
                next_t = time.monotonic()
                delay = 0
            await asyncio.sleep(max(0.0, delay))


if asyncssh:
    class GameSSHServer(asyncssh.SSHServer):
        def __init__(self, mgr: TankServer):
            self.mgr, self.ip, self.admitted = mgr, "?", False

        def connection_made(self, conn):
            peer = conn.get_extra_info("peername")
            self.ip = peer[0] if peer else "?"
            self.admitted = self.mgr.admit(self.ip)
            if not self.admitted:
                conn.abort()

        def connection_lost(self, exc):
            if self.admitted:
                self.mgr.release(self.ip)

        def begin_auth(self, username):
            return bool(self.mgr.cfg.password)       # False -> no auth required

        def password_auth_supported(self):
            return bool(self.mgr.cfg.password)

        def validate_password(self, username, password):
            return hmac.compare_digest(password.encode(), self.mgr.cfg.password.encode())


def ensure_host_key(path: str) -> str:
    if not os.path.exists(path):
        key = asyncssh.generate_private_key("ssh-ed25519")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key.export_private_key())
        log.info("generated host key %s", path)
    return path


async def serve(cfg: Config) -> None:
    mgr = TankServer(cfg)
    server = await asyncssh.create_server(
        lambda: GameSSHServer(mgr), cfg.host, cfg.port,
        server_host_keys=[ensure_host_key(cfg.host_key)],
        process_factory=mgr.handle,
        line_editor=False, encoding="utf-8", errors="replace",
        login_timeout=20, keepalive_interval=15, keepalive_count_max=3,
        compression_algs=None, reuse_address=True)
    log.info("tanks listening on port %d (auth: %s)", cfg.port,
             "password" if cfg.password else "open")
    banner = ["=" * 56, " Tanks server is up. Players can join with:"]
    banner += [f"     ssh -p {cfg.port} {h}" for h in (cfg.advertise or ["<server-ip>"])]
    banner += ["     (same machine: ssh -p %d localhost)" % cfg.port, "=" * 56]
    print("\n".join(banner), flush=True)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:                  # Windows
            pass
    task = asyncio.create_task(mgr.game_loop())
    try:
        await stop.wait()
    finally:
        task.cancel()
        server.close()
        await server.wait_closed()
        log.info("shut down")


def main() -> None:
    if asyncssh is None:
        raise SystemExit("asyncssh is required: pip install asyncssh")
    ap = argparse.ArgumentParser(description="ASCII tanks over SSH")
    ap.add_argument("--host", default="")
    ap.add_argument("--port", type=int, default=1111)
    ap.add_argument("--max-players", type=int, default=30)
    ap.add_argument("--max-per-ip", type=int, default=4)
    ap.add_argument("--host-key", default="ssh_host_key")
    ap.add_argument("--public-host", action="append", default=[],
                    help="address/hostname to show players (repeatable); "
                         "use this for your public IP or DNS name")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config(host=a.host, port=a.port, max_players=a.max_players,
                 max_per_ip=a.max_per_ip, host_key=a.host_key,
                 password=os.environ.get("TANKS_PASSWORD") or None)
    if a.public_host:
        cfg.advertise = a.public_host
    elif a.host and a.host not in ("0.0.0.0", "::"):
        cfg.advertise = [a.host]
    else:
        cfg.advertise = detect_addresses()
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()