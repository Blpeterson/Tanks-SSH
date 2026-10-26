<div align="center">

```
 _____  _    _   _ _  __ ____       ____ ____  _   _
|_   _|/ \  | \ | | |/ // ___|     / ___/ ___|| | | |
  | | / _ \ |  \| | ' / \___ \ ____\___ \___ \| |_| |
  | |/ ___ \| |\  | . \  ___) |_____|__) |__) |  _  |
  |_/_/   \_\_| \_|_|\_\|____/     |____/____/|_| |_|
```

**Last-tank-standing multiplayer ASCII battles, played over plain SSH.**

![Python](https://img.shields.io/badge/python-3.8%2B-blue) ![asyncssh](https://img.shields.io/badge/built%20with-asyncssh-green) ![No client install](https://img.shields.io/badge/client-just%20ssh-orange)

![Gameplay](docs/gameplay.png)

</div>

## Join a game

```bash
ssh -p 1111 <server-ip>
```

No client to install. Your SSH username becomes your tank name. Your terminal must be at least **60x24**.

## How a round works

| Step | What happens |
|------|--------------|
| 1. Map editor | The first player in is the **admin** and builds the map. |
| 2. Waiting room | Everyone else sees the player list and "admin is editing the map". |
| 3. Countdown | Admin hits **Enter**, then a full-screen 3-2-1 ASCII countdown runs. |
| 4. Battle | Everyone gets **3 lives**. Get hit, lose a life, respawn. Lose your last life and you're out. |
| 5. Winner | The last player with lives wins, then everyone returns to the editor. |

- If the admin disconnects, the longest-connected player becomes admin.
- Players who join mid-round spectate until the next round.
- Walls persist between rounds and reset when the server empties.
- In a solo round there is no winner. The admin presses **Enter** to return to the editor.

| Map editor | Waiting room |
|:---:|:---:|
| ![Editor](docs/editor.png) | ![Waiting](docs/waiting-room.png) |
| **Countdown** | **Winner screen** |
| ![Countdown](docs/countdown.png) | ![Winner](docs/winner.png) |

## Controls

| Key | Battle | Map editor (admin) |
|-----|--------|--------------------|
| Arrows / WASD | Move tank | Move `#` cursor |
| Space | Shoot | Place / remove wall |
| Enter | Back to editor (solo round, admin only) | Start the game |
| Q | Quit | Quit |

## Power-ups

| Key | Power-up |
|-----|----------|
| `T` | Triple Shot |
| `R` | Rapid Fire |
| `P` | Piercing Round |
| `M` | Twin Tank (absorbs one hit) |
| `S` | Speed Boost |

## Host your own server

```bash
pip install asyncssh
python3 tanks.py --port 1111 --public-host YOUR.PUBLIC.IP
```

| Option | Purpose |
|--------|---------|
| `--port` | Listen port (default 1111) |
| `--public-host` | Address shown to players (repeatable) |
| `--max-players` / `--max-per-ip` | Connection limits |
| `--host-key` | Host key path (auto-generated on first run) |
| `TANKS_PASSWORD` env var | Require a password to join |

> `ssh_host_key` is a private key. Don't commit your own, and regenerate it for any public server.

## Under the hood

- `Game`: pure, deterministic engine with phases EDIT, COUNTDOWN, PLAY and WIN.
- `KeyParser`: terminal input parser that handles split escape sequences.
- `Session`: per-player input loop with diff-based rendering and backpressure.
- `TankServer`: SSH front end with connection limits and a fixed-timestep game loop.
