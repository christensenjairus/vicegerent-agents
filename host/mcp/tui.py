#!/usr/bin/env python3
"""Textual TUI dashboard for host MCP management.

Keybindings follow k9s conventions:
  j/k, ↑/↓    navigate rows
  g/G          top/bottom of list
  enter        toggle enable/disable on selected server
  ctrl-e       enable selected server
  ctrl-d       disable selected server
  ctrl-s       start the stack
  ctrl-k       stop (kill) the stack
  ctrl-r       reload config
  l            jump to logs for selected server
  d            describe selected server (config detail)
  ?            toggle help overlay
  1-4          switch log tabs (proxy, caddy, ghostunnel, supervisord)
  q / esc      quit
"""

from __future__ import annotations

import sys
from pathlib import Path
from threading import Thread
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).parent))

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Log, Markdown, Static, TabbedContent, TabPane

from vicegerent_mcp import (
    DEFAULT_AUTH_DIR,
    DEFAULT_CONFIG,
    DEFAULT_PROXY_DIR,
    DEFAULT_RUNTIME_DIR,
    _auth_label,
    _style_auth,
    _style_proc,
    auth_state,
    get_supervisor_states,
    iter_servers,
    load_config,
    load_state,
    reload_config,
    runtime_paths,
    set_server_enabled,
    start_stack,
    stop_stack,
    tail_log_iter,
)


# ---------------------------------------------------------------------------
# Help modal
# ---------------------------------------------------------------------------


class HelpScreen(ModalScreen):
    """Full-screen help overlay, dismissed by any keypress."""

    BINDINGS = [Binding("escape", "dismiss", show=False), Binding("?", "dismiss", show=False)]

    HELP_TEXT = """\
# Host MCP — keybindings

## Navigation
| Key | Action |
|-----|--------|
| `j` / `↓` | Move down |
| `k` / `↑` | Move up |
| `g` | Jump to top |
| `G` | Jump to bottom |

## Server actions
| Key | Action |
|-----|--------|
| `Enter` | Toggle enable/disable on selected server |
| `ctrl+e` | Enable selected server |
| `ctrl+d` | Disable selected server |
| `l` | Switch to logs tab for selected server |
| `d` | Describe selected server (config detail) |

## Stack control
| Key | Action |
|-----|--------|
| `ctrl+s` | Start the stack |
| `ctrl+k` | Stop (kill) the stack |
| `ctrl+r` | Reload config from disk |

## Log tabs
| Key | Tab |
|-----|-----|
| `1` | proxy |
| `2` | caddy |
| `3` | ghostunnel |
| `4` | supervisord |

## General
| Key | Action |
|-----|--------|
| `?` | Toggle this help |
| `q` / `Esc` | Quit |
"""

    def compose(self) -> ComposeResult:
        with Vertical(id="help-container"):
            yield Markdown(self.HELP_TEXT)

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-container {
        width: 70;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: double $primary;
        overflow-y: auto;
    }
    """


# ---------------------------------------------------------------------------
# Describe modal
# ---------------------------------------------------------------------------


class DescribeScreen(ModalScreen):
    """Show config details for a selected server."""

    BINDINGS = [Binding("escape", "dismiss", show=False), Binding("d", "dismiss", show=False), Binding("q", "dismiss", show=False)]

    def __init__(self, server) -> None:
        super().__init__()
        self._server = server

    def compose(self) -> ComposeResult:
        s = self._server
        env_lines = "\n".join(f"  {k}: {v}" for k, v in s.env.items()) or "  (none)"
        args_str = " ".join(s.args) or "(none)"
        text = f"""\
# {s.key}

| Field | Value |
|-------|-------|
| Name | {s.name} |
| Mode | {s.mode} |
| Command | `{s.command}` |
| Args | `{args_str}` |
| Enabled | {'yes' if s.enabled else 'no'} |
| URL | {s.url or '—'} |

**Env:**
```
{env_lines}
```

