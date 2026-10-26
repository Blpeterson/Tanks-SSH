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

No client to install. Your SSH username becomes your tank name. Your terminal must be at least **60x24** (even on the smaller maps, so the lobby and countdown screens fit).

Playing on a phone or tablet? Use an SSH app with mouse/touch reporting and just **swipe to move** and **tap to shoot**. See [Touch controls](#touch-controls).

## How a round works

| Step | What happens |
|------|--------------|
| 1. Pick a map size | The first player in is the **admin** and chooses **Small**, **Medium** or **Large**. |
| 2. Map editor | The admin places walls on the chosen map. |
| 3. Waiting room | Everyone else sees the player list and "admin is choosing the map size" or "editing the map". |
| 4. Countdown | Admin hits **Enter**, then a full-screen 3-2-1 ASCII countdown runs. |
| 5. Battle | Everyone gets **3 lives**. Get hit, lose a life, respawn. Lose your last life and you're out. |
| 6. Winner | The last player with lives wins, then everyone returns to the size picker. |

### Map sizes

| Size | Arena | Good for |
|------|-------|----------|
| Small | 30x12 | 2-4 players, fast and chaotic |
| Medium | 44x16 | 4-8 players |
| Large | 60x20 | Big groups (the default) |

- If the admin disconnects, the longest-connected player becomes admin.
- Players who join mid-round spectate until the next round.
- Walls persist between rounds if the admin re-confirms the same size. Picking a different size clears them, and they also reset when the server empties.
- When the server empties, the next game starts fresh on Large.
- In a solo round there is no winner. The admin presses **Enter** to return to the size picker.

| Map size picker | Map editor |
|:---:|:---:|
| ![Size picker](docs/size-picker.png) | ![Editor](docs/editor.png) |
| **Waiting room** | **Countdown** |
| ![Waiting](docs/waiting-room.png) | ![Countdown](docs/countdown.png) |
| **Winner screen** | |
| ![Winner](docs/winner.png) | |

## Controls

| Input | Battle | Size picker (admin) | Map editor (admin) |
|-------|--------|---------------------|--------------------|
| Arrows / WASD | Move tank | Change size | Move `#` cursor |
| Space | Shoot | Confirm size | Place / remove wall |
| Enter | Back to size picker (solo round, admin only) | Confirm size | Start the game |
| Q | Quit | Quit | Quit |

### Touch controls

If your terminal supports mouse/touch reporting (Termux, Blink Shell and similar apps generally do), you can play without a keyboard:

| Gesture | Action |
|---------|--------|
| Swipe left / right / up / down | Move the tank (or the cursor / size selection for the admin) |
| Hold and keep dragging | Keep moving in that direction |
| Tap | Shoot (or confirm size / toggle a wall for the admin) |

The game turns mouse reporting on when you join and off when you leave. Some terminals use touch-drag for text selection and may swallow the gestures. Keyboard controls always keep working. Swipe sensitivity can be tuned with `SWIPE_X` and `SWIPE_Y` in `KeyParser`.

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

- `Game`: pure, deterministic engine with phases SIZE, EDIT, COUNTDOWN, PLAY and WIN.
- `KeyParser`: terminal input parser that handles split escape sequences, plus touch taps and swipes.
- `Session`: per-player input loop with diff-based rendering and backpressure.
- `TankServer`: SSH front end with connection limits and a fixed-timestep game loop.