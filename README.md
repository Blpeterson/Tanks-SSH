<div align="center">

```
 _____  _    _   _ _  __ ____       ____ ____  _   _
|_   _|/ \  | \ | | |/ // ___|     / ___/ ___|| | | |
  | | / _ \ |  \| | ' / \___ \ ____\___ \___ \| |_| |
  | |/ ___ \| |\  | . \  ___) |_____|__) |__) |  _  |
  |_/_/   \_\_| \_|_|\_\|____/     |____/____/|_| |_|
```

**Last-tank-standing multiplayer ASCII battles, played over plain SSH.**
Top-down or a rotating **2.5D isometric** arena, with **lob shots** that arc over walls.

![Python](https://img.shields.io/badge/python-3.8%2B-blue) ![asyncssh](https://img.shields.io/badge/built%20with-asyncssh-green) ![No client install](https://img.shields.io/badge/client-just%20ssh-orange)

![Gameplay](docs/gameplay.png)

</div>

## Join a game
```bash
ssh -p 1111 <server-ip>
```
No client to install. Your SSH username becomes your tank name. Your terminal must be at least **60 x 24** (the 2.5D view also needs a **256-color** terminal). **Esc** quits.

## How a round works

| Step | What happens |
|------|--------------|
| 1. Map size | The first player in is the **admin** and picks Small, Medium or Large. |
| 2. View mode | The admin picks **Top-down** or **2.5D isometric**. |
| 3. Map editor | The admin places walls. In 2.5D, walls come in three heights. |
| 4. Waiting room | Everyone else sees the player list and what the admin is doing right now. |
| 5. Countdown | Admin hits **Enter** for a full-screen 3-2-1 ASCII countdown. |
| 6. Battle | Everyone gets **3 lives**. A hit costs a life and you respawn. Lose your last life and you are out. |
| 7. Winner | The last player with lives wins, then everyone returns to the setup screens. |

If the admin disconnects, the longest-connected player becomes admin.

| Size picker | View picker |
|:---:|:---:|
| ![Size](docs/size-picker.png) | ![View](docs/view-picker.png) |
| **Map editor** | **Waiting room** |
| ![Editor](docs/editor.png) | ![Waiting](docs/waiting-room.png) |
| **2.5D battle** | **Lob shot in flight** |
| ![Iso](docs/2-5d-play.png) | ![Lob](docs/lob-shot.png) |

## Map sizes

| Size | Arena |
|------|-------|
| Small | 30 x 12 |
| Medium | 44 x 16 |
| Large (default) | 60 x 20 |

Walls are kept between rounds if the admin re-confirms the same size. Picking a different size clears them. The terminal minimum stays 60 x 24 on every size so lobby and countdown screens always fit.

## View modes

### Top-down
The classic overhead arena. All walls are `#` and block both tanks and bullets.

### 2.5D isometric
The camera looks diagonally down from one top corner of the arena toward the opposite bottom corner and follows your tank. Walls are drawn as lit blocks. Your own tank is always drawn on top, even behind a tall wall.

| Wall | Height | Tanks | Straight bullets | Lob shots |
|------|--------|-------|------------------|-----------|
| `#` | 1 | blocked | pass over (configurable) | clear it |
| `$` | 2 | blocked | blocked | clear it |
| `@` | 3 | blocked | blocked | only a long, fully charged lob |

#### Rotating the camera
Press **E** to swing your camera to the next corner, **clockwise** (NW, NE, SE, SW, then back to NW). Each player has their own camera, and rotating never changes the game. Arrow keys always follow the screen diagonals, whichever corner you are looking from:

| Key | Moves |
|-----|-------|
| Up / W | up-right |
| Right / D | down-right |
| Down / S | down-left |
| Left / A | up-left |

#### Lob shots
Press **Q** to switch Space between **STRAIGHT** and **LOB** (shown in the HUD).

- **Tap Space:** a short lob (about 4 tiles) that clears 1- and 2-high walls.
- **Hold Space:** the lob flies farther. A power bar in the HUD fills while you hold. Release to fire.
- **Full hold (about 1.2 s):** the highest arc. It can clear a 3-high wall if you are standing the right distance back, so point-blank is too steep and too low to clear it.
- The shell explodes where it lands with a **3x3 blast**. Shields absorb it, a Twin Tank clone absorbs one hit, and you are never hurt by your own blast.
- Lobs have a short cooldown, and are 2.5D only. In top-down, Q and E do nothing.

> Terminals do not report key releases, so "holding" Space relies on your keyboard's key-repeat. If your repeat is very slow or off, tapping fires the short lob only.

## 2.5D map editor
The editor is always top-down, with a different character for each height.

| Key | Action |
|-----|--------|
| Arrows / WASD | Move the cursor |
| `1` `2` `3` | Choose brush height (`#` `$` `@`) |
| Space | Place a wall (same height again removes it) |
| `X` | Erase the wall under the cursor |
| `C` `C` | Clear everything (press twice within 3 s) |
| `V` | Toggle a live isometric preview |
| `E` | Rotate the preview camera |
| Enter | Start the game |

## Controls

| Key | Battle | Setup (admin) |
|-----|--------|---------------|
| Arrows / WASD | Move your tank | Move the cursor / change the selection |
| Space | Shoot (hold to power a lob) | Place or remove a wall |
| `Q` | Toggle straight/lob (2.5D) | none |
| `E` | Rotate camera (2.5D) | Rotate preview |
| Enter | Back to editor (solo round, admin) | Confirm / start |
| **Esc** | Quit | Quit |

### Touch screens
If your terminal reports touch as mouse events: **swipe** to move (hold the drag to keep moving), **tap** to shoot or confirm. Touch cannot rotate the camera or toggle lobs, so use the keyboard for those. Some apps use touch-drag for text selection and may swallow the events.

## Power-ups
`T` Triple Shot, `R` Rapid Fire, `P` Piercing Round, `M` Twin Tank, `S` Speed Boost. They show as `<T>` etc. in 2.5D.

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
| `--host-key` | Host key path (auto-generated) |
| `TANKS_PASSWORD` env var | Require a password |

> `ssh_host_key` is a private key. Do not commit your own; regenerate it for any public server.

### Tuning (`Config` in `tanks.py`)
`low_walls_pass` (can bullets cross 1-high walls), `lob_cooldown`, `lob_max_charge`, `lob_min_range` / `lob_max_range`, `lob_apex_min` / `lob_apex_max` (arc heights), `lob_repeat_gap` / `lob_first_gap` (key-repeat detection), plus `start_lives`, `respawn_delay` and the usual timing values.

## Under the hood
`Game` (pure, deterministic engine with an injectable clock and RNG) / `KeyParser` (keys, Esc, touch) / `Session` (diff-based rendering, backpressure) / `TankServer` (SSH front end and fixed-timestep loop). The isometric renderer is a painter's-algorithm canvas that rotates the world into the camera frame, so a single code path serves all four corners.

## Screenshot checklist
Save into `docs/`: `gameplay.png`, `size-picker.png`, `view-picker.png`, `editor.png`, `waiting-room.png`, `2-5d-play.png`, `lob-shot.png`.