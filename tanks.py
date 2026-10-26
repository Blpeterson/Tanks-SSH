#!/usr/bin/env python3
"""ASCII multiplayer tank game over SSH on port 1111."""
import asyncio, asyncssh, os, random, re, time

PORT = 1111
W, H = 60, 20            # arena size (including border)
TICK = 0.08              # seconds per frame
RESPAWN_DELAY = 10
SHIELD_TIME = 3
SHOT_COOLDOWN = 0.3
BULLET_SPEED = 2         # cells per tick
START_LIVES = 3
GAMEOVER_DELAY = 15      # wait after losing the last life, then rejoin with fresh lives
POWERUP_TIME = 10
POWERUP_EVERY = 8
POWERUP_MAX = 3
HKEY = "ssh_host_key"

POWERUPS = {
    "T": "Triple Shot",
    "R": "Rapid Fire",
    "P": "Piercing Round",
    "M": "Twin Tank",
    "S": "Speed Boost",
}

tanks = {}               # id -> Tank
bullets = []             # [x, y, dx, dy, owner_id, pierce, speed]
powerups = []            # [x, y, kind]
next_id = 0
next_powerup_at = 0


class Tank:
    def __init__(self, tid, out):
        self.id, self.out = tid, out
        self.x = self.y = 0
        self.dx, self.dy = 1, 0
        self.vertical = False        # True -> 'H', False -> I-beam
        self.alive = False
        self.respawn_at = 0          # spawns on first tick
        self.shield_until = 0
        self.last_shot = 0
        self.kills = 0
        self.lives = START_LIVES
        self.powerup = None          # key into POWERUPS
        self.powerup_until = 0
        self.clone = None            # (x, y) of Twin Tank clone

    @property
    def shielded(self):
        return self.alive and time.time() < self.shield_until

    @property
    def char(self):
        return "H" if self.vertical else "\u2336"


def cell_taken(x, y, ignore=None):
    for t in tanks.values():
        if t.alive and t is not ignore:
            if (t.x, t.y) == (x, y) or t.clone == (x, y):
                return True
    return False


def spawn_point():
    for _ in range(300):
        x, y = random.randint(2, W - 3), random.randint(2, H - 3)
        if all(not t.alive or max(abs(t.x - x), abs(t.y - y)) > 4
               for t in tanks.values()):
            return x, y
    return random.randint(2, W - 3), random.randint(2, H - 3)


def occupied(x, y, ignore=None):
    if cell_taken(x, y, ignore):
        return True
    if ignore is not None and ignore.clone == (x, y):
        return True
    return False


def free_cell(x, y):
    return 1 <= x <= W - 2 and 1 <= y <= H - 2


def give_powerup(t, kind, now):
    t.powerup = kind
    t.powerup_until = now + POWERUP_TIME
    t.clone = None
    if kind == "M":
        for ox, oy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1)):
            cx, cy = t.x + ox, t.y + oy
            if free_cell(cx, cy) and not occupied(cx, cy, t):
                t.clone = (cx, cy)
                break


def pickup_check(t, now):
    for p in powerups[:]:
        if (p[0], p[1]) == (t.x, t.y):
            powerups.remove(p)
            give_powerup(t, p[2], now)


def fire(t, now):
    cd = 0.08 if t.powerup == "R" else SHOT_COOLDOWN
    if not t.alive or now - t.last_shot < cd:
        return
    t.last_shot = now
    pierce = t.powerup == "P"
    speed = BULLET_SPEED + (1 if t.powerup == "S" else 0)
    if t.powerup == "T":
        if t.dx:
            dirs = [(t.dx, -1), (t.dx, 0), (t.dx, 1)]
        else:
            dirs = [(-1, t.dy), (0, t.dy), (1, t.dy)]
    else:
        dirs = [(t.dx, t.dy)]
    origins = [(t.x, t.y)]
    if t.clone:
        origins.append(t.clone)
    for ox, oy in origins:
        for dx, dy in dirs:
            bullets.append([ox + dx, oy + dy, dx, dy, t.id, pierce, speed])


