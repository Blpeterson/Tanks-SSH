<div align="center">

```
 _____  _    _   _ _  __ ____       ____ ____  _   _
|_   _|/ \  | \ | | |/ // ___|     / ___/ ___|| | | |
  | | / _ \ |  \| | ' / \___ \ ____\___ \___ \| |_| |
  | |/ ___ \| |\  | . \  ___) |_____|__) |__) |  _  |
  |_/_/   \_\_| \_|_|\_\|____/     |____/____/|_| |_|
```

**Last-tank-standing multiplayer ASCII battles, played over plain SSH.**
Classic top-down arena, or a 2.5D isometric mode with walls of different heights.

![Python](https://img.shields.io/badge/python-3.8%2B-blue) ![asyncssh](https://img.shields.io/badge/built%20with-asyncssh-green) ![No client install](https://img.shields.io/badge/client-just%20ssh-orange)

![Gameplay](docs/gameplay.png)

</div>

## Join a game

```bash
ssh -p 1111 <server-ip>
```

No client to install. Your SSH username becomes your tank name. Your terminal must be at least **60x24** (even on the smaller maps, so the lobby and countdown screens fit). The 2.5D mode uses 256 colours, so use a modern terminal.

Playing on a phone or tablet? Use an SSH app with mouse/touch reporting and just **swipe to move** and **tap to shoot**. See [Touch controls](#touch-controls).

## How a round works

| Step | What happens |
|------|--------------|
| 1. Pick a map size | The first player in is the **admin** and chooses **Small**, **Medium** or **Large**. |
| 2. Pick a view mode | The admin chooses **Top-down** or **2.5D isometric**. |
| 3. Map editor | The admin places walls. In 2.5D mode there are three wall heights. |
| 4. Waiting room | Everyone else sees the player list and what the admin is doing ("choosing the map size", "choosing the view mode" or "editing the map"). |
| 5. Countdown | Admin hits **Enter**, then a full-screen 3-2-1 ASCII countdown runs. |
| 6. Battle | Everyone gets **3 lives**. Get hit, lose a life, respawn. Lose your last life and you're out. |
| 7. Winner | The last player with lives wins, then everyone returns to the size picker. |

- If the admin disconnects, the longest-connected player becomes admin.
- Players who join mid-round spectate until the next round.
- In a solo round there is no winner. The admin presses **Enter** to return to the size picker.
- Walls persist between rounds if the admin re-confirms the same size. Picking a different size clears them, and they also reset when the server empties. When the server empties, the next game starts fresh on Large, top-down.

| Size picker | View picker |
|:---:|:---:|
| ![Size picker](docs/size-picker.png) | ![View picker](docs/view-picker.png) |
| **Waiting room** | **Countdown** |
| ![Waiting](docs/waiting-room.png) | ![Countdown](docs/countdown.png) |
| **Winner screen** | |
| ![Winner](docs/winner.png) | |

### Map sizes

| Size | Arena | Good for |
|------|-------|----------|
| Small | 30x12 | 2-4 players, fast and chaotic |
| Medium | 44x16 | 4-8 players |
| Large | 60x20 | Big groups (the default) |

## View modes

### Top-down (classic)

The original overhead arena. The editor has one wall type, `#`, and what you edit is exactly what you see in battle.

![Top-down map editor](docs/editor.png)

### 2.5D isometric

A diagonal "cube corner" camera: you look from one top corner of the arena down toward the opposite bottom corner, so the world axes run along the screen diagonals and walls are drawn as 3D blocks with a lit left face, a shaded right face and a top.

![2.5D gameplay](docs/2-5d-play.png)

- **Camera follows your tank**, so you always see the area around you. Spectators follow the first living tank.
- **Your own tank is x-rayed**: it is always drawn on top, even when it is behind a tall wall.
- **Tank sprite**: a 4x2 hull with a turret `()` and a barrel that points along its facing (`/` or `\` in the corner it faces). Your tank is bright green, others are white, a shielded tank glows cyan, and a Twin Tank clone is dimmed.
- **Bullets** show as `*` (cyan for yours, red for others). **Power-ups** appear as `<T>`, `<R>` and so on.
- **Arrow keys follow the screen diagonals**, as they would in any isometric game:

  | Key | Tank moves |
  |-----|------------|
  | Up | up-right on screen |
  | Right | down-right |
  | Down | down-left |
  | Left | up-left |

  WASD and swipes work the same way.

#### Wall heights

| Char | Height | Tanks | Bullets |
|------|--------|-------|---------|
| `#` | 1 (low) | blocked | **pass over** |
| `$` | 2 | blocked | blocked |
| `@` | 3 (tall) | blocked | blocked |

Low walls are cover you can shoot across. Tall walls block everything. (Set `low_walls_pass=False` in `Config` to make every wall block bullets.) The arena border is always height 1.

#### 2.5D map editor

The editor is still drawn **top-down**, with the brush character showing the height it will place. Press **V** at any time to flip to a live isometric preview of your map, where the arrows follow the screen diagonals.

![2.5D editor preview](docs/2-5d-editor.png)

| Key | Action |
|-----|--------|
| Arrows / WASD / swipe | Move the cursor |
| `1` `2` `3` | Choose brush height (`#`, `$`, `@`) |
| Space / tap | Place a wall of the current height. Placing the same height again removes it, and placing a different height replaces it. |
| `X` | Erase the wall under the cursor |
| `C` then `C` again within 3 seconds | Clear every wall |
| `V` | Toggle the isometric preview |
| Enter | Start the game |

## Controls

| Input | Battle | Pickers (admin) | Map editor (admin) |
|-------|--------|-----------------|--------------------|
| Arrows / WASD | Move tank | Change choice | Move cursor |
| Space | Shoot | Confirm | Place / remove wall |
| Enter | Back to size picker (solo round, admin only) | Confirm | Start the game |
| `1` `2` `3`, `X`, `C`, `V` | - | - | 2.5D editor tools (see above) |
| Q | Quit | Quit | Quit |

### Touch controls

If your terminal supports mouse/touch reporting (Termux, Blink Shell and similar apps generally do), you can play without a keyboard:

| Gesture | Action |
|---------|--------|
| Swipe left / right / up / down | Move the tank (or the cursor / selection for the admin) |
| Hold and keep dragging | Keep moving in that direction |
| Tap | Shoot (or confirm a choice / place a wall for the admin) |

The game turns mouse reporting on when you join and off when you leave. Some terminals use touch-drag for text selection and may swallow the gestures. Keyboard controls always keep working. Swipe sensitivity can be tuned with `SWIPE_X` and `SWIPE_Y` in `KeyParser`. In 2.5D mode swipes follow the same screen diagonals as the arrow keys.

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

- `Game`: pure, deterministic engine with phases SIZE, VIEW, EDIT, COUNTDOWN, PLAY and WIN. Walls are stored as `{(x, y): height}`.
- `Canvas`: small styled character buffer used by the isometric renderer, which draws tiles far-to-near (painter's algorithm).
- `KeyParser`: terminal input parser that handles split escape sequences, plus touch taps and swipes.
- `Session`: per-player input loop with diff-based rendering and backpressure.
- `TankServer`: SSH front end with connection limits and a fixed-timestep game loop.

## Screenshots to capture

Save these in `docs/`: `gameplay.png`, `size-picker.png`, `view-picker.png`, `editor.png`, `waiting-room.png`, `countdown.png`, `winner.png`, `2-5d-play.png` and `2-5d-editor.png`.