*Press `Esc`, `d`, or `q` to close.*
"""
        with Vertical(id="describe-container"):
            yield Markdown(text)

    DEFAULT_CSS = """
    DescribeScreen {
        align: center middle;
    }
    #describe-container {
        width: 72;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: double $primary;
        overflow-y: auto;
    }
    """


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


class HostMCPApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #header {
        height: 1;
        background: $primary;
        color: $text;
        text-align: center;
        padding: 0 1;
    }
    #server-table {
        height: auto;
        max-height: 12;
        border: solid $primary;
    }
    #infra-status {
        height: 1;
        padding: 0 1;
    }
    #log-tabs {
        height: 1fr;
        border: solid $primary-lighten-2;
    }
    Footer {
        height: 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        # Navigation (vim + arrows)
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "cursor_top", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
        # Row actions
        Binding("enter", "toggle_server", "Toggle", show=True),
        Binding("l", "logs_for_server", "Logs", show=True),
        Binding("d", "describe_server", "Describe", show=True),
        # Server mutations (ctrl = destructive/mutating, mirrors k9s)
        Binding("ctrl+e", "enable_server", "Enable", show=True),
        Binding("ctrl+d", "disable_server", "Disable", show=True),
        # Stack control
        Binding("ctrl+s", "start_stack", "Start", show=True),
        Binding("ctrl+k", "stop_stack", "Kill", show=True),
        Binding("ctrl+r", "reload_config", "Reload", show=True),
        # Log tabs
        Binding("1", "tab_proxy", "proxy", show=False),
        Binding("2", "tab_caddy", "caddy", show=False),
        Binding("3", "tab_ghostunnel", "ghostunnel", show=False),
        Binding("4", "tab_supervisord", "supervisord", show=False),
        # General
        Binding("question_mark", "help", "Help", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("escape", "quit", "Quit", show=False),
    ]

    def __init__(
        self,
        config_path: Path = DEFAULT_CONFIG,
        runtime_dir: Path = DEFAULT_RUNTIME_DIR,
        proxy_dir: Path = DEFAULT_PROXY_DIR,
        auth_dir: Path = DEFAULT_AUTH_DIR,
    ) -> None:
        super().__init__()
        self.config_path = config_path
        self.runtime_dir = runtime_dir
        self.proxy_dir = proxy_dir
        self.auth_dir = auth_dir
        self._servers: list = []  # cached for describe/logs lookups
        self._log_threads: list[Thread] = []

    def compose(self) -> ComposeResult:
        yield Static("Host MCP", id="header")
        yield DataTable(id="server-table")
        yield Static("", id="infra-status")
        with TabbedContent(id="log-tabs"):
            with TabPane("proxy", id="tab-proxy"):
                yield Log(id="log-proxy")
            with TabPane("caddy", id="tab-caddy"):
                yield Log(id="log-caddy")
            with TabPane("ghostunnel", id="tab-ghostunnel"):
                yield Log(id="log-ghostunnel")
            with TabPane("supervisord", id="tab-supervisord"):
                yield Log(id="log-supervisord")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#server-table", DataTable)
        table.add_columns("Server", "Mode", "Auth", "Enabled")
        table.cursor_type = "row"
        self.refresh_data()
        self.set_interval(2, self.refresh_data)
        self._start_log_tailers()

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------

    def refresh_data(self) -> None:
        self._refresh_header()
        self._refresh_server_table()
        self._refresh_infra_status()

    def _refresh_header(self) -> None:
        header = self.query_one("#header", Static)
        sup_states = get_supervisor_states(self.runtime_dir)
        procs = ("proxy", "caddy", "ghostunnel")
        if not sup_states:
            state_label = "[dim][STOPPED][/dim]"
        elif all(sup_states.get(p) == "RUNNING" for p in procs):
            state_label = "[green][RUNNING][/green]"
        else:
            state_label = "[yellow][DEGRADED][/yellow]"
        header.update(f"Host MCP  {state_label}  [dim]? help[/dim]")

    def _refresh_server_table(self) -> None:
        table = self.query_one("#server-table", DataTable)
        cursor_row = table.cursor_row
        table.clear()
        config = load_config(self.config_path)
        state = load_state(runtime_paths(self.runtime_dir)["state"])
        self._servers = iter_servers(config, state)
        for server in self._servers:
            auth_label = _auth_label(server, self.auth_dir)
            table.add_row(
                server.key,
                server.mode,
                _style_auth(auth_label),
                "[green]yes[/green]" if server.enabled else "[red]no[/red]",
            )
        if table.row_count > 0 and cursor_row < table.row_count:
            table.move_cursor(row=cursor_row)

    def _refresh_infra_status(self) -> None:
        status_widget = self.query_one("#infra-status", Static)
        sup_states = get_supervisor_states(self.runtime_dir)
        parts = []
        for proc in ("proxy", "caddy", "ghostunnel"):
            s = sup_states.get(proc, "STOPPED")
            parts.append(f"{proc}: {_style_proc(s)}")
        status_widget.update("  │  ".join(parts))

    # ------------------------------------------------------------------
    # Log tailers
    # ------------------------------------------------------------------

    def _start_log_tailers(self) -> None:
        paths = runtime_paths(self.runtime_dir)
        log_files = {
            "proxy": paths["logs"] / "proxy.log",
            "caddy": paths["logs"] / "caddy.log",
            "ghostunnel": paths["logs"] / "ghostunnel.log",
            "supervisord": paths["logs"] / "supervisord.log",
        }
        for name, log_file in log_files.items():
            log_widget = self.query_one(f"#log-{name}", Log)
            if not log_file.exists():
                log_widget.write_line("No log file yet — start the stack first.")
                continue
            t = Thread(target=self._tail_log, args=(log_file, log_widget), daemon=True)
            t.start()
            self._log_threads.append(t)

    def _tail_log(self, log_file: Path, log_widget: Log) -> None:
        try:
            for line in tail_log_iter(log_file, n_lines=50):
                self.call_from_thread(log_widget.write_line, line)
        except Exception:
            pass

    def _selected_server(self):
        table = self.query_one("#server-table", DataTable)
        if not self._servers or table.row_count == 0:
            return None
        idx = table.cursor_row
        if 0 <= idx < len(self._servers):
            return self._servers[idx]
        return None

    def _set_server(self, enabled: bool) -> None:
        server = self._selected_server()
        if not server:
            self.notify("No server selected", severity="warning")
            return
        try:
            set_server_enabled(server.key, enabled, self.config_path, self.runtime_dir, self.proxy_dir)
            verb = "Enabled" if enabled else "Disabled"
            self.notify(f"{verb} {server.key!r}")
        except SystemExit as e:
            self.notify(str(e), severity="error")
        self.refresh_data()

    # ------------------------------------------------------------------
    # Navigation actions
    # ------------------------------------------------------------------

    def action_cursor_up(self) -> None:
        self.query_one("#server-table", DataTable).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#server-table", DataTable).action_cursor_down()

    def action_cursor_top(self) -> None:
        t = self.query_one("#server-table", DataTable)
        t.move_cursor(row=0)

    def action_cursor_bottom(self) -> None:
        t = self.query_one("#server-table", DataTable)
        if t.row_count > 0:
            t.move_cursor(row=t.row_count - 1)

    # ------------------------------------------------------------------
    # Row actions
    # ------------------------------------------------------------------

    def action_toggle_server(self) -> None:
        server = self._selected_server()
        if not server:
            self.notify("No server selected", severity="warning")
            return
        self._set_server(not server.enabled)

    def action_enable_server(self) -> None:
        self._set_server(True)

    def action_disable_server(self) -> None:
        self._set_server(False)

    def action_describe_server(self) -> None:
        server = self._selected_server()
        if not server:
            self.notify("No server selected", severity="warning")
            return
        self.push_screen(DescribeScreen(server))

    def action_logs_for_server(self) -> None:
        """Jump to the log tab that corresponds to the selected server, if any."""
        server = self._selected_server()
        tabs = self.query_one("#log-tabs", TabbedContent)
        if server and server.key in ("proxy", "caddy", "ghostunnel", "supervisord"):
            tabs.active = f"tab-{server.key}"
        else:
            # Default to proxy tab for app-level servers
            tabs.active = "tab-proxy"

    # ------------------------------------------------------------------
    # Stack control (worker threads so TUI stays responsive)
    # ------------------------------------------------------------------

    @work(exclusive=True, thread=True)
    def action_start_stack(self) -> None:
        self.call_from_thread(self.notify, "Starting stack…")
        try:
            result = start_stack(self.config_path, self.runtime_dir, self.proxy_dir)
            if result == 0:
                self.call_from_thread(self.notify, "Stack started")
                self.call_from_thread(self._start_log_tailers)
            else:
                self.call_from_thread(self.notify, "Stack started with warnings — check logs", severity="warning")
        except SystemExit as e:
            self.call_from_thread(self.notify, str(e), severity="error")
        self.call_from_thread(self.refresh_data)

    @work(exclusive=True, thread=True)
    def action_stop_stack(self) -> None:
        self.call_from_thread(self.notify, "Stopping stack…")
        try:
            stop_stack(self.runtime_dir)
            self.call_from_thread(self.notify, "Stack stopped")
        except SystemExit as e:
            self.call_from_thread(self.notify, str(e), severity="error")
        self.call_from_thread(self.refresh_data)

    def action_reload_config(self) -> None:
        try:
            reload_config(self.config_path, self.runtime_dir, self.proxy_dir)
            self.notify("Config reloaded")
        except SystemExit as e:
            self.notify(str(e), severity="error")
        self.refresh_data()

    # ------------------------------------------------------------------
    # Log tab shortcuts
    # ------------------------------------------------------------------

    def action_tab_proxy(self) -> None:
        self.query_one("#log-tabs", TabbedContent).active = "tab-proxy"

    def action_tab_caddy(self) -> None:
        self.query_one("#log-tabs", TabbedContent).active = "tab-caddy"

    def action_tab_ghostunnel(self) -> None:
        self.query_one("#log-tabs", TabbedContent).active = "tab-ghostunnel"

    def action_tab_supervisord(self) -> None:
        self.query_one("#log-tabs", TabbedContent).active = "tab-supervisord"

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def action_help(self) -> None:
        self.push_screen(HelpScreen())


if __name__ == "__main__":
    HostMCPApp().run()
