#!/usr/bin/env python3
"""
ASCII multiplayer tanks over SSH.

Round flow: the first player in is the admin and edits the map (arrows/WASD move the
'#' cursor, Space toggles a wall, Enter starts). Others wait in a lobby. After a 3-2-1
countdown, last player with lives wins, then everyone returns to the map editor.

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

# 5-row block digits for the full-screen countdown
BIG = {
    "3": [" ##### ", "     ##", "  #### ", "     ##", " ##### "],
    "2": [" ##### ", "     ##", " ##### ", "##     ", "#######"],
    "1": ["   ##  ", " ####  ", "   ##  ", "   ##  ", " ######"],
}
ENTER = "\x1b[?1049h\x1b[2J\x1b[?25l"      # alt screen, clear, hide cursor
LEAVE = "\x1b[?25h\x1b[?1049l"             # show cursor, leave alt screen
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
    """Turns raw terminal bytes into UP/DOWN/LEFT/RIGHT/FIRE/QUIT tokens."""
    ARROWS = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}
    PLAIN = {" ": "FIRE", "w": "UP", "s": "DOWN", "a": "LEFT", "d": "RIGHT",
             "W": "UP", "S": "DOWN", "A": "LEFT", "D": "RIGHT",
             "\r": "ENTER", "\n": "ENTER",
             "q": "QUIT", "Q": "QUIT", "\x03": "QUIT", "\x04": "QUIT"}

    def __init__(self):
        self.buf = ""

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
        self.buf = buf[i:] if (n - i) <= 16 else ""      # never grow unbounded
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

    @property
    def vertical(self) -> bool:
        return self.dy != 0

    @property
    def char(self) -> str:
        return "H" if self.vertical else "\u2336"

    def shielded(self, now: float) -> bool:
        return self.alive and now < self.shield_until


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
        self.walls: set = set()                     # {(x, y)}
        self.phase = "EDIT"                         # EDIT | COUNTDOWN | PLAY | WIN
        self.admin_id: Optional[int] = None
        self.cursor = [cfg.width // 2, cfg.height // 2]
        self.phase_until = 0.0
        self.round_size = 0
        self.winner = ""

    # ---- players
    def add_player(self, name: str) -> Tank:
        self._next_id += 1
        now = self.clock()
        joinable = self.phase in ("EDIT", "COUNTDOWN")   # mid-round joiners spectate
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
    def _enter_edit(self, clear_walls: bool = False) -> None:
        self.phase = "EDIT"
        self.bullets, self.powerups = [], []
        if clear_walls:
            self.walls.clear()
            self.cursor = [self.cfg.width // 2, self.cfg.height // 2]
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

    def _tick_edit(self, now: float) -> None:
        for t in self.tanks.values():
            while t.inbox:
                key = t.inbox.popleft()
                if t.id != self.admin_id:
                    continue
                if key in MOVES:
                    dx, dy = MOVES[key]
                    nx, ny = self.cursor[0] + dx, self.cursor[1] + dy
                    if self.in_bounds(nx, ny):
                        self.cursor = [nx, ny]
                elif key == "FIRE":
                    self.walls.symmetric_difference_update({(self.cursor[0], self.cursor[1])})
                elif key == "ENTER":
                    self.phase, self.phase_until = "COUNTDOWN", now + 3
                    for o in self.tanks.values():
                        o.inbox.clear()
                    return

    def _tick_play(self, now: float) -> None:
        cfg = self.cfg
        for t in list(self.tanks.values()):
            while t.inbox:
                key = t.inbox.popleft()
                if key == "ENTER" and t.id == self.admin_id and self.round_size < 2:
                    self._enter_edit()               # solo round: admin ends it
                    return
                if not t.alive:
                    continue
                if key == "FIRE":
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
        for b in self.bullets:
            dead = False
            for _ in range(b.speed):
                if (not self.in_bounds(b.x, b.y) or (b.x, b.y) in self.walls
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
        for x, y in self.walls:
            g[y][x] = "\x1b[35m#\x1b[0m"
        return g

    def _screen(self, lines: List[str]) -> List[str]:
        """Centered full-screen text frame with exactly height+HUD_ROWS rows."""
        W, total = self.cfg.width, self.cfg.height + HUD_ROWS
        rows = [""] * max(0, (total - len(lines)) // 2) + [l.center(W) for l in lines]
        rows = rows[:total] + [""] * (total - len(rows))
        return [r + "\x1b[K" for r in rows]

    def render(self, me: Tank, now: Optional[float] = None) -> List[str]:
        now = self.clock() if now is None else now
        if self.phase == "EDIT":
            return self._render_edit(me)
        if self.phase == "COUNTDOWN":
            n = min(3, int(max(0.0, self.phase_until - now)) + 1)
            art = [("".join(c * 3 for c in row)) for row in BIG[str(n)] for _ in (0, 1)]
            return self._screen(["GET READY", ""] + art + ["", "Arrows/WASD move  Space shoot"])
        if self.phase == "WIN":
            who = self.winner or "Nobody - it's a draw"
            return self._screen(["*** ROUND OVER ***", "", f"WINNER: {who}", "",
                                 "Back to the map editor shortly..."])
        return self._render_play(me, now)

    def _render_edit(self, me: Tank) -> List[str]:
        admin = self.tanks.get(self.admin_id)
        if me.id != self.admin_id:                   # waiting room
            lines = ["=== WAITING ROOM ===", "",
                     f"{admin.name if admin else '?'} (admin) is editing the map...", "",
                     f"Players ({len(self.tanks)}):"]
            for t in list(self.tanks.values())[:12]:
                tag = " (admin)" if t.id == self.admin_id else (" (you)" if t is me else "")
                lines.append(f"- {t.name}{tag}")
            lines += ["", "The game starts when the admin presses Enter.  Q quit"]
            return self._screen(lines)
        g = self._grid()
        cx, cy = self.cursor
        g[cy][cx] = "\x1b[1;33;7m#\x1b[0m"
        rows = ["".join(r) for r in g]
        names = ", ".join(t.name for t in self.tanks.values())
        rows.append(f"MAP EDITOR - you are the admin | Walls: {len(self.walls)}\x1b[K")
        rows.append("Arrows/WASD move cursor | Space place/remove wall | Enter start game\x1b[K")
        rows.append(f"Players: {names}"[: self.cfg.width] + "\x1b[K")
        rows.append("Q quit\x1b[K")
        return rows

    def _render_play(self, me: Tank, now: float) -> List[str]:
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
        rows = ["".join(r) for r in g]

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
        rows.append("Arrows/WASD move  Space shoot  Q quit | "
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
        self.size_ok = cols == 0 or (cols >= self.cfg.width and rows >= self.cfg.height + HUD_ROWS)
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
                self.write(f"\x1b[2J\x1b[HTerminal too small: need {self.cfg.width}x"
                           f"{self.cfg.height + HUD_ROWS}, have {self.cols}x{self.rows}. Resize the window.")
            return
        if self.backlog() > BACKLOG_LIMIT:           # slow client: drop frame, never queue
            self.last_rows = None
            return
        parts = ["\x1b[2J"] if self.last_rows is None else []
        for i, row in enumerate(rows):
            if self.last_rows is None or self.last_rows[i] != row:
                parts.append(f"\x1b[{i + 1};1H{row}")
        if parts:
            self.write("".join(parts))
        self.last_rows = rows

    async def read_loop(self, game: Game) -> None:
        while not self.closed:
            try:
                data = await asyncio.wait_for(self.process.stdin.read(256), self.cfg.idle_timeout)
            except TerminalSizeChanged:
                self.update_size()
                continue
            except asyncio.TimeoutError:
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