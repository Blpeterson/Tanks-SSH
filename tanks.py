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
HKEY = "ssh_host_key"

tanks = {}               # id -> Tank
bullets = []             # [x, y, dx, dy, owner_id]
next_id = 0


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

    @property
    def shielded(self):
        return self.alive and time.time() < self.shield_until

    @property
    def char(self):
        return "H" if self.vertical else "\u2336"


def spawn_point():
    for _ in range(300):
        x, y = random.randint(2, W - 3), random.randint(2, H - 3)
        if all(not t.alive or max(abs(t.x - x), abs(t.y - y)) > 4
               for t in tanks.values()):
            return x, y
    return random.randint(2, W - 3), random.randint(2, H - 3)


def occupied(x, y, ignore=None):
    return any(t.alive and t is not ignore and (t.x, t.y) == (x, y)
               for t in tanks.values())


def handle_key(t, key):
    now = time.time()
    if key == " ":
        if t.alive and now - t.last_shot >= SHOT_COOLDOWN:
            t.last_shot = now
            bx, by = t.x + t.dx, t.y + t.dy
            bullets.append([bx, by, t.dx, t.dy, t.id])
        return
    moves = {"UP": (0, -1, True), "DOWN": (0, 1, True),
             "LEFT": (-1, 0, False), "RIGHT": (1, 0, False)}
    if key in moves and t.alive:
        dx, dy, vert = moves[key]
        t.dx, t.dy, t.vertical = dx, dy, vert
        nx, ny = t.x + dx, t.y + dy
        if 1 <= nx <= W - 2 and 1 <= ny <= H - 2 and not occupied(nx, ny, t):
            t.x, t.y = nx, ny


def step_game():
    now = time.time()
    for t in tanks.values():
        if not t.alive and now >= t.respawn_at:
            t.x, t.y = spawn_point()
            t.alive = True
            t.shield_until = now + SHIELD_TIME
    alive_bullets = []
    for b in bullets:
        dead = False
        for _ in range(BULLET_SPEED):
            if not (1 <= b[0] <= W - 2 and 1 <= b[1] <= H - 2):
                dead = True
                break
            for t in tanks.values():
                if not t.alive or t.id == b[4]:
                    continue
                if t.shielded and max(abs(t.x - b[0]), abs(t.y - b[1])) <= 1:
                    dead = True          # shield absorbs bullet
                    break
                if (t.x, t.y) == (b[0], b[1]):
                    t.alive = False
                    t.respawn_at = now + RESPAWN_DELAY
                    if b[4] in tanks:
                        tanks[b[4]].kills += 1
                    dead = True
                    break
            if dead:
                break
            b[0] += b[2]
            b[1] += b[3]
        if not dead:
            alive_bullets.append(b)
    bullets[:] = alive_bullets


def render_for(me):
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
    for bx, by, *_ in bullets:
        if 0 < bx < W - 1 and 0 < by < H - 1:
            g[by][bx] = "-"
    for t in tanks.values():
        if t.alive:
            g[t.y][t.x] = (f"\x1b[1;32m{t.char}\x1b[0m" if t is me else t.char)
    lines = ["".join(r) for r in g]
    if me.alive:
        status = "SHIELD UP" if me.shielded else "Fighting"
    else:
        status = f"Destroyed! Respawn in {max(0, me.respawn_at - time.time()):.0f}s"
    lines.append(f"\x1b[2KKills: {me.kills} | Players: {len(tanks)} | {status}")
    lines.append("\x1b[2KArrows: move  Space: shoot  Q: quit")
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
