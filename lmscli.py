#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""lmscli - terminal controller for Lyrion Music Server (LMS)."""
import argparse
import asyncio
import re
import io
import time
import tomllib
from pathlib import Path

import httpx
from PIL import Image as PILImage
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Tabs,  DataTable, Footer, Header, Input, ProgressBar, Static, TabbedContent, TabPane
from textual_image._terminal import get_cell_size
from textual_image.widget import Image as ArtWidget

CONFIG_PATH = Path.home() / ".config" / "lmscli" / "config.toml"
DEFAULT_CONFIG = """\
# lmscli configuration
server = "192.168.32.5:9000"
# Player name or MAC; empty = first player found (prefers one that is playing)
player = ""
# Show album art (uses the kitty/sixel graphics protocol when available)
art = true
art_size = 300
"""


def load_config() -> dict:
    cfg = {"server": "192.168.32.5:9000", "player": "", "art": True, "art_size": 300}
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(DEFAULT_CONFIG)
    cfg.update(tomllib.loads(CONFIG_PATH.read_text()))
    return cfg


def fmt_time(secs) -> str:
    secs = int(secs or 0)
    return f"{secs // 60}:{secs % 60:02d}"


class LMS:
    def __init__(self, server: str):
        self.base = f"http://{server}"
        self.http = httpx.AsyncClient(timeout=10)

    async def rpc(self, player: str, *cmd):
        r = await self.http.post(
            f"{self.base}/jsonrpc.js",
            json={"id": 1, "method": "slim.request", "params": [player or "-", list(cmd)]},
        )
        r.raise_for_status()
        return r.json().get("result", {})

    async def art(self, coverid: str, size: int) -> PILImage.Image | None:
        try:
            r = await self.http.get(f"{self.base}/music/{coverid}/cover_{size}x{size}_o.jpg")
            r.raise_for_status()
            return PILImage.open(io.BytesIO(r.content)).convert("RGB")
        except Exception:
            return None


class Table(DataTable):
    """DataTable whose left/right go to the app (drill/back/tab) instead of scrolling."""

    BINDINGS = [
        Binding("left", "app.left", show=False),
        Binding("right", "app.right", show=False),
    ]


class SearchTable(Table):
    """Results table: pressing up on the first row goes back to the search box."""

    def action_cursor_up(self) -> None:
        if self.cursor_row == 0:
            self.app.query_one("#search-in", Input).focus()
        else:
            super().action_cursor_up()