def handle_key(t, key):
    now = time.time()
    if key == " ":
        fire(t, now)
        return
    moves = {"UP": (0, -1, True), "DOWN": (0, 1, True),
             "LEFT": (-1, 0, False), "RIGHT": (1, 0, False)}
    if key in moves and t.alive:
        dx, dy, vert = moves[key]
        t.dx, t.dy, t.vertical = dx, dy, vert
        steps = 2 if t.powerup == "S" else 1
        for _ in range(steps):
            nx, ny = t.x + dx, t.y + dy
            if free_cell(nx, ny) and not occupied(nx, ny, t):
                t.x, t.y = nx, ny
            else:
                break
        if t.clone:                       # clone mirrors the move
            cx, cy = t.clone
            for _ in range(steps):
                nx, ny = cx + dx, cy + dy
                if (free_cell(nx, ny) and (nx, ny) != (t.x, t.y)
                        and not occupied(nx, ny, t)):
                    cx, cy = nx, ny
                else:
                    break
            t.clone = (cx, cy)
        pickup_check(t, now)


def damage(t, killer_id, now):
    """Main tank was hit."""
    if t.clone:                           # clone absorbs the hit
        t.x, t.y = t.clone
        t.clone = None
        return
    t.alive = False
    t.lives -= 1
    t.powerup = None
    t.clone = None
    t.respawn_at = now + (RESPAWN_DELAY if t.lives > 0 else GAMEOVER_DELAY)
    if killer_id in tanks:
        tanks[killer_id].kills += 1


def bullet_hit(b, now):
    """Returns True if the bullet should be destroyed."""
    x, y, _, _, owner, pierce, _ = b
    stop = False
    for t in list(tanks.values()):
        if not t.alive or t.id == owner:
            continue
        if t.shielded and not pierce and max(abs(t.x - x), abs(t.y - y)) <= 1:
            return True
        if (t.x, t.y) == (x, y):
            damage(t, owner, now)
            stop = True
        elif t.clone == (x, y):
            t.clone = None
            stop = True
        if stop and not pierce:
            return True
    return stop and not pierce


def step_game():
    global next_powerup_at
    now = time.time()
    for t in tanks.values():
        if not t.alive and now >= t.respawn_at:
            if t.lives <= 0:
                t.lives = START_LIVES      # game over is over: fresh lives
            t.x, t.y = spawn_point()
            t.alive = True
            t.shield_until = now + SHIELD_TIME
        if t.powerup and now >= t.powerup_until:
            t.powerup = None
            t.clone = None
    # power-up spawning
    if now >= next_powerup_at:
        next_powerup_at = now + POWERUP_EVERY
        if len(powerups) < POWERUP_MAX and tanks:
            for _ in range(100):
                x, y = random.randint(2, W - 3), random.randint(2, H - 3)
                if not cell_taken(x, y) and all((p[0], p[1]) != (x, y) for p in powerups):
                    powerups.append([x, y, random.choice(list(POWERUPS))])
                    break
    # bullets
    alive_bullets = []
    for b in bullets:
        dead = False
        for _ in range(b[6]):
            if not (1 <= b[0] <= W - 2 and 1 <= b[1] <= H - 2):
                dead = True
                break
            if bullet_hit(b, now):
                dead = True
                break
            b[0] += b[2]
            b[1] += b[3]
        if not dead:
            alive_bullets.append(b)
    bullets[:] = alive_bullets


def bullet_char(dx, dy):
    if dx and dy:
        return "\\" if dx == dy else "/"
    return "|" if dy else "-"


