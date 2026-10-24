# lmscli

A terminal (TUI) controller for [Lyrion Music Server](https://lyrion.org/) (LMS, formerly Logitech Media Server),
built with [Textual](https://textual.textualize.io/). It talks to LMS over its JSON-RPC interface (port 9000),
so it needs no plugins or login.

Features: browse Artists → Albums → Tracks, search, view and clear the queue, switch between players,
group/ungroup players, volume/seek/shuffle/repeat, and album art (kitty/sixel graphics protocol).

## Install

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./lmscli                      # launcher script; or: .venv/bin/python lmscli.py
```

Options: `-s host:port`, `-p "Player name"`, `--no-art`.

## Configuration

`~/.config/lmscli/config.toml` is created on first run:

```toml
server = "192.168.32.5:9000"   # your LMS host:port
player = ""                    # player name or MAC; empty = first one that is playing
art = true
art_size = 300
```

## Keys

| Key | Action |
| --- | --- |
| ↑ ↓ | Move |
| → / Enter | Open (Artists → Albums → Tracks); play at the track level |
| ← / Backspace / Esc | Back up a level (or previous tab at the top) |
| 1 2 3 4 | Library, Queue, Search, Players tabs |
| `/` | Search |
| `R` | Replace queue and play the selected item |
| `a` / `A` | Add to queue / play next |
| Space | Play/pause (on the Players tab: group/ungroup with the current player) |
| `n` / `p` | Next / previous track |
| `+` `-` | Volume up / down (numpad too) |
| `,` `.` | Seek -10s / +10s |
| `s` / `r` | Cycle shuffle / repeat |
| `c` (twice) | Clear the queue (Queue tab) |
| `P` | Cycle player |
| `q` | Quit |

Playing or adding always turns shuffle off, so albums play in order.

## Licence

MIT. See [LICENSE](LICENSE).