class LMSCli(App):
    TITLE = "lmscli"
    CSS = """
    #main { height: 1fr; }
    #browser { width: 2fr; }
    #side { width: 1fr; min-width: 34; max-width: 50; padding: 0 1; }
    #art { margin-bottom: 1; }
    #players-help { color: $text-muted; padding: 0 1; }
    #clear-q { margin: 0 1; min-width: 18; }
    #crumb { color: $text-muted; padding: 0 1; }
    #np-title { text-style: bold; }
    #np-sub { color: $text-muted; }
    #np-state { color: $text-muted; margin-top: 1; }
    #search { margin: 0 0 1 0; }
    DataTable { height: 1fr; }
    ProgressBar { padding: 0 1; }
    Bar > .bar--bar { color: $accent; }
    """
    BINDINGS = [
        Binding("space", "pause", "Play/Pause"),
        Binding("n", "rpc('playlist index +1')", "Next"),
        Binding("p", "rpc('playlist index -1')", "Prev"),
        Binding("plus,shift+plus,shift+equals_sign,equals_sign,add", "rpc('mixer volume +5')", "Vol+"),
        Binding("minus,subtract", "rpc('mixer volume -5')", "Vol-"),
        Binding("comma", "rpc('time -10')", "-10s", show=False),
        Binding("full_stop", "rpc('time +10')", "+10s", show=False),
        Binding("s", "shuffle", "Shuffle"),
        Binding("r", "repeat", "Repeat"),
        Binding("P", "next_player", "Player"),
        Binding("slash", "search", "Search"),
        Binding("1", "tab('library')", "Library", show=False),
        Binding("2", "tab('queue')", "Queue", show=False),
        Binding("3", "tab('search')", "Search", show=False),
        Binding("4", "tab('players')", "Players", show=False),
        Binding("left", "left", "Back/Tab←", show=False),
        Binding("right", "right", "Open/Tab→", show=False),
        Binding("backspace,escape", "up", "Up", show=False),
        Binding("down", "input_down", "Results", show=False),
        Binding("a", "enqueue('add')", "Add"),
        Binding("R", "enqueue('load')", "Replace+play"),
        Binding("A", "enqueue('insert')", "Play next"),
        Binding("c", "clear_queue", "Clear queue"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.lms = LMS(cfg["server"])
        self.players: list[dict] = []
        self.player_idx = 0
        self.status: dict = {}
        self.status_at = 0.0
        self.queue_stamp = None
        self.cover = None
        self.lib = {"level": "artists", "artist": None, "album": None}
        self.artists_cache: list[dict] = []
        self.lib_cursor: dict[str, str] = {}

    # ---- layout ----
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="browser"):
                with TabbedContent(id="tabs", initial="library"):
                    with TabPane("1 Library", id="library"):
                        yield Static("Artists", id="crumb")
                        yield Table(id="lib-t", cursor_type="row", zebra_stripes=True)
                    with TabPane("2 Queue", id="queue"):
                        yield Button("Clear all  (c)", id="clear-q", variant="error")
                        yield Table(id="queue-t", cursor_type="row", zebra_stripes=True)
                    with TabPane("3 Search", id="search"):
                        yield Input(placeholder="Search albums, artists, tracks…   ↓ results   Esc leave", id="search-in")
                        yield SearchTable(id="search-t", cursor_type="row", zebra_stripes=True)
                    with TabPane("4 Players", id="players"):
                        yield Static("Enter: control player (and make it the default)   Space: group / ungroup with the current player", id="players-help")
                        yield Table(id="players-t", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side"):
                if self.cfg["art"]:
                    yield ArtWidget(id="art")
                yield Static("Nothing playing", id="np-title")
                yield Static("", id="np-sub")
                yield ProgressBar(total=100, show_eta=False, show_percentage=False, id="progress")
                yield Static("", id="np-state")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#search-t", DataTable).add_columns("Type", "Name", "Detail")
        self.query_one("#players-t", DataTable).add_columns("", "Player", "State", "Group")
        self.query_one("#queue-t", DataTable).add_columns("#", "Title", "Artist", "Album", "Time")
        for tabs in self.query(Tabs):
            tabs.can_focus = False  # the tab bar would otherwise swallow arrows/enter/space
        self.query_one("#clear-q", Button).can_focus = False  # keep focus on the lists
        self.query_one("#lib-t").focus()
        try:
            await self.pick_player()
        except Exception as e:
            self.notify(f"Cannot reach LMS at {self.cfg['server']}: {e}", severity="error", timeout=10)
            return
        self.run_worker(self.show_artists(), exclusive=True, group="lib")
        self.set_interval(1.0, self.poll)
        self.set_interval(0.25, self.tick)
        await self.poll()

    # ---- players ----
    @property
    def pid(self) -> str:
        return self.players[self.player_idx]["playerid"] if self.players else ""

    async def pick_player(self) -> None:
        res = await self.lms.rpc("-", "players", 0, 20)
        self.players = res.get("players_loop", [])
        want = self.cfg["player"].lower()
        for i, p in enumerate(self.players):
            if want and want in (p["name"].lower(), p["playerid"].lower()):
                self.player_idx = i
                break
        else:
            playing = [i for i, p in enumerate(self.players) if p.get("isplaying")]
            self.player_idx = playing[0] if playing else 0
        self.queue_stamp = None

    async def action_next_player(self) -> None:
        if self.players:
            nxt = self.players[(self.player_idx + 1) % len(self.players)]
            await self.switch_player(nxt["playerid"], save=False)

    async def switch_player(self, playerid: str, save: bool = True) -> None:
        for i, p in enumerate(self.players):
            if p["playerid"] == playerid:
                self.player_idx = i
                break
        else:
            return
        name = self.players[self.player_idx]["name"]
        self.queue_stamp = None
        self.cover = None
        self.status = {}
        self.notify(f"Controlling {name}" + (" (default)" if save else ""))
        if save:
            self.save_default_player(name)
        await self.poll()
        await self.refresh_players()

    def save_default_player(self, name: str) -> None:
        line = f'player = "{name}"'
        text = CONFIG_PATH.read_text() if CONFIG_PATH.exists() else ""
        if re.search(r"(?m)^player\s*=.*$", text):
            text = re.sub(r"(?m)^player\s*=.*$", lambda _: line, text)
        else:
            text += f"\n{line}\n"
        CONFIG_PATH.write_text(text)
        self.cfg["player"] = name

    # ---- players tab ----
    async def refresh_players(self) -> None:
        try:
            res, groups = await asyncio.gather(
                self.lms.rpc("-", "players", 0, 50),
                self.lms.rpc("-", "syncgroups", "?"),
            )
        except Exception:
            return
        cur = self.pid
        self.players = res.get("players_loop", self.players)
        self.player_idx = next((i for i, p in enumerate(self.players) if p["playerid"] == cur), 0)
        self.group_of: dict[str, list[str]] = {}
        for g in groups.get("syncgroups_loop", []):
            members = [m for m in g.get("sync_members", "").split(",") if m]
            names = g.get("sync_member_names", "").split(",")
            for m in members:
                self.group_of[m] = [n for mid, n in zip(members, names) if mid != m]
        t = self.query_one("#players-t", DataTable)
        keep = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value if t.row_count else None
        t.clear()
        active = self.players[self.player_idx]["playerid"] if self.players else ""
        for p in self.players:
            pid = p["playerid"]
            state = ("off" if not p.get("power") else "playing" if p.get("isplaying") else "idle")
            if not p.get("connected", 1):
                state = "disconnected"
            others = self.group_of.get(pid, [])
            group = ("✓ with " + ", ".join(others)) if others else ""
            t.add_row("▶" if pid == active else "", p["name"], state, group, key=pid)
        if keep and keep in t.rows:
            t.move_cursor(row=t.get_row_index(keep))

    async def toggle_group(self) -> None:
        t = self.query_one("#players-t", DataTable)
        if not t.row_count:
            return
        target = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        if target == self.pid:
            self.notify("Select another player to group with the current one")
            return
        name = next(p["name"] for p in self.players if p["playerid"] == target)
        if any(m == self.players[self.player_idx]["name"] for m in self.group_of.get(target, [])):
            await self.lms.rpc(target, "sync", "-")
            self.notify(f"{name} left the group")
        else:
            # LMS: "<leader> sync <joiner>" - the player the command is sent to keeps its queue and leads
            await self.lms.rpc(self.pid, "sync", target)
            self.notify(f"{name} joined {self.players[self.player_idx]['name']}")
        await self.refresh_players()

    # ---- library: artists > albums > tracks ----
    def lib_table(self) -> DataTable:
        return self.query_one("#lib-t", DataTable)

    def set_crumb(self) -> None:
        parts = ["Artists"]
        if self.lib["artist"]:
            parts.append(self.lib["artist"][1])
        if self.lib["album"]:
            parts.append(self.lib["album"][1])
        self.query_one("#crumb", Static).update(Text(" › ".join(parts)))

    def restore_cursor(self, level: str) -> None:
        t, key = self.lib_table(), self.lib_cursor.get(level)
        if key and key in t.rows:
            t.move_cursor(row=t.get_row_index(key))
        else:
            t.move_cursor(row=0)

    async def show_artists(self) -> None:
        if not self.artists_cache:
            res = await self.lms.rpc("-", "artists", 0, 5000)
            self.artists_cache = res.get("artists_loop", [])
        t = self.lib_table()
        t.clear(columns=True)
        t.add_columns("Artist")
        for a in self.artists_cache:
            t.add_row(a["artist"], key=f"artist:{a['id']}")
        self.lib.update(level="artists", artist=None, album=None)
        self.set_crumb()
        self.restore_cursor("artists")

    async def show_albums(self, artist: tuple[str, str]) -> None:
        res = await self.lms.rpc("-", "albums", 0, 500, "tags:ly", "sort:yearalbum", f"artist_id:{artist[0]}")
        t = self.lib_table()
        t.clear(columns=True)
        t.add_columns("Year", "Album")
        for a in res.get("albums_loop", []):
            t.add_row(str(a.get("year") or ""), a["album"], key=f"album:{a['id']}")
        self.lib.update(level="albums", artist=artist, album=None)
        self.set_crumb()
        self.restore_cursor("albums")

    async def show_tracks(self, album: tuple[str, str]) -> None:
        res = await self.lms.rpc("-", "titles", 0, 500, f"album_id:{album[0]}", "sort:tracknum", "tags:td")
        t = self.lib_table()
        t.clear(columns=True)
        t.add_columns("#", "Title", "Time")
        for i, s in enumerate(res.get("titles_loop", [])):
            t.add_row(str(s.get("tracknum") or i + 1), s["title"], fmt_time(s.get("duration")), key=f"track:{s['id']}")
        self.lib.update(level="tracks", album=album)
        self.set_crumb()
        t.move_cursor(row=0)

    def remember_cursor(self) -> None:
        t = self.lib_table()
        if t.row_count:
            self.lib_cursor[self.lib["level"]] = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value

    async def lib_open(self, key: str, name: str, row: int) -> None:
        """Drill into (or play, at the leaf level) the selected library row."""
        level = self.lib["level"]
        self.remember_cursor()
        if level == "artists":
            await self.show_albums((key.split(":")[1], name))
        elif level == "albums":
            await self.show_tracks((key.split(":")[1], name))
        else:  # play the album starting from this track
            await self.shuffle_off()
            await self.lms.rpc(self.pid, "playlistcontrol", "cmd:load", f"album_id:{self.lib['album'][0]}")
            await self.lms.rpc(self.pid, "playlist", "index", row)
            await self.poll()

    async def lib_up(self) -> bool:
        level = self.lib["level"]
        if level == "tracks":
            await self.show_albums(self.lib["artist"])
        elif level == "albums":
            await self.show_artists()
        else:
            return False
        return True

    def in_library(self) -> bool:
        return isinstance(self.focused, DataTable) and self.focused.id == "lib-t"

    def action_input_down(self) -> None:
        """Down from the search box moves into the results."""
        if isinstance(self.focused, Input):
            t = self.query_one("#search-t", DataTable)
            if t.row_count:
                t.focus()

    async def action_up(self) -> None:
        if isinstance(self.focused, Input):  # Esc in the search box
            t = self.query_one("#search-t", DataTable)
            if t.row_count:
                t.focus()
            else:
                self.action_tab("library")
            return
        if self.in_library():
            await self.lib_up()

    async def action_left(self) -> None:
        if self.in_library() and await self.lib_up():
            return
        self._cycle_tab(-1)

    async def action_right(self) -> None:
        f = self.focused
        if self.in_library() and f.row_count:
            await self.select_row(f)
        else:
            self._cycle_tab(1)

    async def select_row(self, t: DataTable) -> None:
        row = t.cursor_row
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value or ""
        cells = t.get_row_at(row)
        name = str(cells[-1] if self.lib["level"] == "albums" else cells[1] if self.lib["level"] == "tracks" else cells[0])
        await self.lib_open(key, name, row)

    @on(DataTable.RowSelected)
    async def row_selected(self, ev: DataTable.RowSelected) -> None:
        tid, key = ev.data_table.id, ev.row_key.value or ""
        if tid == "lib-t":
            await self.select_row(ev.data_table)
        elif tid == "players-t":
            await self.switch_player(key)
        elif tid == "queue-t":
            await self.lms.rpc(self.pid, "playlist", "index", key)
            await self.poll()
        elif key.startswith("artist:"):  # search result: open in the library
            await self.show_albums((key.split(":")[1], str(ev.data_table.get_row(ev.row_key)[1])))
            self.action_tab("library")
            self.lib_table().focus()
        elif key:
            await self.load_item(key, "load")

    async def shuffle_off(self) -> None:
        """Albums should play in order, so playing or adding always switches shuffle off."""
        if int(self.status.get("playlist shuffle", 0)):
            await self.lms.rpc(self.pid, "playlist", "shuffle", 0)
            self.status["playlist shuffle"] = 0

    async def load_item(self, key: str, cmd: str) -> None:
        await self.shuffle_off()
        kind, _, ident = key.partition(":")
        param = {"album": "album_id", "artist": "artist_id", "track": "track_id"}[kind]
        await self.lms.rpc(self.pid, "playlistcontrol", f"cmd:{cmd}", f"{param}:{ident}")
        verb = {"load": "Playing", "add": "Added", "insert": "Playing next"}[cmd]
        self.notify(verb)
        await self.poll()

    async def action_enqueue(self, cmd: str) -> None:
        f = self.focused
        if isinstance(f, DataTable) and f.id != "queue-t" and f.row_count:
            key = f.coordinate_to_cell_key(f.cursor_coordinate).row_key.value
            if f.id == "lib-t" and self.lib["level"] == "tracks" and cmd == "load":
                await self.select_row(f)  # replace queue with the album, starting at this track
            elif key:
                await self.load_item(key, cmd)

    # ---- search ----
    async def action_search(self) -> None:
        self.action_tab("search")
        self.query_one("#search-in", Input).focus()

    @on(Input.Changed, "#search-in")
    def search_changed(self, ev: Input.Changed) -> None:
        if getattr(self, "search_timer", None):
            self.search_timer.stop()
        self.search_timer = self.set_timer(0.4, lambda: self.run_worker(self.run_search(ev.value), exclusive=True, group="search"))

    @on(Input.Submitted, "#search-in")
    async def search_submitted(self, ev: Input.Submitted) -> None:
        await self.run_search(ev.value)
        self.action_input_down()

    async def run_search(self, term: str) -> None:
        term = term.strip()
        t = self.query_one("#search-t", DataTable)
        if len(term) < 2:
            t.clear()
            return
        albums, artists, tracks = await asyncio.gather(
            self.lms.rpc("-", "albums", 0, 50, "tags:aly", f"search:{term}"),
            self.lms.rpc("-", "artists", 0, 50, f"search:{term}"),
            self.lms.rpc("-", "titles", 0, 100, "tags:aly", f"search:{term}"),
        )
        t.clear()
        for a in artists.get("artists_loop", []):
            t.add_row("Artist", a["artist"], "", key=f"artist:{a['id']}")
        for a in albums.get("albums_loop", []):
            t.add_row("Album", a["album"], a.get("artist", ""), key=f"album:{a['id']}")
        for s in tracks.get("titles_loop", []):
            t.add_row("Track", s["title"], f"{s.get('artist', '')} – {s.get('album', '')}", key=f"track:{s['id']}")

    # ---- queue ----
    @on(Button.Pressed, "#clear-q")
    async def clear_pressed(self) -> None:
        await self.action_clear_queue()
        self.focus_pane("queue")

    async def action_clear_queue(self) -> None:
        """Clear the whole queue; the first press arms it, a second within 3s confirms."""
        if self.query_one("#tabs", TabbedContent).active != "queue":
            return
        now = time.monotonic()
        if now - getattr(self, "clear_armed", 0) > 3:
            self.clear_armed = now
            self.notify("Press c again (or click the button again) to clear the whole queue", severity="warning")
            return
        self.clear_armed = 0
        await self.lms.rpc(self.pid, "playlist", "clear")
        self.notify("Queue cleared")
        await self.poll()

    # ---- transport ----
    async def action_rpc(self, spec: str) -> None:
        # Textual only passes the first argument of a *args action, so bindings give one string
        await self.action_cmd(*spec.split())

    async def action_cmd(self, *cmd: str) -> None:
        await self.lms.rpc(self.pid, *cmd)
        await self.poll()

    async def action_pause(self) -> None:
        if isinstance(self.focused, DataTable) and self.focused.id == "players-t":
            await self.toggle_group()
            return
        await self.action_cmd("pause")

    async def action_shuffle(self) -> None:
        cur = int(self.status.get("playlist shuffle", 0))
        await self.action_cmd("playlist", "shuffle", (cur + 1) % 3)

    async def action_repeat(self) -> None:
        cur = int(self.status.get("playlist repeat", 0))
        await self.action_cmd("playlist", "repeat", (cur + 1) % 3)

    def action_tab(self, name: str) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        if tabs.active == name:
            self.focus_pane(name)
        else:
            # Drop focus first: a focused widget in the pane being hidden makes Textual
            # re-activate that pane. tab_activated() then focuses the new pane's list.
            self.set_focus(None)
            tabs.active = name

    def focus_pane(self, name: str) -> None:
        # Textual makes any pane that gains focus the active tab, so a stale request
        # (the user has already moved on) would flip the tab back. Drop it.
        tabs = self.query_one("#tabs", TabbedContent)
        if tabs.active != name:
            return
        pane = tabs.get_pane(name)
        target = pane.query_one(Input) if name == "search" else next(iter(pane.query(DataTable)), None)
        if target:
            target.focus()

    @on(TabbedContent.TabActivated)
    def tab_activated(self, ev: TabbedContent.TabActivated) -> None:
        self.call_after_refresh(self.focus_pane, ev.pane.id)

    def _cycle_tab(self, step: int) -> None:
        if isinstance(self.focused, Input):
            return
        tabs = self.query_one("#tabs", TabbedContent)
        ids = ["library", "queue", "search", "players"]
        self.action_tab(ids[(ids.index(tabs.active) + step) % len(ids)])

    # ---- status polling ----
    async def poll(self) -> None:
        if not self.players or getattr(self, "polling", False):
            return  # a slow server must not stack up overlapping polls
        self.polling = True
        try:
            await self._poll()
        except NoMatches:  # another screen (e.g. the command palette) is on top
            pass
        finally:
            self.polling = False

    async def _poll(self) -> None:
        try:
            st = await self.lms.rpc(self.pid, "status", "-", 1, "tags:alKcdJ")
        except Exception:
            return
        self.status, self.status_at = st, time.monotonic()
        loop = st.get("playlist_loop") or []
        track = loop[0] if loop else None
        title = self.query_one("#np-title", Static)
        sub = self.query_one("#np-sub", Static)
        if track:
            title.update(track.get("title", ""))
            sub.update(f"{track.get('artist', '')}\n{track.get('album', '')}")
            cid = track.get("coverid") or track.get("artwork_track_id")
            if cid and cid != self.cover and self.cfg["art"]:
                self.cover = cid
                self.run_worker(self.set_art(cid), exclusive=True, group="art")
        else:
            title.update("Nothing playing")
            sub.update("")
        if st.get("playlist_timestamp") != self.queue_stamp:
            self.queue_stamp = st.get("playlist_timestamp")
            self.run_worker(self.refresh_queue(), exclusive=True, group="queue")
        self.poll_count = getattr(self, "poll_count", 0) + 1
        if self.poll_count % 3 == 0 or not hasattr(self, "group_of"):
            await self.refresh_players()
        self.render_state()
        self.sub_title = self.players[self.player_idx]["name"]

    def on_resize(self, event: events.Resize) -> None:
        self.call_after_refresh(self.fit_art)

    def fit_art(self) -> None:
        """Size the art widget in cells so the cover keeps its aspect ratio."""
        if not self.cfg["art"]:
            return
        side = self.query_one("#side")
        cell = get_cell_size()
        ratio = (cell.width / cell.height) if cell.width and cell.height else 0.5
        text_rows = 11  # title, subtitle, progress, state and spacing below the art
        max_w = max(side.content_size.width, 8)
        max_h = max(side.content_size.height - text_rows, 4)
        w = min(max_w, int(max_h / ratio))
        art = self.query_one("#art", ArtWidget)
        art.styles.width, art.styles.height = w, max(int(w * ratio), 1)

    async def set_art(self, cid: str) -> None:
        img = await self.lms.art(cid, self.cfg["art_size"])
        if img and cid == self.cover:
            self.query_one("#art", ArtWidget).image = img
            self.fit_art()

    async def refresh_queue(self) -> None:
        res = await self.lms.rpc(self.pid, "status", 0, 500, "tags:alKd")
        t = self.query_one("#queue-t", DataTable)
        t.clear()
        for i, s in enumerate(res.get("playlist_loop", [])):
            t.add_row(str(i + 1), s.get("title", ""), s.get("artist", ""), s.get("album", ""),
                      fmt_time(s.get("duration")), key=str(i))
        cur = res.get("playlist_cur_index")
        if cur is not None and t.row_count:
            t.move_cursor(row=min(int(cur), t.row_count - 1))

    def current_time(self) -> float:
        st = self.status
        t = float(st.get("time", 0) or 0)
        if st.get("mode") == "play":
            t += time.monotonic() - self.status_at
        return min(t, float(st.get("duration", t) or t))

    def tick(self) -> None:
        try:
            self._tick()
        except NoMatches:  # timer fired during shutdown
            pass

    def _tick(self) -> None:
        dur = float(self.status.get("duration", 0) or 0)
        bar = self.query_one("#progress", ProgressBar)
        bar.update(total=dur or 100, progress=self.current_time() if dur else 0)
        self.render_state()

    def render_state(self) -> None:
        st = self.status
        if not st:
            return
        mode = {"play": "▶", "pause": "⏸", "stop": "⏹"}.get(st.get("mode"), "?")
        if not int(st.get("power", 1)):
            mode = "⏻ off"
        shuffle = ["off", "songs", "albums"][int(st.get("playlist shuffle", 0))]
        repeat = ["off", "track", "playlist"][int(st.get("playlist repeat", 0))]
        pos = f"{fmt_time(self.current_time())} / {fmt_time(st.get('duration'))}"
        idx, total = int(st.get("playlist_cur_index", 0) or 0) + 1, st.get("playlist_tracks", 0)
        self.query_one("#np-state", Static).update(Text(
            f"{mode}  {pos}\nVol {st.get('mixer volume', '?')}%   Track {idx}/{total}\n"
            f"Shuffle: {shuffle}   Repeat: {repeat}"
        ))


def main() -> None:
    ap = argparse.ArgumentParser(description="Terminal controller for Lyrion Music Server")
    ap.add_argument("-s", "--server", help="host:port (overrides config)")
    ap.add_argument("-p", "--player", help="player name or MAC (overrides config)")
    ap.add_argument("--no-art", action="store_true", help="disable album art")
    args = ap.parse_args()
    cfg = load_config()
    if args.server:
        cfg["server"] = args.server
    if args.player:
        cfg["player"] = args.player
    if args.no_art:
        cfg["art"] = False
    LMSCli(cfg).run()


if __name__ == "__main__":
    main()