def render_for(me):
    now = time.time()
    g = [[" "] * W for _ in range(H)]
    for x in range(W):
        g[0][x] = g[H - 1][x] = "#"
    for y in range(H):
        g[y][0] = g[y][W - 1] = "#"
    for t in tanks.values():             # shield boxes
        if t.shielded:
            for dx in range(-1, 2):
                for dy in range(-1, 2):
                    if max(abs(dx), abs(dy)) == 1:
                        x, y = t.x + dx, t.y + dy
                        if 0 < x < W - 1 and 0 < y < H - 1:
                            g[y][x] = ("+" if abs(dx) == abs(dy) else
                                       "-" if dy else "|")
    for px, py, kind in powerups:
        g[py][px] = f"\x1b[1;33m{kind}\x1b[0m"
    for bx, by, bdx, bdy, *_ in bullets:
        if 0 < bx < W - 1 and 0 < by < H - 1:
            g[by][bx] = bullet_char(bdx, bdy)
    for t in tanks.values():
        if t.alive:
            if t.clone:
                cx, cy = t.clone
                g[cy][cx] = f"\x1b[2m{t.char}\x1b[0m"
            g[t.y][t.x] = (f"\x1b[1;32m{t.char}\x1b[0m" if t is me else t.char)
    lines = ["".join(r) for r in g]
    hearts = "\u2665" * max(me.lives, 0) or "-"
    if me.alive:
        status = "SHIELD UP" if me.shielded else "Fighting"
    elif me.lives <= 0:
        status = f"GAME OVER - rejoining in {max(0, me.respawn_at - now):.0f}s"
    else:
        status = f"Destroyed! Respawn in {max(0, me.respawn_at - now):.0f}s"
    if me.powerup and me.alive:
        status += f" | {POWERUPS[me.powerup]} {max(0, me.powerup_until - now):.0f}s"
    lines.append(f"\x1b[2KLives: {hearts} | Kills: {me.kills} | Players: {len(tanks)} | {status}")
    lines.append("\x1b[2KArrows: move  Space: shoot  Q: quit   "
                 "Power-ups: T=triple R=rapid P=pierce M=twin S=speed")
    return "\x1b[H" + "\r\n".join(lines)


async def game_loop():
    while True:
        step_game()
        for t in list(tanks.values()):
            try:
                t.out.write(render_for(t))
            except Exception:
                pass
        await asyncio.sleep(TICK)


KEY_RE = re.compile(r"\x1b\[([ABCD])|\x1bO([ABCD])|(.)", re.S)
ARROWS = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}


async def handle_client(process):
    global next_id
    next_id += 1
    t = Tank(next_id, process.stdout)
    tanks[t.id] = t
    process.stdout.write("\x1b[2J\x1b[?25l")        # clear, hide cursor
    try:
        quit_ = False
        while not quit_:
            data = await process.stdin.read(64)
            if not data:
                break
            for m in KEY_RE.finditer(data):
                arrow = m.group(1) or m.group(2)
                key = ARROWS[arrow] if arrow else m.group(3)
                if key in ("q", "Q", "\x03", "\x04"):
                    quit_ = True
                    break
                handle_key(t, key)
    except (asyncssh.TerminalSizeChanged, ConnectionError):
        pass
    finally:
        tanks.pop(t.id, None)
        try:
            process.stdout.write("\x1b[?25h\x1b[2J\x1b[HThanks for playing!\r\n")
            process.exit(0)
        except Exception:
            pass


class OpenServer(asyncssh.SSHServer):
    def begin_auth(self, username):
        return False        # no authentication required


async def main():
    if not os.path.exists(HKEY):
        asyncssh.generate_private_key("ssh-rsa").write_private_key(HKEY)
    await asyncssh.create_server(
        OpenServer, "", PORT,
        server_host_keys=[HKEY],
        process_factory=handle_client,
        line_editor=False,          # raw keystrokes
        encoding="utf-8",
    )
    print(f"Tank game listening on port {PORT}")
    await game_loop()


if __name__ == "__main__":
    asyncio.run(main())