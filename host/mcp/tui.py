#!/usr/bin/env python3
"""Textual TUI dashboard for the vicegerent host ToolHive stack.

Read-mostly dashboard over vicegerent_mcp: shows the 6 ToolHive workloads plus
the supervised vMCP / ghostunnel / caffeinate processes, tails their logs, and
offers start / stop / restart of the supervised stack.

Keybindings (k9s-flavoured):
  j/k, ↑/↓     navigate workload rows
  ctrl+s        start the stack
  ctrl+k        stop (kill) the supervised stack
  ctrl+r        restart the supervised stack
  1-4           switch log tabs (vmcp, ghostunnel, supervisord, caffeinate)
  r             refresh now
  ?             help
  q / esc       quit
"""

from __future__ import annotations

import sys
from pathlib import Path
from threading import Thread
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parent))

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Log, Markdown, Static, TabbedContent, TabPane

from vicegerent_mcp import (
    DEFAULT_RUNTIME_DIR,
    DEFAULT_SERVERS_CONFIG,
    SUPERVISED_PROGRAMS,
    get_supervisor_states,
    group_name,
    is_server_enabled,
    list_workloads,
    load_server_state,
    load_servers_config,
    runtime_paths,
    start_stack,
    stop_stack,
    tail_log_iter,
)

LOG_TABS = ("vmcp", "ghostunnel", "supervisord", "caffeinate")


def _proc_markup(state: str) -> str:
    if state == "RUNNING":
        return f"[green]{state}[/green]"
    if state in ("STARTING", "BACKOFF"):
        return f"[yellow]{state}[/yellow]"
    if state:
        return f"[red]{state}[/red]"
    return "[dim]STOPPED[/dim]"


def _workload_markup(state: str) -> str:
    if state == "running":
        return f"[green]{state}[/green]"
    if state in ("starting", "auth_retrying", "authenticating"):
        return f"[yellow]{state}[/yellow]"
    if state:
        return f"[red]{state}[/red]"
    return "[dim]not created[/dim]"


class HelpScreen(ModalScreen):
    """Full-screen help overlay."""

    BINDINGS = [Binding("escape", "dismiss", show=False), Binding("question_mark", "dismiss", show=False)]

    HELP_TEXT = """\
# vicegerent host stack — keybindings

## Navigation
| Key | Action |
|-----|--------|
| `j` / `↓` | Move down |
| `k` / `↑` | Move up |
| `r` | Refresh now |

## Stack control
| Key | Action |
|-----|--------|
| `ctrl+s` | Start the stack |
| `ctrl+k` | Stop (kill) the supervised stack |
| `ctrl+r` | Restart the supervised stack |

## Log tabs
| Key | Tab |
|-----|-----|
| `1` | vmcp |
| `2` | ghostunnel |
| `3` | supervisord |
| `4` | caffeinate |

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
    HelpScreen { align: center middle; }
    #help-container {
        width: 64; height: auto; max-height: 80%;
        padding: 1 2; background: $surface; border: double $primary; overflow-y: auto;
    }
    """


