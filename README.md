# letterbox

Guild Wars 2 in a letterbox on Linux with KDE Plasma. The game renders only a wide strip of your monitor
and the free space above it becomes a stats bar with FPS, ping, CPU and GPU sensors, the event timer,
a clock and more.

I won't bore you with why I built this. If you're curious, there's a [section at the end](#why-i-built-this).

![The bar above the game, 3840×360 on a 4K monitor](docs/bar.png)

## What it does

- GW2 runs windowed without borders at full width but less height, for example 3840×1800 on a 4K
  monitor. That's 17 % fewer pixels for the GPU, and the game looks the same, just wider.
- The strip that's left shows a bar you can put together yourself. Pick tiles, stack them, put them
  side by side, make some wider than others.
- Everything is set up in a settings app with a live preview of the real bar.
- When GW2 closes, your monitor and panels go back to normal.

### Tiles

| Tile | Shows |
|---|---|
| FPS | current FPS, optionally with the 60 s average, plus the 1 % low |
| Ping | round trip time to the GW2 server and the 60 s average |
| CPU, CPU package, CPU temp | clock, power and temperature |
| GW2 main thread | load of the game's busiest thread, the usual culprit when the CPU looks idle but FPS drop |
| GPU power, clock, busy, temp, VRAM | AMD GPUs |
| Map, Combat, Character | where you are, how long you've been fighting, who you're playing |
| Bottleneck | a guess at what holds the game back right now, GPU or CPU |
| Event timer | world bosses and metas as a timeline like the one on the wiki |
| Fans, Memory, Network | the rest |

Every number tile can show the number, a graph of the last 30 s to 5 min, or both, in a colour of your
choice.

### Event timer and clock

- A timeline of world bosses, map metas, festivals and the Tyria day and night cycle, grouped by expansion
  and Living World season. Pick whole expansions or single maps, set how far ahead it looks, how tall
  each row is and how much space sits between them.
- The meta of the map you're on can join the timeline on its own.
- Under the clock you get the Tyria time of day, the next event and the time to daily and weekly reset.
- A start warning a few minutes before a boss, in a colour you choose, with blinking and a sound if you
  want them.
- The clock can be digital, analog or minimal.

### More

- Four themes, your own colours for every tile and text, and your own fonts.
- An ambient background that picks up the colours at the edge of the game image.
- In combat the bar can dim itself or show only warnings like a hot GPU or a ping spike.
- Profiles, so you can keep one layout for WvW and one for open world.
- Hotkeys that work in game. **Meta+Shift+B** cycles the bar between full, compact, clock only and off.
  **Meta+Shift+M** drops a lag marker (a timestamp in `~/.local/share/letterbox/markers.log`) so you can
  find that one laggy moment in your logs later.

## Requirements

- KDE Plasma 6 on Wayland. The placement is done by a KWin script, so other desktops won't work.
- GW2 through Steam with Proton. Other installs work too if you tell letterbox where GW2 keeps its graphics
  settings (see below).
- Python 3 with GTK 4, PyGObject, pycairo and NumPy. On Arch that's
  `sudo pacman -S python-gobject gtk4 python-cairo python-numpy qt6-tools kscreen iproute2`
- [MangoHud](https://github.com/flightlessmango/MangoHud) for the FPS tiles.
- An AMD GPU for the GPU tiles. Everything else works with any GPU.

## Install

```sh
git clone https://github.com/flo-coast/letterbox.git && cd letterbox && ./install.sh
```

Run it as your normal user. It installs to `~/.local/share/letterbox`, adds the `letterbox` command and
the app "Letterbox settings", and asks for sudo once for the GW2 logger service. That service reads GW2's
MumbleLink (the official interface for overlays) for the map, combat, character and thread tiles. Under
Proton that memory lives inside the game process, which is why it needs root.

Then

1. Put `letterbox run %command%` in the Steam launch options of Guild Wars 2. If you already have options,
   put it in front, for example `letterbox run mangohud %command%`.
2. Close GW2 and switch letterbox mode on in the settings app, or run `letterbox on`.
3. Start the game.

Letterbox mode keeps its own copy of GW2's graphics settings. `letterbox off` brings back the fullscreen
ones, so switching back and forth doesn't mess up either.

### FPS tiles

The installer looks for a MangoHud config of GW2 and reads the logs from its `output_folder`. If you don't
have one yet, put this in `~/.config/MangoHud/wine-Gw2-64.conf`

```ini
output_folder=/home/YOU/.local/share/letterbox/mangohud
autostart_log=5
log_interval=0
```

### Settings that aren't in the app

In `~/.config/letterbox/config.json`

| Key | Default | For |
|---|---|---|
| `output` | primary monitor | the monitor GW2 goes on, e.g. `"DP-1"` |
| `gpu` | first discrete AMD GPU | PCI address of the GPU to show, e.g. `"0000:03:00.0"` |
| `gfx_settings` | found in your Steam libraries | path to `GFXSettings.Gw2-64.exe.xml` for non-Steam installs |
| `mangohud_dir` | `~/.local/share/letterbox/mangohud` | where MangoHud writes GW2's logs |

The logger runs as root and doesn't see your config. If it picks the wrong GPU, set `LETTERBOX_GPU` in
`/etc/systemd/system/letterbox-gw2log.service`.

## Commands

```
letterbox on [height]   letterbox mode on (GW2 closed)
letterbox off           back to normal fullscreen
letterbox status
letterbox run <cmd>     for the Steam launch options
letterbox start|stop    what run does around the game, if you want to hook it up elsewhere (gamemode)
```

## Uninstall

`./uninstall.sh` removes everything but your settings. `./uninstall.sh --purge` takes those too.

## Good to know

- Ping comes from the kernel's numbers for the game's connection. Nothing extra goes over the network.
- The logger writes one CSV per day to `/var/log/letterbox` and keeps 14 days. Those files include your
  character name and position. They stay on your machine.
- The event times come from the GW2 wiki's event timer via
  [giovazz89/gw2-api-event-timers](https://github.com/giovazz89/gw2-api-event-timers). The installer
  downloads them, and "Update event data" in the settings app fetches them again.
- On T2 MacBooks with my t2pm probe running, two more tiles show up for the Apple SMC power budget. On
  any other machine they simply aren't there.

## Why I built this

My MacBook Pro 16" (2019) has an AMD Radeon and runs Arch. On an external 4K monitor GW2 pushes that GPU
to its limit, and in big fights it drops frames and stutters. Rendering fewer pixels takes some of that
load off. On a 16:9 monitor the game doesn't really need all of its height anyway, so I gave it a wider
picture and used the strip that's left for the things I kept checking while playing, which I didn't
want to cover the game with an overlay.

Power management on that Mac is its own story, see [t2-cruise](https://github.com/flo-coast/t2-cruise).

## Credits

Written by **flo-coast** (2026), with Claude (Anthropic) as a coding assistant.
Event timer data from the GW2 wiki via [giovazz89/gw2-api-event-timers](https://github.com/giovazz89/gw2-api-event-timers).
Not affiliated with ArenaNet or NCSOFT. Guild Wars 2 is a trademark of ArenaNet, LLC.

## License

[GPL v3 or later](LICENSE). No warranty. letterbox changes GW2's graphics settings file (it keeps
copies next to it) and the hiding mode of your Plasma panels while the game runs.