class HostMCPApp(App):
    CSS = """
    Screen { layout: vertical; }
    #header {
        height: 1; background: $primary; color: $text; text-align: center; padding: 0 1;
    }
    #workload-table { height: auto; max-height: 12; border: solid $primary; }
    #infra-status { height: 1; padding: 0 1; }
    #log-tabs { height: 1fr; border: solid $primary-lighten-2; }
    Footer { height: 1; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("r", "refresh_now", "Refresh", show=True),
        Binding("ctrl+s", "start_stack", "Start", show=True),
        Binding("ctrl+k", "stop_stack", "Kill", show=True),
        Binding("ctrl+r", "restart_stack", "Restart", show=True),
        Binding("1", "tab_vmcp", "vmcp", show=False),
        Binding("2", "tab_ghostunnel", "ghostunnel", show=False),
        Binding("3", "tab_supervisord", "supervisord", show=False),
        Binding("4", "tab_caffeinate", "caffeinate", show=False),
        Binding("question_mark", "help", "Help", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("escape", "quit", "Quit", show=False),
    ]

    def __init__(
        self,
        runtime_dir: Path = DEFAULT_RUNTIME_DIR,
        servers_config: Path = DEFAULT_SERVERS_CONFIG,
    ) -> None:
        super().__init__()
        self.runtime_dir = runtime_dir
        self.servers_config = servers_config
        self.config = load_servers_config(servers_config)
        self.group = group_name(self.config)
        self._log_threads: list[Thread] = []
        self._tailing = False

    def compose(self) -> ComposeResult:
        yield Static("vicegerent host stack", id="header")
        yield DataTable(id="workload-table")
        yield Static("", id="infra-status")
        with TabbedContent(id="log-tabs"):
            for name in LOG_TABS:
                with TabPane(name, id=f"tab-{name}"):
                    yield Log(id=f"log-{name}")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#workload-table", DataTable)
        table.add_columns("Workload", "Status")
        table.cursor_type = "row"
        self.refresh_data()
        self.set_interval(2, self.refresh_data)
        self._start_log_tailers()

    # ------------------------------------------------------------------ data

    def refresh_data(self) -> None:
        sup_states = get_supervisor_states(self.runtime_dir)
        self._refresh_header(sup_states)
        self._refresh_workloads()
        self._refresh_infra(sup_states)

    def _refresh_header(self, sup_states: dict[str, str]) -> None:
        header = self.query_one("#header", Static)
        if not sup_states:
            label = "[dim][STOPPED][/dim]"
        elif all(sup_states.get(p) == "RUNNING" for p in SUPERVISED_PROGRAMS):
            label = "[green][RUNNING][/green]"
        else:
            label = "[yellow][DEGRADED][/yellow]"
        header.update(f"vicegerent host stack  {label}  [dim]group={self.group}  ? help[/dim]")

    def _refresh_workloads(self) -> None:
        table = self.query_one("#workload-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        workloads = list_workloads(self.group)
        state = load_server_state(self.runtime_dir)
        for server in self.config.get("servers", []):
            name = server["name"]
            if not is_server_enabled(server, state):
                table.add_row(name, "[dim]disabled[/dim]")
            else:
                table.add_row(name, _workload_markup(workloads.get(name, "")))
        if table.row_count and cursor < table.row_count:
            table.move_cursor(row=cursor)

    def _refresh_infra(self, sup_states: dict[str, str]) -> None:
        widget = self.query_one("#infra-status", Static)
        parts = [f"{p}: {_proc_markup(sup_states.get(p, ''))}" for p in SUPERVISED_PROGRAMS]
        widget.update("  │  ".join(parts))

    # ------------------------------------------------------------------ logs

    def _start_log_tailers(self) -> None:
        if self._tailing:
            return
        self._tailing = True
        paths = runtime_paths(self.runtime_dir)
        for name in LOG_TABS:
            log_file = paths["logs"] / f"{name}.log"
            widget = self.query_one(f"#log-{name}", Log)
            if not log_file.exists():
                widget.write_line("No log file yet — start the stack first.")
                continue
            t = Thread(target=self._tail, args=(log_file, widget), daemon=True)
            t.start()
            self._log_threads.append(t)

    def _tail(self, log_file: Path, widget: Log) -> None:
        try:
            for line in tail_log_iter(log_file, n_lines=50):
                self.call_from_thread(widget.write_line, line)
        except Exception:
            pass

    # --------------------------------------------------------------- actions

    def action_cursor_up(self) -> None:
        self.query_one("#workload-table", DataTable).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#workload-table", DataTable).action_cursor_down()

    def action_refresh_now(self) -> None:
        self.refresh_data()

    @work(exclusive=True, thread=True)
    def action_start_stack(self) -> None:
        self.call_from_thread(self.notify, "Starting stack… (workloads + vMCP + ghostunnel)")
        try:
            rc = start_stack(self.runtime_dir, self.servers_config)
            msg = "Stack started" if rc == 0 else "Stack started with warnings — check logs"
            self.call_from_thread(self.notify, msg, severity="information" if rc == 0 else "warning")
            self.call_from_thread(self._start_log_tailers)
        except SystemExit as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
        self.call_from_thread(self.refresh_data)

    @work(exclusive=True, thread=True)
    def action_stop_stack(self) -> None:
        self.call_from_thread(self.notify, "Stopping supervised stack…")
        try:
            stop_stack(self.runtime_dir, self.servers_config)
            self.call_from_thread(self.notify, "Supervised stack stopped (workloads left running)")
        except SystemExit as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
        self.call_from_thread(self.refresh_data)

    @work(exclusive=True, thread=True)
    def action_restart_stack(self) -> None:
        self.call_from_thread(self.notify, "Restarting supervised stack…")
        try:
            stop_stack(self.runtime_dir, self.servers_config)
            rc = start_stack(self.runtime_dir, self.servers_config)
            msg = "Stack restarted" if rc == 0 else "Restarted with warnings — check logs"
            self.call_from_thread(self.notify, msg, severity="information" if rc == 0 else "warning")
        except SystemExit as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
        self.call_from_thread(self.refresh_data)

    def _activate_tab(self, name: str) -> None:
        self.query_one("#log-tabs", TabbedContent).active = f"tab-{name}"

    def action_tab_vmcp(self) -> None:
        self._activate_tab("vmcp")

    def action_tab_ghostunnel(self) -> None:
        self._activate_tab("ghostunnel")

    def action_tab_supervisord(self) -> None:
        self._activate_tab("supervisord")

    def action_tab_caffeinate(self) -> None:
        self._activate_tab("caffeinate")

    def action_help(self) -> None:
        self.push_screen(HelpScreen())


if __name__ == "__main__":
    HostMCPApp().run()
