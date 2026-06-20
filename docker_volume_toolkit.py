#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["rich>=13", "textual>=0.80"]
# ///
"""Docker Volume Toolkit - migrate, archive, or delete Docker volumes.

Run with no command for an interactive menu (Textual TUI). Or pick a mode
directly with a command; add --yes to run it headless (plain console, no TUI):

  migrate   copy every volume named {from_prefix}{tail} to {to_prefix}{tail}
  archive   tar.gz selected volumes into the current directory
  delete    permanently remove selected volumes

MIGRATE preserves the tail - whatever follows the FROM prefix (_home,
_workspace, _certs, _db, ...). The destination is overwritten via rsync
--delete; the source volume is left intact for verification before manual
removal.

A command without --yes opens that mode's TUI pre-filled from the flags; with
--yes it runs non-interactively (CI / cron friendly). --dry-run works globally
and on every command - mount / inspect only, writing and deleting nothing.

Usage:
  ./docker_volume_toolkit.py                          # interactive menu
  ./docker_volume_toolkit.py --dry-run                # menu, verify-only mode
  ./docker_volume_toolkit.py migrate --from a_ --to b_ --yes
  ./docker_volume_toolkit.py archive --filter '_home$' --yes
  ./docker_volume_toolkit.py delete  --filter '^scratch_' --yes --dry-run

Note: Docker encodes '.' in volume names as '-2e' (e.g. 'alice.smith'
appears as 'alice-2esmith'). The --filter regex matches the full volume name.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

# Force truecolor so Rich/Textual emit exact 24-bit hex instead of downsampling
# to the 256-color palette (where dark slates land on xterm teal). WSL / Docker
# Desktop terminals frequently leave COLORTERM unset even though the host
# terminal (Windows Terminal, VS Code) renders truecolor fine. setdefault so an
# explicit user override still wins.
os.environ.setdefault("COLORTERM", "truecolor")


def _preflight() -> None:
    """Verify the third-party deps are importable before anything else loads.
    Runs ahead of the rich/textual imports so a missing package yields a clear
    'install this' message instead of a raw ImportError. When launched via the
    shebang (`uv run --script`) uv installs the inline deps first, so this is a
    silent no-op; it only bites when the script is run with a plain Python that
    lacks the packages."""
    import importlib.util

    def _present(mod: str) -> bool:
        # find_spec can raise (e.g. broken parent package); treat any failure
        # as "not importable" so the preflight itself never tips over.
        try:
            return importlib.util.find_spec(mod) is not None
        except Exception:
            return False

    required = {"rich": "rich>=13", "textual": "textual>=0.80"}
    missing = [spec for mod, spec in required.items() if not _present(mod)]
    if not missing:
        return
    print("docker_volume_toolkit.py is missing required packages:", file=sys.stderr)
    for spec in missing:
        print(f"  - {spec}", file=sys.stderr)
    print(f"\nInstall with:  pip install {' '.join(missing)}", file=sys.stderr)
    print("Or run via uv:  ./docker_volume_toolkit.py  (auto-installs)",
          file=sys.stderr)
    sys.exit(1)


_preflight()

from rich.console import Console, Group
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from rich.theme import Theme


# Duoptimum brand palette: cyan + orange against a dark blue-grey base.
# Sourced from stellars-jupyterhub-ds/services/jupyterhub/duoptimum-hub-web/
# src/theme/tokens.ts (DARK). All non-brand roles derived as hue shifts.
DUO = {
    # Brand cyan family
    "cyan":         "#21a8e4",   # accent
    "cyan_bright":  "#46bcf0",   # accentHover
    "cyan_deep":    "#0e93cf",   # accentActive
    "cyan_dark":    "#0096d1",   # info
    # Brand orange family
    "orange":       "#da8230",   # accent2 / warning
    "orange_bright": "#f0a050",  # hue shift, lighter
    "orange_dark":  "#a86420",   # hue shift, darker
    # Derived hue shifts (single-step rotations off cyan / orange)
    "amber":        "#e6c660",   # orange -> yellow (filter highlight)
    "mint":         "#3fb950",   # cyan -> green (success)
    "rose":         "#ef4444",   # warm danger
    # Dark base (blacks)
    "bg_dim":       "#1a1f25",   # screen background - dimmer, lets items pop
    "bg":           "#252b32",
    "bg_subtle":    "#2a313a",
    "surface":      "#303841",
    "surface_hi":   "#374049",
    "border":       "#404b54",
    "border_hi":    "#4d5a65",
    # Text
    "text":         "#c3c3c3",
    "text_muted":   "#a5a5a5",
    "text_subtle":  "#7d8791",
}

# Role mapping (kept as PASTEL key for site compatibility; values now duoptimum)
PASTEL = {
    "from":     DUO["orange"],         # source highlight (left)
    "to":       DUO["cyan"],           # destination highlight (right)
    "filter":   DUO["amber"],          # filter-match user portion (both)
    "user":     DUO["text"],           # neutral user-segment text
    "dim":      DUO["text_subtle"],    # filter-excluded rows
    "title":    DUO["cyan_bright"],    # panel headers / section titles
    "label":    DUO["text_muted"],     # field descriptions
    "ok":       DUO["mint"],           # green for success
    "warn":     DUO["orange"],         # orange for warnings
    "err":      DUO["rose"],           # red for errors
    "info":     DUO["cyan_dark"],      # info text
    "accent":   DUO["orange"],         # OVERALL progress bar
    "bar_bg":   DUO["surface"],        # bar background
}


ALPINE_IMG = "alpine:latest"

# Remap rich's named primaries to the pastel palette so existing [red] / [bold blue]
# / etc. markup throughout the script renders softly without per-site rewrites.
# Also override rich's default progress.* / bar.* styles (speed=red, download=
# green, remaining=cyan, bar.complete=bright magenta) which read as too harsh.
PASTEL_THEME = Theme({
    "red":     PASTEL["err"],
    "green":   PASTEL["ok"],
    "blue":    PASTEL["info"],
    "yellow":  PASTEL["warn"],
    "cyan":    PASTEL["title"],
    "magenta": PASTEL["accent"],
    # progress bar internals
    "bar.back":              DUO["surface"],
    "bar.complete":          DUO["cyan"],
    "bar.finished":          DUO["mint"],
    "bar.pulse":             DUO["cyan"],
    "progress.description":  DUO["text"],
    "progress.percentage":   DUO["cyan"],
    "progress.filesize":     DUO["cyan_dark"],
    "progress.filesize.total": DUO["text_muted"],
    "progress.download":     DUO["cyan_dark"],
    "progress.data.speed":   DUO["orange"],
    "progress.remaining":    DUO["text_muted"],
    "progress.elapsed":      DUO["text_muted"],
    "progress.spinner":      DUO["cyan"],
})

console = Console(theme=PASTEL_THEME)

VERSION = "1.2.4"
APP_TITLE = "Docker Volume Toolkit"

# Shared top header bar: app name on the left, version pinned to the right
# corner. Embedded into each screen's CSS via {HEADER_CSS}.
HEADER_CSS = f"""
        #app-header {{ height: 1; background: {DUO['bg_subtle']}; }}
        #hdr-title {{ width: 1fr; padding: 0 2; color: {PASTEL['title']}; text-style: bold; }}
        #hdr-version {{ width: auto; padding: 0 2; color: {DUO['text_subtle']}; }}
"""


@dataclass
class Migration:
    tail: str          # the part of the name after the FROM prefix
    src: str
    dst: str
    size_bytes: int = 0
    error: str = ""
    success: bool = field(default=False)
    removed: bool = field(default=False)

    @property
    def label(self) -> str:
        return self.tail


@dataclass
class Archive:
    name: str          # the source volume name
    size_bytes: int = 0
    target: str = ""   # rendered archive filename (basename, lands in cwd)
    error: str = ""
    success: bool = field(default=False)

    @property
    def label(self) -> str:
        return self.name


@dataclass
class Deletion:
    name: str          # the volume to remove
    error: str = ""
    success: bool = field(default=False)

    @property
    def label(self) -> str:
        return self.name


def render_template(name: str, template: str, when) -> str:
    """Substitute {name}, {date}, {datetime} in an archive filename template.
    `when` is a datetime passed once per run so every rendered name shares the
    same stamp. The result is reduced to a basename (path separators stripped)
    so archives land directly in the current working directory."""
    rendered = (
        template
        .replace("{name}", name)
        .replace("{datetime}", when.strftime("%Y-%m-%d_%H%M%S"))
        .replace("{date}", when.strftime("%Y-%m-%d"))
    )
    return os.path.basename(rendered.replace("\\", "/"))


def human(b: int) -> str:
    f = float(b)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} PB"


def fmt_duration(seconds: float) -> str:
    """MM:SS, or H:MM:SS once it crosses an hour."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def trunc_left(s: str, width: int) -> str:
    """Truncate from the left with a leading ellipsis, keeping the tail (the
    meaningful end of a volume name) visible."""
    if len(s) <= width:
        return s
    return "…" + s[-(width - 1):]


def styled_volume(
    full: str,
    prefix: str,
    prefix_style: str,
    base_style: str,
    match_style,
    filt_re,
) -> Text:
    """Render a full volume name: the leading `prefix` in `prefix_style`, the
    rest in `base_style`, with every filter-matched span overlaid in
    `match_style`. The filter is matched against the whole name, so a match
    anywhere (prefix, body, or tail) is highlighted."""
    t = Text(full, style=base_style)
    if prefix and full.startswith(prefix):
        t.stylize(prefix_style, 0, len(prefix))
    if filt_re:
        for mo in filt_re.finditer(full):
            if mo.end() > mo.start():
                t.stylize(match_style, mo.start(), mo.end())
    return t


def _common_prefix(names: list[str]) -> str:
    """Longest string shared by the start of every name."""
    if not names:
        return ""
    first, last = min(names), max(names)
    i = 0
    while i < len(first) and i < len(last) and first[i] == last[i]:
        i += 1
    return first[:i]


def autocomplete_prefix(value: str, names: list[str]) -> str:
    """Fish-style one-segment autocomplete for a prefix field.

    Extend `value` toward the common prefix of the `names` that start with it,
    advancing by a single slug - up to and including the next `-`/`_` separator.
    Returns `value` unchanged when the next segment is ambiguous (the common
    prefix does not grow), so the user must type to disambiguate rather than
    have a variant guessed for them."""
    cands = [n for n in names if n.startswith(value)]
    if not cands:
        return value
    lcp = _common_prefix(cands)
    if len(lcp) <= len(value):
        return value  # nothing unambiguous to add - user must type
    for i, ch in enumerate(lcp[len(value):]):
        if ch in "-_":
            return lcp[: len(value) + i + 1]  # stop after the separator
    return lcp  # no separator ahead - take the whole unambiguous remainder


def docker(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], text=True, **kw)


def list_volumes() -> set[str]:
    r = docker(["volume", "ls", "-q"], capture_output=True, check=True)
    return {v for v in r.stdout.split() if v}


def remove_volume(name: str) -> tuple[bool, str]:
    r = docker(["volume", "rm", name], capture_output=True)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, ""


def _preflight_docker() -> None:
    """Ensure the host dependency is in place before any action runs.

    Every operation shells out to `docker volume` / `docker run`, so a clear
    message here beats an opaque failure mid-run. Two checks: the `docker`
    client is on PATH, and its daemon is reachable (a server-version query
    succeeds). The container-side tools (rsync / tar / pv) are installed inside
    the disposable alpine container at runtime, so Docker is the only host
    dependency to secure here; rich / textual are handled by `_preflight()`."""
    if shutil.which("docker") is None:
        console.print(
            f"[{PASTEL['err']}]Docker is not installed or not on PATH.[/]"
        )
        console.print(
            f"[{PASTEL['label']}]Install Docker, then re-run: "
            f"https://docs.docker.com/get-docker/[/]"
        )
        sys.exit(1)
    r = docker(["version", "--format", "{{.Server.Version}}"],
               capture_output=True)
    if r.returncode != 0 or not r.stdout.strip():
        console.print(
            f"[{PASTEL['err']}]Docker is installed but the daemon is not "
            f"reachable.[/]"
        )
        msg = (r.stderr or r.stdout).strip()
        if msg:
            console.print(f"[{PASTEL['label']}]{msg.splitlines()[-1]}[/]")
        console.print(
            f"[{PASTEL['label']}]Start the Docker daemon (or the Docker "
            f"Desktop / engine service) and try again.[/]"
        )
        sys.exit(1)


def list_candidate_volumes() -> list[str]:
    """All Docker volumes - the designer pool; FROM prefix narrows it."""
    return sorted(list_volumes())


def volume_size(volume: str) -> int:
    name = f"migrate-size-{volume}".replace("/", "-").replace(".", "-")[:63]
    docker(["rm", "-f", name], capture_output=True)
    r = docker(
        ["run", "--rm", "--name", name, "-v", f"{volume}:/d:ro",
         ALPINE_IMG, "du", "-sb", "/d"],
        capture_output=True,
    )
    if r.returncode != 0:
        return 0
    try:
        return int(r.stdout.split()[0])
    except (ValueError, IndexError):
        return 0


def discover_migrations(
    from_prefix: str,
    to_prefix: str,
    user_filter: re.Pattern | None,
) -> list[Migration]:
    all_volumes = list_volumes()
    pattern = re.compile(rf"^{re.escape(from_prefix)}(.+)$")
    found = []
    for v in all_volumes:
        m = pattern.match(v)
        if not m:
            continue
        tail = m.group(1)
        if user_filter and not user_filter.search(v):
            continue
        dst = f"{to_prefix}{tail}"
        if dst == v:
            continue
        found.append(Migration(tail=tail, src=v, dst=dst))
    return sorted(found, key=lambda x: x.tail)


def ensure_dst_exists(m: Migration, all_volumes: set[str]) -> bool:
    if m.dst in all_volumes:
        return True
    r = docker(["volume", "create", m.dst], capture_output=True)
    if r.returncode == 0:
        all_volumes.add(m.dst)
        return True
    m.error = f"failed to create dest volume: {r.stderr.strip()}"
    return False


def print_plan(migs: list[Migration]) -> None:
    t = Table(title="Migration Plan", show_lines=False)
    t.add_column("#", justify="right", style="bold cyan")
    t.add_column("Tail", style="bold")
    t.add_column("Source", style="cyan")
    t.add_column("Destination", style="green")
    t.add_column("Size", justify="right", style="yellow")
    total = 0
    have_sizes = any(m.size_bytes for m in migs)
    for i, m in enumerate(migs, 1):
        size_str = human(m.size_bytes) if m.size_bytes else "-"
        t.add_row(str(i), m.tail, m.src, m.dst, size_str)
        total += m.size_bytes
    console.print(t)
    if have_sizes:
        console.print(f"[bold]Total to copy: {human(total)}[/bold]")


def parse_selection(s: str, max_n: int) -> list[int]:
    """Parse '1,3,5-7' style selection - returns sorted 1-based indices."""
    result: set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
            if start < 1 or end > max_n or start > end:
                raise ValueError(f"range out of bounds: {part}")
            result.update(range(start, end + 1))
        else:
            n = int(part)
            if n < 1 or n > max_n:
                raise ValueError(f"index out of bounds: {part}")
            result.add(n)
    return sorted(result)


def container_name(m: Migration) -> str:
    """Disposable container name per migration - safe Docker name characters."""
    safe = m.label.replace("/", "-").replace(".", "-")
    return f"migrate-vol-{safe}"


def run_dry(m: Migration) -> bool:
    """Mount both volumes and verify access without copying.
    Each migration uses its own dedicated container, removed on exit."""
    name = container_name(m)
    # Defensive: remove any stale container with this name
    docker(["rm", "-f", name], capture_output=True)
    r = docker(
        [
            "run", "--rm", "--name", name,
            "-v", f"{m.src}:/src:ro",
            "-v", f"{m.dst}:/dst",
            ALPINE_IMG, "sh", "-c",
            "test -d /src && test -d /dst && ls -la /src > /dev/null && ls -la /dst > /dev/null",
        ],
        capture_output=True,
    )
    if r.returncode != 0:
        m.error = (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout) else "mount check failed"
        return False
    return True


PROGRESS_RE = re.compile(r"^\s*([\d,]+)\s+(\d+)%\s+[\d.]+[kKmMgGtT]?B/s")


def run_copy(m: Migration, on_progress, on_stage) -> bool:
    """Run rsync inside an alpine container, parse --info=progress2 output.

    Two stages: 'discovery' (apk add rsync + rsync's source scan, no byte
    progress yet) then 'migration' (bytes flowing). `on_stage(name)` fires on
    each transition; `on_progress(completed, total)` is called as bytes flow so
    any front-end (Textual bar, etc.) can render it. The caller owns the
    overall/aggregate counter.
    """
    name = container_name(m)
    # Defensive: remove any stale container with this name
    docker(["rm", "-f", name], capture_output=True)
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "-v", f"{m.src}:/src:ro",
        "-v", f"{m.dst}:/dst",
        ALPINE_IMG,
        "sh", "-c",
        # apk add rsync, then sync. -aAX preserves all metadata; --delete
        # makes destination match source (clears default skeleton files).
        "apk add --no-cache rsync >/dev/null 2>&1 && "
        "rsync -aAX --delete --info=progress2 --no-inc-recursive /src/ /dst/",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    last_err = ""
    est_total = 0
    last_bytes = 0
    transferring = False
    on_stage("discovery")
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        match = PROGRESS_RE.match(line)
        if match:
            bytes_done = int(match.group(1).replace(",", ""))
            pct = int(match.group(2))
            if pct > 0:
                # Real, measurable progress: rsync now reports a total. This is
                # the true start of migration. Flip stage once, then feed the
                # determinate bar. Re-estimate each line - later ones sharpen.
                est_total = int(bytes_done * 100 / pct)
                if not transferring:
                    on_stage("migration")
                    transferring = True
                on_progress(bytes_done, est_total)
                last_bytes = bytes_done
            else:
                # pct still 0 - transfer warming up, total unknown. Stay in
                # discovery (orange pulse) instead of flashing a full green bar.
                last_bytes = max(last_bytes, bytes_done)
        elif line and "rsync" in line.lower() and ("error" in line.lower() or "failed" in line.lower()):
            last_err = line
    proc.wait()
    if proc.returncode != 0:
        m.error = last_err or f"rsync exit code {proc.returncode}"
        return False
    # Lock total to actual final bytes and mark complete
    final = max(est_total, last_bytes)
    m.size_bytes = final
    on_progress(final, final)
    return True


# ===========================================================================
# Archive / delete workers (mirror run_dry / run_copy)
# ===========================================================================


def _safe_container_name(prefix: str, name: str) -> str:
    safe = name.replace("/", "-").replace(".", "-")
    return f"{prefix}-{safe}"[:63]


def archive_dry(a: Archive) -> bool:
    """Verify the archive can be produced without writing anything: mount the
    volume read-only at /src and the host cwd read-write at /out, check both
    are reachable. Keeps a.target; WRITES NOTHING. Sets a.error on failure."""
    name = _safe_container_name("archive-dry", a.name)
    docker(["rm", "-f", name], capture_output=True)
    r = docker(
        [
            "run", "--rm", "--name", name,
            "-v", f"{a.name}:/src:ro",
            "-v", f"{os.getcwd()}:/out",
            ALPINE_IMG, "sh", "-c",
            "test -d /src && test -w /out",
        ],
        capture_output=True,
    )
    if r.returncode != 0:
        out = (r.stderr or r.stdout).strip()
        a.error = out.splitlines()[-1] if out else "mount check failed"
        return False
    return True


PV_PCT_RE = re.compile(r"^\s*(\d+)\s*$")


def run_archive_worker(a: Archive, on_progress, on_stage) -> bool:
    """tar.gz the volume into the host cwd inside a disposable alpine container.

    Two stages: 'discovery' (apk add tar pv, tar scan) then 'archiving' (bytes
    flowing). pv -n prints integer percentages on stderr, one per line; on the
    first percentage fire on_stage('archiving') and feed on_progress(done,total)
    with total=size_bytes. Before the first percentage stay in discovery. When
    size_bytes is 0 or pv is unavailable, fall back to an indeterminate run (no
    on_progress) and just complete on success."""
    name = _safe_container_name("archive-vol", a.name)
    docker(["rm", "-f", name], capture_output=True)
    size = a.size_bytes
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "-v", f"{a.name}:/src:ro",
        "-v", f"{os.getcwd()}:/out",
        ALPINE_IMG,
        "sh", "-c",
        # apk add tar + pv, then stream the tar through pv (byte progress) into
        # gzip. -n makes pv emit integer percentages on stderr. -s gives it the
        # total so percentages are meaningful.
        "apk add --no-cache tar pv >/dev/null 2>&1 && "
        f"tar cf - -C /src . | pv -n -s {size} | gzip > /out/{a.target}",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    archiving = False
    on_stage("discovery")
    assert proc.stderr is not None
    for line in proc.stderr:
        line = line.strip()
        match = PV_PCT_RE.match(line)
        if not match:
            continue
        pct = int(match.group(1))
        if size > 0:
            if not archiving:
                on_stage("archiving")
                archiving = True
            on_progress(int(pct / 100 * size), size)
    proc.wait()
    if proc.returncode != 0:
        a.error = f"archive exit code {proc.returncode}"
        return False
    if size > 0:
        on_progress(size, size)
    return True


def delete_dry(d: Deletion) -> bool:
    """Verify the volume exists without deleting anything. True if present;
    sets d.error and returns False when missing."""
    r = docker(["volume", "inspect", d.name], capture_output=True)
    if r.returncode != 0:
        out = (r.stderr or r.stdout).strip()
        d.error = out.splitlines()[-1] if out else "volume not found"
        return False
    return True


# ===========================================================================
# Interactive designer (Textual TUI)
# ===========================================================================


def run_designer(
    candidates: list[str],
    from_p: str = "",
    to_p: str = "",
    filt: str = "",
    worker_count: int = 3,
    overwrite: bool = False,
    rm_src: bool = False,
) -> tuple[str, str, str, int, bool, bool] | None:
    """Launch designer; return (from, to, filter, workers, overwrite, rm_src) or None."""
    # Import locally so non-interactive runs don't pay the textual import cost.
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Checkbox, Footer, Input, Static

    FROM_STYLE = f"black on {PASTEL['from']}"
    TO_STYLE = f"black on {PASTEL['to']}"
    FILT_STYLE = f"black on {PASTEL['filter']}"
    USER_STYLE = PASTEL["user"]
    DIM_STYLE = PASTEL["dim"]

    class DesignerApp(App):
        CSS = f"""
        Screen {{ layout: vertical; background: {DUO['bg_dim']}; layers: base overlay; align: center middle; }}
{HEADER_CSS}
        /* Validation warning: a content-sized overlay box centred by the
           Screen, floating over the still-visible designer (same pattern as
           the execution completion popup). */
        #warn-box {{
            layer: overlay;
            display: none;
            width: auto; height: auto;
            max-width: 80%;
            padding: 1 4;
            background: {DUO['bg_subtle']};
            border: heavy {DUO['rose']};
            text-align: center;
        }}
        #warn-box.-show {{ display: block; }}
        #inputs {{
            height: auto;
            padding: 1 2 0 2;
            background: {DUO['bg_subtle']};
        }}
        #status-bar {{
            height: auto;
            padding: 0 2;
            background: {DUO['bg_subtle']};
            border-bottom: heavy {DUO['border']};
        }}
        .desc {{ color: {DUO['text_muted']}; height: 1; }}
        Input {{
            margin: 0 0 1 0;
            background: {DUO['surface']};
            border: round {DUO['border']};
            color: {DUO['text']};
        }}
        Input:focus {{ border: round {DUO['cyan']}; }}

        .row {{ height: auto; layout: horizontal; }}
        .field-grow {{ width: 1fr; height: auto; padding-right: 1; }}
        .field-small {{ width: 28; height: auto; padding-right: 1; }}
        .field-tiny {{ width: 22; height: auto; }}

        Checkbox {{
            background: {DUO['bg_subtle']};
            border: round {DUO['border']};
            color: {DUO['text']};
            margin: 0;
            padding: 0 1;
        }}
        Checkbox:focus {{ border: round {DUO['cyan']}; }}
        /* The button box (▐ ▌) uses 'surface' so it stays visible against the
           Checkbox's 'bg_subtle'. Textual always renders the literal 'X' inner
           glyph; off-state hides it by coloring it the same as the box bg. */
        #overwrite > .toggle--button, #rmsrc > .toggle--button {{
            background: {DUO['surface']};
            color: {DUO['surface']};
        }}
        #overwrite.-on > .toggle--button, #rmsrc.-on > .toggle--button {{
            background: {DUO['surface']};
            color: {DUO['orange']};
            text-style: bold;
        }}

        #panels {{ height: 1fr; background: {DUO['bg']}; }}
        .col {{ width: 1fr; padding: 0 1; }}
        .left-col {{ border-right: solid {DUO['border']}; }}
        /* Panel bodies take Tab focus; ring stays invisible (panel bg) until
           focused, then cyan - same affordance as the input controls. */
        .panel-body {{ border: round {DUO['bg']}; }}
        .panel-body:focus {{ border: round {DUO['cyan']}; }}
        .col-title {{
            background: {DUO['surface']};
            color: {DUO['cyan_bright']};
            padding: 0 1;
            text-style: bold;
            text-align: center;
            height: 1;
        }}

        Footer {{ background: {DUO['bg_subtle']}; color: {DUO['text_muted']}; }}
        """

        BINDINGS = [
            Binding("up", "focus_up", "Up", show=False),
            Binding("down", "focus_down", "Down", show=False),
            Binding("enter", "confirm", "Confirm", priority=True),
            Binding("ctrl+s", "confirm", "Confirm", show=False),
            Binding("shift+tab", "autocomplete", "Autocomplete", priority=True),
            Binding("ctrl+c", "cancel", "Cancel"),
            Binding("escape", "cancel", "Cancel"),
        ]

        # 2D row layout for arrow navigation
        ROW_LAYOUT = [
            ["from", "to"],
            ["filter", "workers", "overwrite", "rmsrc"],
        ]

        def __init__(
            self,
            all_volumes: list[str],
            from_p: str,
            to_p: str,
            filt: str,
            worker_count: int,
            overwrite: bool,
            rm_src: bool,
        ) -> None:
            super().__init__()
            self.all_volumes = all_volumes
            self.from_p = from_p
            self.to_p = to_p
            self.filt = filt
            self.worker_count = max(1, worker_count)
            self.overwrite = overwrite
            self.rm_src = rm_src
            self.result: tuple[str, str, str, int, bool, bool] | None = None
            self._prev_focus = None

        def get_system_commands(self, screen):
            # Fixed brand theme - drop the palette's "Theme" switcher.
            for cmd in super().get_system_commands(screen):
                if cmd.title != "Theme":
                    yield cmd

        def compose(self) -> ComposeResult:
            with Horizontal(id="app-header"):
                yield Static(APP_TITLE, id="hdr-title")
                yield Static(f"v{VERSION}", id="hdr-version")
            with Vertical(id="inputs"):
                with Horizontal(classes="row"):
                    with Vertical(classes="field-grow"):
                        yield Static(
                            f"[{PASTEL['label']}]FROM prefix  -  source prefix to match[/]",
                            classes="desc",
                        )
                        yield Input(
                            value=self.from_p,
                            placeholder="e.g. jupyterlab-",
                            id="from",
                            select_on_focus=False,
                        )
                    with Vertical(classes="field-grow"):
                        yield Static(
                            f"[{PASTEL['label']}]TO prefix  -  replacement destination prefix[/]",
                            classes="desc",
                        )
                        yield Input(
                            value=self.to_p,
                            placeholder="e.g. jupyterhub_jupyterlab_",
                            id="to",
                            select_on_focus=False,
                        )
                with Horizontal(classes="row"):
                    with Vertical(classes="field-grow"):
                        yield Static(
                            f"[{PASTEL['label']}]FILTER regex -  whole name (empty = all)[/]",
                            classes="desc",
                        )
                        yield Input(
                            value=self.filt,
                            placeholder="e.g. ^alice",
                            id="filter",
                            select_on_focus=False,
                        )
                    with Vertical(classes="field-small"):
                        yield Static(
                            f"[{PASTEL['label']}]WORKERS  -  parallel containers[/]",
                            classes="desc",
                        )
                        yield Input(
                            value=str(self.worker_count),
                            placeholder="3",
                            id="workers",
                            select_on_focus=False,
                        )
                    with Vertical(classes="field-tiny"):
                        yield Static(
                            f"[{PASTEL['label']}]OVERWRITE existing[/]",
                            classes="desc",
                        )
                        yield Checkbox(
                            "replace dst",
                            value=self.overwrite,
                            id="overwrite",
                        )
                    with Vertical(classes="field-tiny"):
                        yield Static(
                            f"[{PASTEL['label']}]REMOVE source[/]",
                            classes="desc",
                        )
                        yield Checkbox(
                            "rm src",
                            value=self.rm_src,
                            id="rmsrc",
                        )

            yield Static(id="status-bar")

            with Horizontal(id="panels"):
                with Vertical(classes="col left-col"):
                    yield Static("BEFORE  (source)", classes="col-title")
                    with VerticalScroll(classes="panel-body"):
                        yield Static(id="left-body")
                with Vertical(classes="col"):
                    yield Static("AFTER  (destination)", classes="col-title")
                    with VerticalScroll(classes="panel-body"):
                        yield Static(id="right-body")

            yield Static(id="warn-box")
            yield Footer()

        def on_mount(self) -> None:
            self.title = "Volume Migration Designer"
            self.sub_title = (
                "arrows navigate - Shift+Tab complete - "
                "Enter confirm - Esc cancel"
            )
            self._refresh()
            self.query_one("#from", Input).focus()

        def on_input_changed(self, event) -> None:
            if event.input.id == "from":
                self.from_p = event.value
            elif event.input.id == "to":
                self.to_p = event.value
            elif event.input.id == "filter":
                self.filt = event.value
            elif event.input.id == "workers":
                try:
                    n = int(event.value)
                    self.worker_count = max(1, n)
                except ValueError:
                    pass  # keep previous valid value; status line flags it
            self._refresh()

        def on_checkbox_changed(self, event) -> None:
            if event.checkbox.id == "overwrite":
                self.overwrite = bool(event.value)
                self._refresh()
            elif event.checkbox.id == "rmsrc":
                self.rm_src = bool(event.value)
                self._refresh()

        def on_input_submitted(self, event) -> None:
            # Pressing Enter inside any input confirms the form.
            self.action_confirm()

        def _row_col(self, widget_id: str) -> tuple[int, int]:
            for r, row in enumerate(self.ROW_LAYOUT):
                if widget_id in row:
                    return r, row.index(widget_id)
            return 0, 0

        def _focus_at(self, r: int, c: int) -> None:
            r %= len(self.ROW_LAYOUT)
            row = self.ROW_LAYOUT[r]
            c = min(max(0, c), len(row) - 1)
            w = self.query_one(f"#{row[c]}")
            w.focus()
            # No select-all on entry; drop the cursor at the end of the value.
            if isinstance(w, Input):
                w.cursor_position = len(w.value)

        def _focusable(self) -> bool:
            return isinstance(self.focused, (Input, Checkbox))

        def action_focus_up(self) -> None:
            if isinstance(self.focused, VerticalScroll):
                self.focused.scroll_up()
                return
            if not self._focusable():
                self._focus_at(0, 0)
                return
            r, c = self._row_col(self.focused.id)
            self._focus_at(r - 1, c)

        def action_focus_down(self) -> None:
            if isinstance(self.focused, VerticalScroll):
                self.focused.scroll_down()
                return
            if not self._focusable():
                self._focus_at(0, 0)
                return
            r, c = self._row_col(self.focused.id)
            self._focus_at(r + 1, c)

        def _focus_sibling(self, delta: int) -> None:
            if not self._focusable():
                return
            r, c = self._row_col(self.focused.id)
            row = self.ROW_LAYOUT[r]
            if len(row) <= 1:
                return
            self._focus_at(r, (c + delta) % len(row))

        async def on_key(self, event) -> None:
            # While the validation warning is up, any key just dismisses it.
            if self._warn_visible():
                self._hide_warn()
                event.prevent_default()
                event.stop()
                return
            if event.key not in ("left", "right"):
                return
            f = self.focused
            if isinstance(f, Checkbox):
                # Checkbox has no cursor - left/right always navigates siblings.
                self._focus_sibling(-1 if event.key == "left" else +1)
                event.prevent_default()
                event.stop()
                return
            if not isinstance(f, Input):
                return
            # Hand off left/right to row-sibling navigation when the cursor
            # is already at the input edge - otherwise let Input move the cursor.
            if event.key == "left" and f.cursor_position == 0:
                self._focus_sibling(-1)
                event.prevent_default()
                event.stop()
            elif event.key == "right" and f.cursor_position == len(f.value):
                self._focus_sibling(+1)
                event.prevent_default()
                event.stop()

        def _refresh(self) -> None:
            from_re = None
            from_ok = True
            if self.from_p:
                try:
                    from_re = re.compile(rf"^{re.escape(self.from_p)}(.+)$")
                except re.error:
                    from_ok = False

            filt_re = None
            filt_ok = True
            if self.filt:
                try:
                    filt_re = re.compile(self.filt)
                except re.error:
                    filt_ok = False

            left_lines: list[Text] = []
            right_lines: list[Text] = []
            total_candidates = 0

            for v in self.all_volumes:
                if not from_re:
                    continue
                m = from_re.match(v)
                if not m:
                    continue
                total_candidates += 1
                tail = m.group(1)
                # Filter matches against the WHOLE source name; non-matching rows
                # are hidden so the panels show only what will migrate.
                if filt_re and not filt_re.search(v):
                    continue

                dst_name = f"{self.to_p}{tail}" if self.to_p \
                    else f"<TO?>{tail}"
                left_lines.append(styled_volume(
                    v, self.from_p, FROM_STYLE, USER_STYLE, FILT_STYLE, filt_re,
                ))
                right_lines.append(styled_volume(
                    dst_name, self.to_p or "<TO?>", TO_STYLE, USER_STYLE,
                    FILT_STYLE, filt_re,
                ))

            # Status line at top of each panel
            status_bits: list[str] = []
            if not from_ok:
                status_bits.append(f"[{PASTEL['err']}]invalid FROM regex[/]")
            if not filt_ok:
                status_bits.append(f"[{PASTEL['err']}]invalid FILTER regex[/]")
            if from_re and total_candidates == 0:
                status_bits.append(f"[{PASTEL['warn']}]no candidate volumes match FROM[/]")
            elif from_re and not left_lines:
                status_bits.append(f"[{PASTEL['warn']}]no volumes match FILTER[/]")
            elif from_re and filt_re:
                status_bits.append(
                    f"[{PASTEL['ok']}]{len(left_lines)}[/] of "
                    f"[{PASTEL['info']}]{total_candidates}[/] candidates match filter"
                )
            elif from_re:
                status_bits.append(
                    f"[{PASTEL['info']}]{total_candidates}[/] candidates"
                )
            if not from_re:
                status_bits.append(
                    f"[{PASTEL['label']}]type a FROM prefix to start[/]"
                )
            status_bits.append(
                f"[{PASTEL['label']}]workers:[/] [{PASTEL['title']}]{self.worker_count}[/]"
            )
            ov_color = PASTEL["warn"] if self.overwrite else PASTEL["label"]
            ov_label = "ON" if self.overwrite else "off"
            status_bits.append(
                f"[{PASTEL['label']}]overwrite:[/] [{ov_color}]{ov_label}[/]"
            )
            rm_color = PASTEL["err"] if self.rm_src else PASTEL["label"]
            rm_label = "ON" if self.rm_src else "off"
            status_bits.append(
                f"[{PASTEL['label']}]rm-src:[/] [{rm_color}]{rm_label}[/]"
            )

            status = Text.from_markup("  ".join(status_bits) or " ")
            self.query_one("#status-bar", Static).update(status)

            no_rows = Text("(no rows)", style=f"italic {DIM_STYLE}")
            left_panel = Group(*left_lines) if left_lines else no_rows
            right_panel = Group(*right_lines) if right_lines else no_rows

            self.query_one("#left-body", Static).update(left_panel)
            self.query_one("#right-body", Static).update(right_panel)

        def action_autocomplete(self) -> None:
            # Shift+Tab: fish-style one-segment complete on FROM / TO.
            if self._warn_visible():
                self._hide_warn()
                return
            f = self.focused
            if not isinstance(f, Input) or f.id not in ("from", "to"):
                return
            completed = autocomplete_prefix(f.value, self.all_volumes)
            if completed != f.value:
                f.value = completed              # fires on_input_changed
                f.cursor_position = len(completed)

        def _warn_visible(self) -> bool:
            return self.query_one("#warn-box", Static).has_class("-show")

        def _show_warn(self, markup: str) -> None:
            box = self.query_one("#warn-box", Static)
            box.update(Text.from_markup(markup))
            box.add_class("-show")
            # Blur the input so the next keypress reaches App.on_key (a focused
            # Input would otherwise swallow printable keys) - any key dismisses.
            self._prev_focus = self.focused
            self.set_focus(None)

        def _hide_warn(self) -> None:
            self.query_one("#warn-box", Static).remove_class("-show")
            if self._prev_focus is not None:
                self.set_focus(self._prev_focus)
                self._prev_focus = None

        def _validate(self) -> tuple[int, int]:
            """Count (src==dst collisions, pre-existing destinations) for the
            current FROM/TO/filter over the volume pool."""
            if not self.from_p or not self.to_p:
                return 0, 0
            try:
                from_re = re.compile(rf"^{re.escape(self.from_p)}(.+)$")
            except re.error:
                return 0, 0
            filt_re = None
            if self.filt:
                try:
                    filt_re = re.compile(self.filt)
                except re.error:
                    filt_re = None
            pool = set(self.all_volumes)
            collisions = existing = 0
            for v in self.all_volumes:
                m = from_re.match(v)
                if not m:
                    continue
                if filt_re and not filt_re.search(v):
                    continue
                dst = self.to_p + m.group(1)
                if dst == v:
                    collisions += 1
                elif dst in pool:
                    existing += 1
            return collisions, existing

        def action_confirm(self) -> None:
            if self._warn_visible():
                self._hide_warn()
                return
            collisions, existing = self._validate()
            if collisions:
                self._show_warn(
                    f"[bold {PASTEL['err']}]Cannot continue[/]\n\n"
                    f"[{PASTEL['warn']}]{collisions}[/] volume(s) would map to "
                    f"the same name - FROM and TO\nproduce identical names. "
                    f"This is not allowed;\nchange FROM or TO.\n\n"
                    f"[{PASTEL['label']}]press any key to dismiss[/]"
                )
                return
            if existing and not self.overwrite:
                self._show_warn(
                    f"[bold {PASTEL['err']}]Cannot continue[/]\n\n"
                    f"[{PASTEL['warn']}]{existing}[/] destination volume(s) "
                    f"already exist and\nOVERWRITE is off. Enable OVERWRITE to "
                    f"clean\nand replace, or change TO.\n\n"
                    f"[{PASTEL['label']}]press any key to dismiss[/]"
                )
                return
            self.result = (
                self.from_p, self.to_p, self.filt,
                self.worker_count, self.overwrite, self.rm_src,
            )
            self.exit()

        def action_cancel(self) -> None:
            self.result = None
            self.exit()

    app = DesignerApp(
        candidates, from_p, to_p, filt, worker_count, overwrite, rm_src,
    )
    app.run()
    return app.result


# ===========================================================================
# Interactive planner (Textual TUI)
# ===========================================================================


def run_planner(
    migs: list[Migration],
    worker_count: int,
    dry_run: bool,
    overwrite: bool,
    existing_dsts: set[str],
    from_prefix: str,
    to_prefix: str,
    filt_re: re.Pattern | None,
    rm_src: bool,
) -> tuple[str, list[int]] | None:
    """Show the migration plan; return ('confirm', indices) or ('edit', []) or
    ('cancel', []) or None when the window is closed."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal
    from textual.widgets import Footer, OptionList, Static
    from textual.widgets.option_list import Option

    class PlannerApp(App):
        # SelectionList clamps options to a single line (Textual 8.2.7), which
        # hid wrapped targets; OptionList renders multi-line options, so we use
        # it with a self-managed selection set and a custom orange-X marker.
        CSS = f"""
        Screen {{ layout: vertical; background: {DUO['bg_dim']}; }}
{HEADER_CSS}
        #header {{
            height: auto;
            padding: 1 2;
            background: {DUO['bg_subtle']};
            border-bottom: heavy {DUO['border']};
            color: {DUO['text']};
        }}

        OptionList {{
            background: {DUO['bg_dim']};
            color: {DUO['text']};
            padding: 0 2;
            border: none;
            height: 1fr;
        }}
        OptionList > .option-list--option {{ text-wrap: nowrap; }}
        /* Subtle current-row highlight - override Textual's bright $block-cursor
           background (blurred and focused) so the target text stays legible. */
        OptionList > .option-list--option-highlighted {{
            background: {DUO['surface']};
            color: {DUO['text']};
            text-style: none;
        }}
        OptionList:focus > .option-list--option-highlighted {{
            background: {DUO['surface']};
            color: {DUO['text']};
            text-style: none;
        }}

        Footer {{ background: {DUO['bg_subtle']}; color: {DUO['text_muted']}; }}
        """

        BINDINGS = [
            Binding("space", "toggle", "Toggle", priority=True),
            Binding("enter", "confirm", "Run selected", priority=True),
            Binding("ctrl+s", "confirm", "Run selected", show=False),
            Binding("e", "edit", "Edit (back)"),
            Binding("a", "select_all", "All"),
            Binding("n", "select_none", "None"),
            Binding("ctrl+c", "cancel", "Cancel"),
            Binding("escape", "cancel", "Cancel"),
        ]

        def __init__(
            self,
            migs: list[Migration],
            worker_count: int,
            dry_run: bool,
            overwrite: bool,
            existing_dsts: set[str],
            from_prefix: str,
            to_prefix: str,
            filt_re: re.Pattern | None,
            rm_src: bool,
        ) -> None:
            super().__init__()
            self.migs = migs
            self.worker_count = worker_count
            self.dry_run = dry_run
            self.overwrite = overwrite
            self.existing_dsts = existing_dsts
            self.from_prefix = from_prefix
            self.to_prefix = to_prefix
            self.filt_re = filt_re
            self.rm_src = rm_src
            self.conflicts = [
                i for i, m in enumerate(migs) if m.dst in existing_dsts
            ]
            self.selected: set[int] = set(range(len(migs)))
            self.result: tuple[str, list[int]] | None = None

        def get_system_commands(self, screen):
            # Fixed brand theme - drop the palette's "Theme" switcher.
            for cmd in super().get_system_commands(screen):
                if cmd.title != "Theme":
                    yield cmd

        def _styled_name(self, full: str, prefix: str, prefix_style: str) -> Text:
            """Render full volume name with prefix colored and every
            filter-matched span (anywhere in the name) highlighted."""
            return styled_volume(
                full, prefix, f"bold {prefix_style}", PASTEL["user"],
                f"bold {PASTEL['filter']}", self.filt_re,
            )

        def _prompt(self, i: int) -> Text:
            """Build the (possibly two-line) row content for migration i."""
            m = self.migs[i]
            selected = i in self.selected
            # 2-cell marker gutter: orange 'X ' when selected, blank otherwise
            line = Text()
            line.append("X " if selected else "  ",
                        style=f"bold {DUO['orange']}")
            if m.dst in self.existing_dsts:
                line.append("[exists] ", style=f"bold {DUO['amber']}")
            src = self._styled_name(m.src, self.from_prefix, PASTEL["from"])
            dst = self._styled_name(m.dst, self.to_prefix, PASTEL["to"])
            line.append_text(src)
            sep = "  ->  "
            width = self.size.width or 100
            # padding (2+2) + marker (2) already in line; small slack
            avail = max(40, width - 6)
            inline_len = 2 + line.cell_len - 2 + len(sep) + dst.cell_len
            if inline_len <= avail:
                line.append(sep, style=PASTEL["label"])
                line.append_text(dst)
            else:
                # target on its own line, indented under the source name
                line.append("\n      ->  ", style=PASTEL["label"])
                line.append_text(dst)
            return line

        def _rebuild(self) -> None:
            ol = self.query_one(OptionList)
            hi = ol.highlighted
            ol.clear_options()
            ol.add_options(
                [Option(self._prompt(i), id=str(i)) for i in range(len(self.migs))]
            )
            if self.migs:
                ol.highlighted = 0 if hi is None else min(hi, len(self.migs) - 1)

        def compose(self) -> ComposeResult:
            mode_label = "DRY-RUN" if self.dry_run else "MIGRATION"
            ov_label = "ON (clean+replace)" if self.overwrite else "off (error if exists)"
            ov_color = PASTEL["warn"] if self.overwrite else PASTEL["label"]
            rm_label = "ON (delete src)" if self.rm_src else "off (keep src)"
            rm_color = PASTEL["err"] if self.rm_src else PASTEL["label"]
            header_lines = [
                f"[{PASTEL['title']}]MIGRATION PLAN[/]   "
                f"[{PASTEL['label']}]mode:[/] [{PASTEL['warn']}]{mode_label}[/]   "
                f"[{PASTEL['label']}]workers:[/] [{PASTEL['title']}]{self.worker_count}[/]   "
                f"[{PASTEL['label']}]volumes:[/] [{PASTEL['ok']}]{len(self.migs)}[/]   "
                f"[{PASTEL['label']}]overwrite:[/] [{ov_color}]{ov_label}[/]   "
                f"[{PASTEL['label']}]rm-src:[/] [{rm_color}]{rm_label}[/]"
            ]
            if self.rm_src:
                header_lines.append(
                    f"[{PASTEL['err']}]WARNING:[/] source volumes will be "
                    f"[bold {PASTEL['err']}]DELETED[/] after each successful copy."
                )
            if self.conflicts:
                if self.overwrite:
                    header_lines.append(
                        f"[{PASTEL['err']}]WARNING:[/] "
                        f"[{PASTEL['warn']}]{len(self.conflicts)}[/] "
                        f"destination volume(s) already exist - contents will be "
                        f"[bold {PASTEL['warn']}]CLEANED and REPLACED[/] in place "
                        f"(volume kept)."
                    )
                else:
                    header_lines.append(
                        f"[{PASTEL['err']}]ERROR:[/] "
                        f"[{PASTEL['err']}]{len(self.conflicts)}[/] "
                        f"destination volume(s) already exist - migration will "
                        f"[bold {PASTEL['err']}]ABORT[/] "
                        f"(toggle OVERWRITE in designer to clean and replace)."
                    )
            header_lines.append(
                "[dim]Space toggles row   a = all   n = none   "
                "e = edit (back to designer)   "
                "Enter = run selected   Esc = cancel[/]"
            )
            with Horizontal(id="app-header"):
                yield Static(APP_TITLE, id="hdr-title")
                yield Static(f"v{VERSION}", id="hdr-version")
            yield Static(
                Text.from_markup("\n".join(header_lines)),
                id="header",
            )
            yield OptionList(id="plan")
            yield Footer()

        def on_mount(self) -> None:
            self.title = "Volume Migration Plan"
            self.sub_title = (
                "review selection - Enter to run, 'e' to edit, Esc to cancel"
            )
            self._rebuild()
            self.query_one(OptionList).focus()

        def on_resize(self, event) -> None:
            # Width changed - inline-vs-wrapped decisions may flip; rebuild rows.
            self._rebuild()

        def action_toggle(self) -> None:
            ol = self.query_one(OptionList)
            i = ol.highlighted
            if i is None:
                return
            if i in self.selected:
                self.selected.discard(i)
            else:
                self.selected.add(i)
            ol.replace_option_prompt_at_index(i, self._prompt(i))

        def action_select_all(self) -> None:
            self.selected = set(range(len(self.migs)))
            self._rebuild()

        def action_select_none(self) -> None:
            self.selected = set()
            self._rebuild()

        def action_confirm(self) -> None:
            self.result = ("confirm", sorted(self.selected))
            self.exit()

        def action_edit(self) -> None:
            self.result = ("edit", [])
            self.exit()

        def action_cancel(self) -> None:
            self.result = ("cancel", [])
            self.exit()

    app = PlannerApp(
        migs, worker_count, dry_run, overwrite, existing_dsts,
        from_prefix, to_prefix, filt_re, rm_src,
    )
    app.run()
    return app.result


def run_execution(
    migs: list[Migration],
    worker_count: int,
    dry_run: bool,
    rm_src: bool,
) -> None:
    """Run the migrations on a managed (alt-screen) Textual screen.

    A sticky OVERALL bar sits above a scrolling list of per-volume rows; as
    rows finish, the view auto-scrolls to keep the running/queued frontier in
    sight. Worker threads push progress in via call_from_thread. `migs` is
    mutated in place (success / removed / error); the caller prints the
    summary once the screen is dismissed."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import Footer, ProgressBar, Static

    LABEL_W = 46
    STAGE_W = 11
    title_word = "Verifying (dry-run)" if dry_run else "Migrating"

    class MigRow(Horizontal):
        def __init__(self, idx: int, m: Migration) -> None:
            super().__init__(id=f"row-{idx}", classes="mig-row queued")
            self.m = m

        def compose(self) -> ComposeResult:
            yield Static(trunc_left(self.m.src, LABEL_W), classes="vol-name")
            yield Static(Text("queued", style=DUO["text_subtle"]), classes="stage")
            yield ProgressBar(
                total=1, show_eta=False, show_percentage=not dry_run, id="bar",
            )

        def state(self, cls: str) -> None:
            self.remove_class("queued", "running", "done", "fail")
            self.add_class(cls)

        def set_stage(self, text: str, color: str) -> None:
            self.query_one(".stage", Static).update(Text(text, style=color))

    class MigrationApp(App):
        CSS = f"""
        Screen {{ background: {DUO['bg_dim']}; layers: base overlay; align: center middle; }}
{HEADER_CSS}
        #overall-row {{
            height: auto; padding: 0 2;
            background: {DUO['bg_subtle']};
            border-bottom: heavy {DUO['border']};
        }}
        #overall-name {{ width: {LABEL_W + 2 + STAGE_W}; color: {DUO['cyan']}; text-style: bold; }}

        #rows {{ height: 1fr; padding: 0 2; background: {DUO['bg']}; }}
        .mig-row {{ height: 1; }}
        .vol-name {{ width: {LABEL_W + 2}; }}
        .stage {{ width: {STAGE_W}; }}
        .mig-row.queued .vol-name {{ color: {DUO['text_subtle']}; }}
        .mig-row.running .vol-name {{ color: {DUO['cyan']}; }}
        .mig-row.done .vol-name {{ color: {DUO['mint']}; }}
        .mig-row.fail .vol-name {{ color: {DUO['rose']}; }}

        ProgressBar {{ width: 1fr; height: 1; }}
        Bar {{ width: 1fr; }}
        /* migration = determinate cyan; discovery = indeterminate orange pulse;
           done = mint; fail = rose. Overall bar is the standard cyan. */
        Bar > .bar--bar {{ color: {DUO['cyan']}; background: {DUO['surface']}; }}
        Bar > .bar--indeterminate {{ color: {DUO['orange']}; background: {DUO['surface']}; }}
        Bar > .bar--complete {{ color: {DUO['mint']}; background: {DUO['surface']}; }}
        .mig-row.fail Bar > .bar--bar {{ color: {DUO['rose']}; }}
        .mig-row.fail Bar > .bar--indeterminate {{ color: {DUO['rose']}; }}
        .mig-row.fail Bar > .bar--complete {{ color: {DUO['rose']}; }}
        #overall-row Bar > .bar--bar {{ color: {DUO['cyan']}; background: {DUO['surface']}; }}
        #overall-row Bar > .bar--complete {{ color: {DUO['mint']}; background: {DUO['surface']}; }}

        /* Completion popup: a content-sized box on the overlay layer, centered
           by the Screen's align. Only its own footprint covers the rows - the
           execution screen stays visible behind it (a full-size overlay
           container would paint over everything, even when transparent). */
        #banner-box {{
            layer: overlay;
            display: none;
            width: auto; height: auto;
            padding: 1 4;
            background: {DUO['bg_subtle']};
            border: heavy {DUO['cyan']};
            text-align: center;
        }}
        #banner-box.-show {{ display: block; }}

        Footer {{ background: {DUO['bg_subtle']}; color: {DUO['text_muted']}; }}
        """

        BINDINGS = [Binding("ctrl+c", "quit", "Cancel")]

        def __init__(self) -> None:
            super().__init__()
            self.migs = migs
            self.rows: dict[str, MigRow] = {}
            self.completed = 0
            self.finished = False
            self.start_time = 0.0
            self._pool: ThreadPoolExecutor | None = None

        def get_system_commands(self, screen):
            # Fixed brand theme - drop the palette's "Theme" switcher.
            for cmd in super().get_system_commands(screen):
                if cmd.title != "Theme":
                    yield cmd

        def compose(self) -> ComposeResult:
            with Horizontal(id="app-header"):
                yield Static(
                    f"{APP_TITLE}   "
                    f"[{PASTEL['label']}]{title_word} · {len(self.migs)} volumes · "
                    f"{worker_count} workers[/]",
                    id="hdr-title",
                )
                yield Static(f"v{VERSION}", id="hdr-version")
            with Horizontal(id="overall-row"):
                yield Static("OVERALL", id="overall-name")
                yield ProgressBar(
                    total=len(self.migs), show_eta=False, show_percentage=True,
                    id="overall-bar",
                )
            with VerticalScroll(id="rows"):
                for i, m in enumerate(self.migs):
                    row = MigRow(i, m)
                    self.rows[m.label] = row
                    yield row
            yield Static(id="banner-box")
            yield Footer()

        def on_mount(self) -> None:
            self.title = "Volume Migration"
            self.start_time = time.monotonic()
            self._pool = ThreadPoolExecutor(max_workers=worker_count)
            for m in self.migs:
                self._pool.submit(self._do_one, m)

        # --- worker thread side -------------------------------------------
        def _do_one(self, m: Migration) -> None:
            self.call_from_thread(self._start_row, m)
            if dry_run:
                m.success = run_dry(m)
            else:
                m.success = run_copy(
                    m,
                    lambda done, total: self.call_from_thread(
                        self._update_bar, m, done, total
                    ),
                    lambda stage: self.call_from_thread(self._set_stage, m, stage),
                )
                if m.success and rm_src:
                    ok, err = remove_volume(m.src)
                    m.removed = ok
                    if not ok:
                        m.error = f"copied ok, source removal failed: {err}"
            self.call_from_thread(self._complete_row, m)

        # --- UI thread side -----------------------------------------------
        def _start_row(self, m: Migration) -> None:
            row = self.rows[m.label]
            row.state("running")
            if dry_run:
                row.set_stage("verify", DUO["cyan"])
            else:
                # Discovery: orange, indeterminate pulse until first byte.
                row.set_stage("discovery", DUO["orange"])
                row.query_one("#bar", ProgressBar).update(total=None)
            row.scroll_visible()

        def _set_stage(self, m: Migration, stage: str) -> None:
            row = self.rows[m.label]
            if stage == "discovery":
                row.set_stage("discovery", DUO["orange"])
                row.query_one("#bar", ProgressBar).update(total=None)
            elif stage == "migration":
                row.set_stage("migration", DUO["cyan"])

        def _update_bar(self, m: Migration, done: int, total: int) -> None:
            if total > 0:
                self.rows[m.label].query_one("#bar", ProgressBar).update(
                    total=total, progress=done,
                )

        def _complete_row(self, m: Migration) -> None:
            row = self.rows[m.label]
            bar = row.query_one("#bar", ProgressBar)
            if m.success:
                if bar.total is None:
                    bar.update(total=1)
                bar.update(progress=bar.total or 1)
                row.state("done")
                row.set_stage("done", DUO["mint"])
            else:
                # Stop any discovery pulse so a failed row reads as stalled.
                if bar.total is None:
                    bar.update(total=1, progress=0)
                row.state("fail")
                row.set_stage("failed", DUO["rose"])
            self.query_one("#overall-bar", ProgressBar).advance(1)
            self.completed += 1
            if self.completed >= len(self.migs):
                self._finish()
            else:
                self._scroll_frontier()

        def _scroll_frontier(self) -> None:
            for m in self.migs:
                row = self.rows[m.label]
                if row.has_class("running") or row.has_class("queued"):
                    row.scroll_visible()
                    return

        def _finish(self) -> None:
            self.finished = True
            elapsed = time.monotonic() - self.start_time
            ok = sum(1 for x in self.migs if x.success)
            fails = len(self.migs) - ok
            verb = "Verification" if dry_run else "Migration"
            state, color = ("complete", DUO["mint"]) if fails == 0 else ("failed", DUO["rose"])
            fail_color = DUO["rose"] if fails else DUO["text_muted"]
            lines = [
                f"[bold {color}]{verb} {state}[/]",
                "",
                f"[{DUO['mint']}]{ok}[/] ok    [{fail_color}]{fails}[/] failed",
                f"[{DUO['text_muted']}]elapsed[/] "
                f"[{DUO['cyan']}]{fmt_duration(elapsed)}[/]",
                "",
                f"[{DUO['text_subtle']}]press any key to close[/]",
            ]
            box = self.query_one("#banner-box", Static)
            box.update(Text.from_markup("\n".join(lines)))
            box.styles.border = ("heavy", color)
            box.add_class("-show")
            self.sub_title = f"done - {ok}/{len(self.migs)} ok · press any key"

        def on_key(self, event) -> None:
            if self.finished:
                self.exit()

    app = MigrationApp()
    app.run()
    if app._pool is not None:
        app._pool.shutdown(wait=False)


# ===========================================================================
# Action menu (first screen)
# ===========================================================================


def make_menu_app(dry_run: bool):
    """Build the action-menu App. Factory seam so the screen can be rendered
    headlessly via run_test. .run() returns one of 'migrate'|'archive'|
    'delete'|None via app.result."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal
    from textual.widgets import Footer, OptionList, Static
    from textual.widgets.option_list import Option

    ACTIONS = [
        ("migrate", "Migrate volumes",
         "copy volumes from one name prefix to another"),
        ("archive", "Archive volumes",
         "tar.gz selected volumes into the current directory"),
        ("delete", "Delete volumes",
         "permanently remove selected volumes"),
    ]

    class MenuApp(App):
        CSS = f"""
        Screen {{ layout: vertical; background: {DUO['bg_dim']}; }}
{HEADER_CSS}
        #header {{
            height: auto;
            padding: 1 2;
            background: {DUO['bg_subtle']};
            border-bottom: heavy {DUO['border']};
            color: {DUO['text']};
        }}

        OptionList {{
            background: {DUO['bg_dim']};
            color: {DUO['text']};
            padding: 1 2;
            border: none;
            height: 1fr;
        }}
        OptionList > .option-list--option {{ text-wrap: nowrap; }}
        OptionList > .option-list--option-highlighted {{
            background: {DUO['surface']};
            color: {DUO['text']};
            text-style: none;
        }}
        OptionList:focus > .option-list--option-highlighted {{
            background: {DUO['surface']};
            color: {DUO['text']};
            text-style: none;
        }}

        Footer {{ background: {DUO['bg_subtle']}; color: {DUO['text_muted']}; }}
        """

        BINDINGS = [
            Binding("enter", "choose", "Select", priority=True),
            Binding("space", "choose", "Select", priority=True),
            Binding("ctrl+c", "cancel", "Cancel"),
            Binding("escape", "cancel", "Cancel"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.dry_run = dry_run
            self.result: str | None = None

        def get_system_commands(self, screen):
            for cmd in super().get_system_commands(screen):
                if cmd.title != "Theme":
                    yield cmd

        def _row(self, label: str, desc: str) -> Text:
            line = Text()
            line.append(label, style=f"bold {PASTEL['title']}")
            line.append("   ")
            line.append(desc, style=PASTEL["label"])
            return line

        def compose(self) -> ComposeResult:
            mode = (f"   [{PASTEL['warn']}]DRY-RUN[/] "
                    f"[{PASTEL['label']}]- no destructive side effects[/]"
                    if self.dry_run else "")
            with Horizontal(id="app-header"):
                yield Static(APP_TITLE, id="hdr-title")
                yield Static(f"v{VERSION}", id="hdr-version")
            yield Static(
                Text.from_markup(
                    f"[{PASTEL['title']}]SELECT AN ACTION[/]{mode}\n"
                    f"[dim]arrows navigate   Enter / Space select   "
                    f"Esc cancel[/]"
                ),
                id="header",
            )
            ol = OptionList(id="menu")
            yield ol
            yield Footer()

        def on_mount(self) -> None:
            self.title = "Docker Volume Toolkit"
            self.sub_title = (
                "DRY-RUN - choose an action" if self.dry_run
                else "choose an action"
            )
            ol = self.query_one(OptionList)
            ol.add_options(
                [Option(self._row(lbl, desc), id=key)
                 for key, lbl, desc in ACTIONS]
            )
            ol.highlighted = 0
            ol.focus()

        def on_option_list_option_selected(self, event) -> None:
            self.result = event.option.id
            self.exit()

        def action_choose(self) -> None:
            ol = self.query_one(OptionList)
            i = ol.highlighted
            if i is None:
                return
            self.result = ACTIONS[i][0]
            self.exit()

        def action_cancel(self) -> None:
            self.result = None
            self.exit()

    return MenuApp()


def run_menu(dry_run: bool) -> str | None:
    """First screen: pick migrate / archive / delete. Returns the action key
    or None when cancelled."""
    app = make_menu_app(dry_run)
    app.run()
    return app.result


# ===========================================================================
# Archive designer (Textual TUI)
# ===========================================================================


def make_archive_designer_app(
    candidates: list[str],
    filt: str,
    workers: int,
    template: str,
):
    """Build the archive-designer App. Returns (filt, workers, template) via
    app.result on confirm, else None."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Footer, Input, Static

    SEL_STYLE = f"black on {PASTEL['filter']}"
    USER_STYLE = PASTEL["user"]
    DIM_STYLE = PASTEL["dim"]

    class ArchiveDesignerApp(App):
        CSS = f"""
        Screen {{ layout: vertical; background: {DUO['bg_dim']}; layers: base overlay; align: center middle; }}
{HEADER_CSS}
        #warn-box {{
            layer: overlay;
            display: none;
            width: auto; height: auto;
            max-width: 80%;
            padding: 1 4;
            background: {DUO['bg_subtle']};
            border: heavy {DUO['rose']};
            text-align: center;
        }}
        #warn-box.-show {{ display: block; }}
        #inputs {{
            height: auto;
            padding: 1 2 0 2;
            background: {DUO['bg_subtle']};
        }}
        #status-bar {{
            height: auto;
            padding: 0 2;
            background: {DUO['bg_subtle']};
            border-bottom: heavy {DUO['border']};
        }}
        .desc {{ color: {DUO['text_muted']}; height: 1; }}
        Input {{
            margin: 0 0 1 0;
            background: {DUO['surface']};
            border: round {DUO['border']};
            color: {DUO['text']};
        }}
        Input:focus {{ border: round {DUO['cyan']}; }}

        .row {{ height: auto; layout: horizontal; }}
        .field-grow {{ width: 1fr; height: auto; padding-right: 1; }}
        .field-small {{ width: 28; height: auto; }}

        #panels {{ height: 1fr; background: {DUO['bg']}; }}
        .col {{ width: 1fr; padding: 0 1; }}
        .left-col {{ border-right: solid {DUO['border']}; }}
        .panel-body {{ border: round {DUO['bg']}; }}
        .panel-body:focus {{ border: round {DUO['cyan']}; }}
        .col-title {{
            background: {DUO['surface']};
            color: {DUO['cyan_bright']};
            padding: 0 1;
            text-style: bold;
            text-align: center;
            height: 1;
        }}

        Footer {{ background: {DUO['bg_subtle']}; color: {DUO['text_muted']}; }}
        """

        BINDINGS = [
            Binding("up", "focus_up", "Up", show=False),
            Binding("down", "focus_down", "Down", show=False),
            Binding("enter", "confirm", "Confirm", priority=True),
            Binding("ctrl+s", "confirm", "Confirm", show=False),
            Binding("shift+tab", "autocomplete", "Autocomplete", priority=True),
            Binding("ctrl+c", "cancel", "Cancel"),
            Binding("escape", "cancel", "Cancel"),
        ]

        ROW_LAYOUT = [
            ["select", "workers"],
            ["template"],
        ]

        def __init__(self) -> None:
            super().__init__()
            self.all_volumes = candidates
            self.filt = filt
            self.worker_count = max(1, workers)
            self.template = template
            self.when = datetime.now()
            self.result: tuple[str, int, str] | None = None
            self._prev_focus = None

        def get_system_commands(self, screen):
            for cmd in super().get_system_commands(screen):
                if cmd.title != "Theme":
                    yield cmd

        def compose(self) -> ComposeResult:
            with Horizontal(id="app-header"):
                yield Static(APP_TITLE, id="hdr-title")
                yield Static(f"v{VERSION}", id="hdr-version")
            with Vertical(id="inputs"):
                with Horizontal(classes="row"):
                    with Vertical(classes="field-grow"):
                        yield Static(
                            f"[{PASTEL['label']}]SELECT regex - whole name "
                            f"(empty = all)[/]",
                            classes="desc",
                        )
                        yield Input(
                            value=self.filt,
                            placeholder="e.g. ^alice",
                            id="select",
                            select_on_focus=False,
                        )
                    with Vertical(classes="field-small"):
                        yield Static(
                            f"[{PASTEL['label']}]WORKERS - parallel containers[/]",
                            classes="desc",
                        )
                        yield Input(
                            value=str(self.worker_count),
                            placeholder="3",
                            id="workers",
                            select_on_focus=False,
                        )
                with Horizontal(classes="row"):
                    with Vertical(classes="field-grow"):
                        yield Static(
                            f"[{PASTEL['label']}]TEMPLATE - filename "
                            f"({{name}} {{date}} {{datetime}})[/]",
                            classes="desc",
                        )
                        yield Input(
                            value=self.template,
                            placeholder="{name}-{date}.tar.gz",
                            id="template",
                            select_on_focus=False,
                        )

            yield Static(id="status-bar")

            with Horizontal(id="panels"):
                with Vertical(classes="col left-col"):
                    yield Static("MATCHED  (volumes)", classes="col-title")
                    with VerticalScroll(classes="panel-body"):
                        yield Static(id="left-body")
                with Vertical(classes="col"):
                    yield Static("ARCHIVE  (filename)", classes="col-title")
                    with VerticalScroll(classes="panel-body"):
                        yield Static(id="right-body")

            yield Static(id="warn-box")
            yield Footer()

        def on_mount(self) -> None:
            self.title = "Volume Archive Designer"
            self.sub_title = (
                "arrows navigate - Shift+Tab complete - "
                "Enter confirm - Esc cancel"
            )
            self._refresh()
            self.query_one("#select", Input).focus()

        def on_input_changed(self, event) -> None:
            if event.input.id == "select":
                self.filt = event.value
            elif event.input.id == "template":
                self.template = event.value
            elif event.input.id == "workers":
                try:
                    self.worker_count = max(1, int(event.value))
                except ValueError:
                    pass  # keep last valid; status flags it
            self._refresh()

        def on_input_submitted(self, event) -> None:
            self.action_confirm()

        def _row_col(self, widget_id: str) -> tuple[int, int]:
            for r, row in enumerate(self.ROW_LAYOUT):
                if widget_id in row:
                    return r, row.index(widget_id)
            return 0, 0

        def _focus_at(self, r: int, c: int) -> None:
            r %= len(self.ROW_LAYOUT)
            row = self.ROW_LAYOUT[r]
            c = min(max(0, c), len(row) - 1)
            w = self.query_one(f"#{row[c]}")
            w.focus()
            if isinstance(w, Input):
                w.cursor_position = len(w.value)

        def _focusable(self) -> bool:
            return isinstance(self.focused, Input)

        def action_focus_up(self) -> None:
            if isinstance(self.focused, VerticalScroll):
                self.focused.scroll_up()
                return
            if not self._focusable():
                self._focus_at(0, 0)
                return
            r, c = self._row_col(self.focused.id)
            self._focus_at(r - 1, c)

        def action_focus_down(self) -> None:
            if isinstance(self.focused, VerticalScroll):
                self.focused.scroll_down()
                return
            if not self._focusable():
                self._focus_at(0, 0)
                return
            r, c = self._row_col(self.focused.id)
            self._focus_at(r + 1, c)

        def _focus_sibling(self, delta: int) -> None:
            if not self._focusable():
                return
            r, c = self._row_col(self.focused.id)
            row = self.ROW_LAYOUT[r]
            if len(row) <= 1:
                return
            self._focus_at(r, (c + delta) % len(row))

        async def on_key(self, event) -> None:
            if self._warn_visible():
                self._hide_warn()
                event.prevent_default()
                event.stop()
                return
            if event.key not in ("left", "right"):
                return
            f = self.focused
            if not isinstance(f, Input):
                return
            if event.key == "left" and f.cursor_position == 0:
                self._focus_sibling(-1)
                event.prevent_default()
                event.stop()
            elif event.key == "right" and f.cursor_position == len(f.value):
                self._focus_sibling(+1)
                event.prevent_default()
                event.stop()

        def _matched(self) -> tuple[list[str], object, bool]:
            """Return (matched volume names, filt_re or None, filt_ok)."""
            filt_re = None
            filt_ok = True
            if self.filt:
                try:
                    filt_re = re.compile(self.filt)
                except re.error:
                    filt_ok = False
            matched = []
            for v in self.all_volumes:
                if filt_re and not filt_re.search(v):
                    continue
                matched.append(v)
            return matched, filt_re, filt_ok

        def _refresh(self) -> None:
            matched, filt_re, filt_ok = self._matched()
            total = len(self.all_volumes)

            left_lines: list[Text] = []
            right_lines: list[Text] = []
            for v in matched:
                left_lines.append(styled_volume(
                    v, "", USER_STYLE, USER_STYLE, SEL_STYLE, filt_re,
                ))
                tgt = render_template(v, self.template, self.when) \
                    if self.template else "<empty template>"
                right_lines.append(Text(tgt, style=USER_STYLE))

            status_bits: list[str] = []
            if not filt_ok:
                status_bits.append(f"[{PASTEL['err']}]invalid SELECT regex[/]")
            else:
                status_bits.append(
                    f"[{PASTEL['ok']}]{len(matched)}[/] of "
                    f"[{PASTEL['info']}]{total}[/] volumes match"
                )
            status_bits.append(
                f"[{PASTEL['label']}]workers:[/] "
                f"[{PASTEL['title']}]{self.worker_count}[/]"
            )
            status = Text.from_markup("  ".join(status_bits) or " ")
            self.query_one("#status-bar", Static).update(status)

            no_rows = Text("(no rows)", style=f"italic {DIM_STYLE}")
            self.query_one("#left-body", Static).update(
                Group(*left_lines) if left_lines else no_rows)
            self.query_one("#right-body", Static).update(
                Group(*right_lines) if right_lines else no_rows)

        def action_autocomplete(self) -> None:
            if self._warn_visible():
                self._hide_warn()
                return
            f = self.focused
            if not isinstance(f, Input) or f.id != "select":
                return
            completed = autocomplete_prefix(f.value, self.all_volumes)
            if completed != f.value:
                f.value = completed
                f.cursor_position = len(completed)

        def _warn_visible(self) -> bool:
            return self.query_one("#warn-box", Static).has_class("-show")

        def _show_warn(self, markup: str) -> None:
            box = self.query_one("#warn-box", Static)
            box.update(Text.from_markup(markup))
            box.add_class("-show")
            self._prev_focus = self.focused
            self.set_focus(None)

        def _hide_warn(self) -> None:
            self.query_one("#warn-box", Static).remove_class("-show")
            if self._prev_focus is not None:
                self.set_focus(self._prev_focus)
                self._prev_focus = None

        def action_confirm(self) -> None:
            if self._warn_visible():
                self._hide_warn()
                return
            matched, _, filt_ok = self._matched()
            if not filt_ok:
                return
            if not self.template.strip():
                self._show_warn(
                    f"[bold {PASTEL['err']}]Cannot continue[/]\n\n"
                    f"The TEMPLATE is empty.\n"
                    f"Enter a filename template.\n\n"
                    f"[{PASTEL['label']}]press any key to dismiss[/]"
                )
                return
            rendered = [render_template(v, self.template, self.when)
                        for v in matched]
            empties = sum(1 for r in rendered if not r)
            if empties:
                self._show_warn(
                    f"[bold {PASTEL['err']}]Cannot continue[/]\n\n"
                    f"[{PASTEL['warn']}]{empties}[/] archive name(s) render to "
                    f"an empty\nfilename. Adjust the TEMPLATE.\n\n"
                    f"[{PASTEL['label']}]press any key to dismiss[/]"
                )
                return
            dupes = len(rendered) - len(set(rendered))
            if dupes:
                self._show_warn(
                    f"[bold {PASTEL['err']}]Cannot continue[/]\n\n"
                    f"[{PASTEL['warn']}]{dupes}[/] duplicate archive "
                    f"filename(s)\nacross the matched set. Add {{name}} to the "
                    f"\nTEMPLATE so names stay unique.\n\n"
                    f"[{PASTEL['label']}]press any key to dismiss[/]"
                )
                return
            self.result = (self.filt, self.worker_count, self.template)
            self.exit()

        def action_cancel(self) -> None:
            self.result = None
            self.exit()

    return ArchiveDesignerApp()


def run_archive_designer(
    candidates: list[str],
    filt: str = "",
    workers: int = 3,
    template: str = "{name}-{date}.tar.gz",
) -> tuple[str, int, str] | None:
    """Launch the archive designer; return (filt, workers, template) or None."""
    app = make_archive_designer_app(candidates, filt, workers, template)
    app.run()
    return app.result


# ===========================================================================
# Archive planner (Textual TUI, with size-investigation wait popup)
# ===========================================================================


def make_archive_planner_app(
    archives: list[Archive],
    workers: int,
    dry_run: bool,
):
    """Build the archive-planner App. On mount it probes volume sizes in a
    ThreadPoolExecutor behind a centred wait popup, then shows the selectable
    plan. Returns ('confirm', indices) / ('edit', []) / ('cancel', [])."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal
    from textual.widgets import Footer, OptionList, Static
    from textual.widgets.option_list import Option

    class ArchivePlannerApp(App):
        CSS = f"""
        Screen {{ layout: vertical; background: {DUO['bg_dim']}; layers: base overlay; align: center middle; }}
{HEADER_CSS}
        #header {{
            height: auto;
            padding: 1 2;
            background: {DUO['bg_subtle']};
            border-bottom: heavy {DUO['border']};
            color: {DUO['text']};
        }}

        OptionList {{
            background: {DUO['bg_dim']};
            color: {DUO['text']};
            padding: 0 2;
            border: none;
            height: 1fr;
        }}
        OptionList > .option-list--option {{ text-wrap: nowrap; }}
        OptionList > .option-list--option-highlighted {{
            background: {DUO['surface']};
            color: {DUO['text']};
            text-style: none;
        }}
        OptionList:focus > .option-list--option-highlighted {{
            background: {DUO['surface']};
            color: {DUO['text']};
            text-style: none;
        }}

        #wait-box {{
            layer: overlay;
            display: none;
            width: auto; height: auto;
            padding: 1 4;
            background: {DUO['bg_subtle']};
            border: heavy {DUO['cyan']};
            text-align: center;
        }}
        #wait-box.-show {{ display: block; }}

        Footer {{ background: {DUO['bg_subtle']}; color: {DUO['text_muted']}; }}
        """

        BINDINGS = [
            Binding("space", "toggle", "Toggle", priority=True),
            Binding("enter", "confirm", "Run selected", priority=True),
            Binding("ctrl+s", "confirm", "Run selected", show=False),
            Binding("e", "edit", "Edit (back)"),
            Binding("a", "select_all", "All"),
            Binding("n", "select_none", "None"),
            Binding("ctrl+c", "cancel", "Cancel"),
            Binding("escape", "cancel", "Cancel"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.archives = archives
            self.worker_count = workers
            self.dry_run = dry_run
            self.selected: set[int] = set(range(len(archives)))
            self.result: tuple[str, list[int]] | None = None
            self._sized = False
            self._pool: ThreadPoolExecutor | None = None
            self._done = 0

        def get_system_commands(self, screen):
            for cmd in super().get_system_commands(screen):
                if cmd.title != "Theme":
                    yield cmd

        def _prompt(self, i: int) -> Text:
            a = self.archives[i]
            selected = i in self.selected
            line = Text()
            line.append("X " if selected else "  ",
                        style=f"bold {DUO['orange']}")
            line.append_text(styled_volume(
                a.name, "", f"bold {PASTEL['user']}", PASTEL["user"], "", None,
            ))
            line.append("  ->  ", style=PASTEL["label"])
            line.append(a.target, style=PASTEL["to"])
            line.append("   ", style=PASTEL["label"])
            line.append(human(a.size_bytes), style=PASTEL["warn"])
            return line

        def _header_markup(self) -> str:
            mode = "DRY-RUN" if self.dry_run else "ARCHIVE"
            total = sum(a.size_bytes for a in self.archives)
            lines = [
                f"[{PASTEL['title']}]ARCHIVE PLAN[/]   "
                f"[{PASTEL['label']}]mode:[/] [{PASTEL['warn']}]{mode}[/]   "
                f"[{PASTEL['label']}]workers:[/] "
                f"[{PASTEL['title']}]{self.worker_count}[/]   "
                f"[{PASTEL['label']}]volumes:[/] "
                f"[{PASTEL['ok']}]{len(self.archives)}[/]   "
                f"[{PASTEL['label']}]total:[/] "
                f"[{PASTEL['warn']}]{human(total)}[/]",
                f"[{PASTEL['label']}]target dir:[/] "
                f"[{PASTEL['info']}]{os.getcwd()}[/]",
                "[dim]Space toggles row   a = all   n = none   "
                "e = edit (back to designer)   "
                "Enter = run selected   Esc = cancel[/]",
            ]
            return "\n".join(lines)

        def _rebuild(self) -> None:
            ol = self.query_one(OptionList)
            hi = ol.highlighted
            ol.clear_options()
            ol.add_options(
                [Option(self._prompt(i), id=str(i))
                 for i in range(len(self.archives))]
            )
            if self.archives:
                ol.highlighted = 0 if hi is None \
                    else min(hi, len(self.archives) - 1)

        def compose(self) -> ComposeResult:
            with Horizontal(id="app-header"):
                yield Static(APP_TITLE, id="hdr-title")
                yield Static(f"v{VERSION}", id="hdr-version")
            yield Static(Text.from_markup(self._header_markup()), id="header")
            yield OptionList(id="plan")
            yield Static(id="wait-box")
            yield Footer()

        def on_mount(self) -> None:
            self.title = "Volume Archive Plan"
            self.sub_title = "investigating volume sizes - please wait"
            box = self.query_one("#wait-box", Static)
            box.update(Text.from_markup(
                f"[bold {PASTEL['title']}]Investigating volume sizes[/]\n\n"
                f"[{PASTEL['label']}]please wait[/]\n\n"
                f"[{PASTEL['info']}]computing sizes: 0/"
                f"{len(self.archives)}[/]"
            ))
            box.add_class("-show")
            self._pool = ThreadPoolExecutor(max_workers=self.worker_count)
            for a in self.archives:
                self._pool.submit(self._size_one, a)

        def _size_one(self, a: Archive) -> None:
            a.size_bytes = volume_size(a.name)
            self.call_from_thread(self._size_done)

        def _size_done(self) -> None:
            self._done += 1
            box = self.query_one("#wait-box", Static)
            box.update(Text.from_markup(
                f"[bold {PASTEL['title']}]Investigating volume sizes[/]\n\n"
                f"[{PASTEL['label']}]please wait[/]\n\n"
                f"[{PASTEL['info']}]computing sizes: {self._done}/"
                f"{len(self.archives)}[/]"
            ))
            if self._done >= len(self.archives):
                self._finish_sizing()

        def _finish_sizing(self) -> None:
            self.query_one("#wait-box", Static).remove_class("-show")
            self.query_one("#header", Static).update(
                Text.from_markup(self._header_markup()))
            self._sized = True
            self._rebuild()
            self.sub_title = (
                "review selection - Enter to run, 'e' to edit, Esc to cancel"
            )
            self.query_one(OptionList).focus()

        def action_toggle(self) -> None:
            if not self._sized:
                return
            ol = self.query_one(OptionList)
            i = ol.highlighted
            if i is None:
                return
            if i in self.selected:
                self.selected.discard(i)
            else:
                self.selected.add(i)
            ol.replace_option_prompt_at_index(i, self._prompt(i))

        def action_select_all(self) -> None:
            if not self._sized:
                return
            self.selected = set(range(len(self.archives)))
            self._rebuild()

        def action_select_none(self) -> None:
            if not self._sized:
                return
            self.selected = set()
            self._rebuild()

        def action_confirm(self) -> None:
            if not self._sized:
                return
            self.result = ("confirm", sorted(self.selected))
            self.exit()

        def action_edit(self) -> None:
            self.result = ("edit", [])
            self.exit()

        def action_cancel(self) -> None:
            self.result = ("cancel", [])
            self.exit()

    return ArchivePlannerApp()


def run_archive_planner(
    archives: list[Archive],
    workers: int,
    dry_run: bool,
) -> tuple[str, list[int]] | None:
    """Show the archive plan (probing sizes first); return the result tuple."""
    app = make_archive_planner_app(archives, workers, dry_run)
    app.run()
    if app._pool is not None:
        app._pool.shutdown(wait=False)
    return app.result


# ===========================================================================
# Archive execution (Textual TUI, mirrors run_execution)
# ===========================================================================


def make_archive_execution_app(
    archives: list[Archive],
    workers: int,
    dry_run: bool,
):
    """Build the archive-execution App: overall count bar + per-volume rows,
    discovery orange pulse -> archiving cyan determinate -> done mint / fail
    rose, completion popup."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import Footer, ProgressBar, Static

    LABEL_W = 46
    STAGE_W = 11
    title_word = "Verifying (dry-run)" if dry_run else "Archiving"

    class ArcRow(Horizontal):
        def __init__(self, idx: int, a: Archive) -> None:
            super().__init__(id=f"row-{idx}", classes="mig-row queued")
            self.a = a

        def compose(self) -> ComposeResult:
            yield Static(trunc_left(self.a.name, LABEL_W), classes="vol-name")
            yield Static(Text("queued", style=DUO["text_subtle"]),
                         classes="stage")
            yield ProgressBar(
                total=1, show_eta=False, show_percentage=not dry_run, id="bar",
            )

        def state(self, cls: str) -> None:
            self.remove_class("queued", "running", "done", "fail")
            self.add_class(cls)

        def set_stage(self, text: str, color: str) -> None:
            self.query_one(".stage", Static).update(Text(text, style=color))

    class ArchiveApp(App):
        CSS = f"""
        Screen {{ background: {DUO['bg_dim']}; layers: base overlay; align: center middle; }}
{HEADER_CSS}
        #overall-row {{
            height: auto; padding: 0 2;
            background: {DUO['bg_subtle']};
            border-bottom: heavy {DUO['border']};
        }}
        #overall-name {{ width: {LABEL_W + 2 + STAGE_W}; color: {DUO['cyan']}; text-style: bold; }}

        #rows {{ height: 1fr; padding: 0 2; background: {DUO['bg']}; }}
        .mig-row {{ height: 1; }}
        .vol-name {{ width: {LABEL_W + 2}; }}
        .stage {{ width: {STAGE_W}; }}
        .mig-row.queued .vol-name {{ color: {DUO['text_subtle']}; }}
        .mig-row.running .vol-name {{ color: {DUO['cyan']}; }}
        .mig-row.done .vol-name {{ color: {DUO['mint']}; }}
        .mig-row.fail .vol-name {{ color: {DUO['rose']}; }}

        ProgressBar {{ width: 1fr; height: 1; }}
        Bar {{ width: 1fr; }}
        Bar > .bar--bar {{ color: {DUO['cyan']}; background: {DUO['surface']}; }}
        Bar > .bar--indeterminate {{ color: {DUO['orange']}; background: {DUO['surface']}; }}
        Bar > .bar--complete {{ color: {DUO['mint']}; background: {DUO['surface']}; }}
        .mig-row.fail Bar > .bar--bar {{ color: {DUO['rose']}; }}
        .mig-row.fail Bar > .bar--indeterminate {{ color: {DUO['rose']}; }}
        .mig-row.fail Bar > .bar--complete {{ color: {DUO['rose']}; }}
        #overall-row Bar > .bar--bar {{ color: {DUO['cyan']}; background: {DUO['surface']}; }}
        #overall-row Bar > .bar--complete {{ color: {DUO['mint']}; background: {DUO['surface']}; }}

        #banner-box {{
            layer: overlay;
            display: none;
            width: auto; height: auto;
            padding: 1 4;
            background: {DUO['bg_subtle']};
            border: heavy {DUO['cyan']};
            text-align: center;
        }}
        #banner-box.-show {{ display: block; }}

        Footer {{ background: {DUO['bg_subtle']}; color: {DUO['text_muted']}; }}
        """

        BINDINGS = [Binding("ctrl+c", "quit", "Cancel")]

        def __init__(self) -> None:
            super().__init__()
            self.archives = archives
            self.rows: dict[str, ArcRow] = {}
            self.completed = 0
            self.finished = False
            self.start_time = 0.0
            self._pool: ThreadPoolExecutor | None = None

        def get_system_commands(self, screen):
            for cmd in super().get_system_commands(screen):
                if cmd.title != "Theme":
                    yield cmd

        def compose(self) -> ComposeResult:
            with Horizontal(id="app-header"):
                yield Static(
                    f"{APP_TITLE}   "
                    f"[{PASTEL['label']}]{title_word} · "
                    f"{len(self.archives)} volumes · "
                    f"{workers} workers[/]",
                    id="hdr-title",
                )
                yield Static(f"v{VERSION}", id="hdr-version")
            with Horizontal(id="overall-row"):
                yield Static("OVERALL", id="overall-name")
                yield ProgressBar(
                    total=len(self.archives), show_eta=False,
                    show_percentage=True, id="overall-bar",
                )
            with VerticalScroll(id="rows"):
                for i, a in enumerate(self.archives):
                    row = ArcRow(i, a)
                    self.rows[a.label] = row
                    yield row
            yield Static(id="banner-box")
            yield Footer()

        def on_mount(self) -> None:
            self.title = "Volume Archive"
            self.start_time = time.monotonic()
            self._pool = ThreadPoolExecutor(max_workers=workers)
            for a in self.archives:
                self._pool.submit(self._do_one, a)

        # --- worker thread side -------------------------------------------
        def _do_one(self, a: Archive) -> None:
            self.call_from_thread(self._start_row, a)
            if dry_run:
                a.success = archive_dry(a)
            else:
                a.success = run_archive_worker(
                    a,
                    lambda done, total: self.call_from_thread(
                        self._update_bar, a, done, total
                    ),
                    lambda stage: self.call_from_thread(
                        self._set_stage, a, stage),
                )
            self.call_from_thread(self._complete_row, a)

        # --- UI thread side -----------------------------------------------
        def _start_row(self, a: Archive) -> None:
            row = self.rows[a.label]
            row.state("running")
            if dry_run:
                row.set_stage("verify", DUO["cyan"])
            else:
                row.set_stage("discovery", DUO["orange"])
                row.query_one("#bar", ProgressBar).update(total=None)
            row.scroll_visible()

        def _set_stage(self, a: Archive, stage: str) -> None:
            row = self.rows[a.label]
            if stage == "discovery":
                row.set_stage("discovery", DUO["orange"])
                row.query_one("#bar", ProgressBar).update(total=None)
            elif stage == "archiving":
                row.set_stage("archiving", DUO["cyan"])

        def _update_bar(self, a: Archive, done: int, total: int) -> None:
            if total > 0:
                self.rows[a.label].query_one("#bar", ProgressBar).update(
                    total=total, progress=done,
                )

        def _complete_row(self, a: Archive) -> None:
            row = self.rows[a.label]
            bar = row.query_one("#bar", ProgressBar)
            if a.success:
                if bar.total is None:
                    bar.update(total=1)
                bar.update(progress=bar.total or 1)
                row.state("done")
                row.set_stage("done", DUO["mint"])
            else:
                if bar.total is None:
                    bar.update(total=1, progress=0)
                row.state("fail")
                row.set_stage("failed", DUO["rose"])
            self.query_one("#overall-bar", ProgressBar).advance(1)
            self.completed += 1
            if self.completed >= len(self.archives):
                self._finish()
            else:
                self._scroll_frontier()

        def _scroll_frontier(self) -> None:
            for a in self.archives:
                row = self.rows[a.label]
                if row.has_class("running") or row.has_class("queued"):
                    row.scroll_visible()
                    return

        def _finish(self) -> None:
            self.finished = True
            elapsed = time.monotonic() - self.start_time
            ok = sum(1 for x in self.archives if x.success)
            fails = len(self.archives) - ok
            verb = "Verification" if dry_run else "Archiving"
            state, color = ("complete", DUO["mint"]) if fails == 0 \
                else ("failed", DUO["rose"])
            fail_color = DUO["rose"] if fails else DUO["text_muted"]
            lines = [
                f"[bold {color}]{verb} {state}[/]",
                "",
                f"[{DUO['mint']}]{ok}[/] ok    [{fail_color}]{fails}[/] failed",
                f"[{DUO['text_muted']}]elapsed[/] "
                f"[{DUO['cyan']}]{fmt_duration(elapsed)}[/]",
                "",
                f"[{DUO['text_subtle']}]press any key to close[/]",
            ]
            box = self.query_one("#banner-box", Static)
            box.update(Text.from_markup("\n".join(lines)))
            box.styles.border = ("heavy", color)
            box.add_class("-show")
            self.sub_title = (
                f"done - {ok}/{len(self.archives)} ok · press any key")

        def on_key(self, event) -> None:
            if self.finished:
                self.exit()

    return ArchiveApp()


def run_archive_execution(
    archives: list[Archive],
    workers: int,
    dry_run: bool,
) -> None:
    """Run the archive jobs on a managed (alt-screen) Textual screen."""
    app = make_archive_execution_app(archives, workers, dry_run)
    app.run()
    if app._pool is not None:
        app._pool.shutdown(wait=False)


# ===========================================================================
# Delete designer (Textual TUI)
# ===========================================================================


def make_delete_designer_app(candidates: list[str], filt: str):
    """Build the delete-designer App: one SELECT regex field + a single preview
    pane of matched volumes. Returns (filt,) via app.result on confirm."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Footer, Input, Static

    SEL_STYLE = f"black on {PASTEL['filter']}"
    USER_STYLE = PASTEL["user"]
    DIM_STYLE = PASTEL["dim"]

    class DeleteDesignerApp(App):
        CSS = f"""
        Screen {{ layout: vertical; background: {DUO['bg_dim']}; layers: base overlay; align: center middle; }}
{HEADER_CSS}
        #inputs {{
            height: auto;
            padding: 1 2 0 2;
            background: {DUO['bg_subtle']};
        }}
        #status-bar {{
            height: auto;
            padding: 0 2;
            background: {DUO['bg_subtle']};
            border-bottom: heavy {DUO['border']};
        }}
        .desc {{ color: {DUO['text_muted']}; height: 1; }}
        Input {{
            margin: 0 0 1 0;
            background: {DUO['surface']};
            border: round {DUO['border']};
            color: {DUO['text']};
        }}
        Input:focus {{ border: round {DUO['cyan']}; }}

        #panels {{ height: 1fr; background: {DUO['bg']}; }}
        .col {{ width: 1fr; padding: 0 1; }}
        .panel-body {{ border: round {DUO['bg']}; }}
        .panel-body:focus {{ border: round {DUO['cyan']}; }}
        .col-title {{
            background: {DUO['surface']};
            color: {DUO['rose']};
            padding: 0 1;
            text-style: bold;
            text-align: center;
            height: 1;
        }}

        Footer {{ background: {DUO['bg_subtle']}; color: {DUO['text_muted']}; }}
        """

        BINDINGS = [
            Binding("enter", "confirm", "Confirm", priority=True),
            Binding("ctrl+s", "confirm", "Confirm", show=False),
            Binding("shift+tab", "autocomplete", "Autocomplete", priority=True),
            Binding("ctrl+c", "cancel", "Cancel"),
            Binding("escape", "cancel", "Cancel"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.all_volumes = candidates
            self.filt = filt
            self.result: tuple[str] | None = None

        def get_system_commands(self, screen):
            for cmd in super().get_system_commands(screen):
                if cmd.title != "Theme":
                    yield cmd

        def compose(self) -> ComposeResult:
            with Horizontal(id="app-header"):
                yield Static(APP_TITLE, id="hdr-title")
                yield Static(f"v{VERSION}", id="hdr-version")
            with Vertical(id="inputs"):
                yield Static(
                    f"[{PASTEL['label']}]SELECT regex - whole name "
                    f"(empty = all)  -  these volumes will be DELETED[/]",
                    classes="desc",
                )
                yield Input(
                    value=self.filt,
                    placeholder="e.g. ^alice",
                    id="select",
                    select_on_focus=False,
                )

            yield Static(id="status-bar")

            with Horizontal(id="panels"):
                with Vertical(classes="col"):
                    yield Static("TO DELETE  (volumes)", classes="col-title")
                    with VerticalScroll(classes="panel-body"):
                        yield Static(id="left-body")

            yield Footer()

        def on_mount(self) -> None:
            self.title = "Volume Delete Designer"
            self.sub_title = (
                "Shift+Tab complete - Enter confirm - Esc cancel"
            )
            self._refresh()
            self.query_one("#select", Input).focus()

        def on_input_changed(self, event) -> None:
            if event.input.id == "select":
                self.filt = event.value
            self._refresh()

        def on_input_submitted(self, event) -> None:
            self.action_confirm()

        def _matched(self) -> tuple[list[str], object, bool]:
            filt_re = None
            filt_ok = True
            if self.filt:
                try:
                    filt_re = re.compile(self.filt)
                except re.error:
                    filt_ok = False
            matched = []
            for v in self.all_volumes:
                if filt_re and not filt_re.search(v):
                    continue
                matched.append(v)
            return matched, filt_re, filt_ok

        def _refresh(self) -> None:
            matched, filt_re, filt_ok = self._matched()
            total = len(self.all_volumes)

            left_lines = [
                styled_volume(v, "", USER_STYLE, USER_STYLE, SEL_STYLE, filt_re)
                for v in matched
            ]

            status_bits: list[str] = []
            if not filt_ok:
                status_bits.append(f"[{PASTEL['err']}]invalid SELECT regex[/]")
            else:
                status_bits.append(
                    f"[{PASTEL['err']}]{len(matched)}[/] of "
                    f"[{PASTEL['info']}]{total}[/] volumes will be deleted"
                )
            status = Text.from_markup("  ".join(status_bits) or " ")
            self.query_one("#status-bar", Static).update(status)

            no_rows = Text("(no rows)", style=f"italic {DIM_STYLE}")
            self.query_one("#left-body", Static).update(
                Group(*left_lines) if left_lines else no_rows)

        def action_autocomplete(self) -> None:
            f = self.focused
            if not isinstance(f, Input) or f.id != "select":
                return
            completed = autocomplete_prefix(f.value, self.all_volumes)
            if completed != f.value:
                f.value = completed
                f.cursor_position = len(completed)

        def action_confirm(self) -> None:
            _, _, filt_ok = self._matched()
            if not filt_ok:
                return
            self.result = (self.filt,)
            self.exit()

        def action_cancel(self) -> None:
            self.result = None
            self.exit()

    return DeleteDesignerApp()


def run_delete_designer(
    candidates: list[str],
    filt: str = "",
) -> tuple[str] | None:
    """Launch the delete designer; return (filt,) or None."""
    app = make_delete_designer_app(candidates, filt)
    app.run()
    return app.result


# ===========================================================================
# Delete planner (Textual TUI, no size probe)
# ===========================================================================


def make_delete_planner_app(deletions: list[Deletion], dry_run: bool):
    """Build the delete-planner App: selectable rows, rose destructive warning
    header. Returns ('confirm', indices) / ('edit', []) / ('cancel', [])."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal
    from textual.widgets import Footer, OptionList, Static
    from textual.widgets.option_list import Option

    class DeletePlannerApp(App):
        CSS = f"""
        Screen {{ layout: vertical; background: {DUO['bg_dim']}; }}
{HEADER_CSS}
        #header {{
            height: auto;
            padding: 1 2;
            background: {DUO['bg_subtle']};
            border-bottom: heavy {DUO['rose']};
            color: {DUO['text']};
        }}

        OptionList {{
            background: {DUO['bg_dim']};
            color: {DUO['text']};
            padding: 0 2;
            border: none;
            height: 1fr;
        }}
        OptionList > .option-list--option {{ text-wrap: nowrap; }}
        OptionList > .option-list--option-highlighted {{
            background: {DUO['surface']};
            color: {DUO['text']};
            text-style: none;
        }}
        OptionList:focus > .option-list--option-highlighted {{
            background: {DUO['surface']};
            color: {DUO['text']};
            text-style: none;
        }}

        Footer {{ background: {DUO['bg_subtle']}; color: {DUO['text_muted']}; }}
        """

        BINDINGS = [
            Binding("space", "toggle", "Toggle", priority=True),
            Binding("enter", "confirm", "Run selected", priority=True),
            Binding("ctrl+s", "confirm", "Run selected", show=False),
            Binding("e", "edit", "Edit (back)"),
            Binding("a", "select_all", "All"),
            Binding("n", "select_none", "None"),
            Binding("ctrl+c", "cancel", "Cancel"),
            Binding("escape", "cancel", "Cancel"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.deletions = deletions
            self.dry_run = dry_run
            self.selected: set[int] = set(range(len(deletions)))
            self.result: tuple[str, list[int]] | None = None

        def get_system_commands(self, screen):
            for cmd in super().get_system_commands(screen):
                if cmd.title != "Theme":
                    yield cmd

        def _prompt(self, i: int) -> Text:
            d = self.deletions[i]
            selected = i in self.selected
            line = Text()
            line.append("X " if selected else "  ",
                        style=f"bold {DUO['orange']}")
            line.append(d.name, style=f"bold {PASTEL['user']}")
            return line

        def _rebuild(self) -> None:
            ol = self.query_one(OptionList)
            hi = ol.highlighted
            ol.clear_options()
            ol.add_options(
                [Option(self._prompt(i), id=str(i))
                 for i in range(len(self.deletions))]
            )
            if self.deletions:
                ol.highlighted = 0 if hi is None \
                    else min(hi, len(self.deletions) - 1)

        def compose(self) -> ComposeResult:
            mode = "DRY-RUN" if self.dry_run else "DELETE"
            header_lines = [
                f"[bold {PASTEL['err']}]WARNING: selected volumes will be "
                f"PERMANENTLY DELETED[/]",
                f"[{PASTEL['title']}]DELETE PLAN[/]   "
                f"[{PASTEL['label']}]mode:[/] [{PASTEL['warn']}]{mode}[/]   "
                f"[{PASTEL['label']}]volumes:[/] "
                f"[{PASTEL['ok']}]{len(self.deletions)}[/]",
                "[dim]Space toggles row   a = all   n = none   "
                "e = edit (back to designer)   "
                "Enter = run selected   Esc = cancel[/]",
            ]
            with Horizontal(id="app-header"):
                yield Static(APP_TITLE, id="hdr-title")
                yield Static(f"v{VERSION}", id="hdr-version")
            yield Static(
                Text.from_markup("\n".join(header_lines)), id="header")
            yield OptionList(id="plan")
            yield Footer()

        def on_mount(self) -> None:
            self.title = "Volume Delete Plan"
            self.sub_title = (
                "review selection - Enter to delete, 'e' to edit, Esc cancel"
            )
            self._rebuild()
            self.query_one(OptionList).focus()

        def action_toggle(self) -> None:
            ol = self.query_one(OptionList)
            i = ol.highlighted
            if i is None:
                return
            if i in self.selected:
                self.selected.discard(i)
            else:
                self.selected.add(i)
            ol.replace_option_prompt_at_index(i, self._prompt(i))

        def action_select_all(self) -> None:
            self.selected = set(range(len(self.deletions)))
            self._rebuild()

        def action_select_none(self) -> None:
            self.selected = set()
            self._rebuild()

        def action_confirm(self) -> None:
            self.result = ("confirm", sorted(self.selected))
            self.exit()

        def action_edit(self) -> None:
            self.result = ("edit", [])
            self.exit()

        def action_cancel(self) -> None:
            self.result = ("cancel", [])
            self.exit()

    return DeletePlannerApp()


def run_delete_planner(
    deletions: list[Deletion],
    dry_run: bool,
) -> tuple[str, list[int]] | None:
    """Show the delete plan (no size probe); return the result tuple."""
    app = make_delete_planner_app(deletions, dry_run)
    app.run()
    return app.result


# ===========================================================================
# Delete execution (Textual TUI, mirrors run_execution)
# ===========================================================================


def make_delete_execution_app(deletions: list[Deletion], dry_run: bool):
    """Build the delete-execution App: per-row brief indeterminate pulse then
    complete (total=1); real run calls remove_volume, dry_run uses delete_dry."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import Footer, ProgressBar, Static

    LABEL_W = 46
    STAGE_W = 11
    title_word = "Verifying (dry-run)" if dry_run else "Deleting"

    class DelRow(Horizontal):
        def __init__(self, idx: int, d: Deletion) -> None:
            super().__init__(id=f"row-{idx}", classes="mig-row queued")
            self.d = d

        def compose(self) -> ComposeResult:
            yield Static(trunc_left(self.d.name, LABEL_W), classes="vol-name")
            yield Static(Text("queued", style=DUO["text_subtle"]),
                         classes="stage")
            yield ProgressBar(
                total=1, show_eta=False, show_percentage=False, id="bar",
            )

        def state(self, cls: str) -> None:
            self.remove_class("queued", "running", "done", "fail")
            self.add_class(cls)

        def set_stage(self, text: str, color: str) -> None:
            self.query_one(".stage", Static).update(Text(text, style=color))

    class DeleteApp(App):
        CSS = f"""
        Screen {{ background: {DUO['bg_dim']}; layers: base overlay; align: center middle; }}
{HEADER_CSS}
        #overall-row {{
            height: auto; padding: 0 2;
            background: {DUO['bg_subtle']};
            border-bottom: heavy {DUO['border']};
        }}
        #overall-name {{ width: {LABEL_W + 2 + STAGE_W}; color: {DUO['cyan']}; text-style: bold; }}

        #rows {{ height: 1fr; padding: 0 2; background: {DUO['bg']}; }}
        .mig-row {{ height: 1; }}
        .vol-name {{ width: {LABEL_W + 2}; }}
        .stage {{ width: {STAGE_W}; }}
        .mig-row.queued .vol-name {{ color: {DUO['text_subtle']}; }}
        .mig-row.running .vol-name {{ color: {DUO['cyan']}; }}
        .mig-row.done .vol-name {{ color: {DUO['mint']}; }}
        .mig-row.fail .vol-name {{ color: {DUO['rose']}; }}

        ProgressBar {{ width: 1fr; height: 1; }}
        Bar {{ width: 1fr; }}
        Bar > .bar--bar {{ color: {DUO['cyan']}; background: {DUO['surface']}; }}
        Bar > .bar--indeterminate {{ color: {DUO['orange']}; background: {DUO['surface']}; }}
        Bar > .bar--complete {{ color: {DUO['mint']}; background: {DUO['surface']}; }}
        .mig-row.fail Bar > .bar--bar {{ color: {DUO['rose']}; }}
        .mig-row.fail Bar > .bar--indeterminate {{ color: {DUO['rose']}; }}
        .mig-row.fail Bar > .bar--complete {{ color: {DUO['rose']}; }}
        #overall-row Bar > .bar--bar {{ color: {DUO['cyan']}; background: {DUO['surface']}; }}
        #overall-row Bar > .bar--complete {{ color: {DUO['mint']}; background: {DUO['surface']}; }}

        #banner-box {{
            layer: overlay;
            display: none;
            width: auto; height: auto;
            padding: 1 4;
            background: {DUO['bg_subtle']};
            border: heavy {DUO['cyan']};
            text-align: center;
        }}
        #banner-box.-show {{ display: block; }}

        Footer {{ background: {DUO['bg_subtle']}; color: {DUO['text_muted']}; }}
        """

        BINDINGS = [Binding("ctrl+c", "quit", "Cancel")]

        def __init__(self) -> None:
            super().__init__()
            self.deletions = deletions
            self.rows: dict[str, DelRow] = {}
            self.completed = 0
            self.finished = False
            self.start_time = 0.0
            self._pool: ThreadPoolExecutor | None = None

        def get_system_commands(self, screen):
            for cmd in super().get_system_commands(screen):
                if cmd.title != "Theme":
                    yield cmd

        def compose(self) -> ComposeResult:
            with Horizontal(id="app-header"):
                yield Static(
                    f"{APP_TITLE}   "
                    f"[{PASTEL['label']}]{title_word} · "
                    f"{len(self.deletions)} volumes[/]",
                    id="hdr-title",
                )
                yield Static(f"v{VERSION}", id="hdr-version")
            with Horizontal(id="overall-row"):
                yield Static("OVERALL", id="overall-name")
                yield ProgressBar(
                    total=len(self.deletions), show_eta=False,
                    show_percentage=True, id="overall-bar",
                )
            with VerticalScroll(id="rows"):
                for i, d in enumerate(self.deletions):
                    row = DelRow(i, d)
                    self.rows[d.label] = row
                    yield row
            yield Static(id="banner-box")
            yield Footer()

        def on_mount(self) -> None:
            self.title = "Volume Delete"
            self.start_time = time.monotonic()
            # Single worker: deletions are quick and serial keeps the row
            # frontier readable.
            self._pool = ThreadPoolExecutor(max_workers=4)
            for d in self.deletions:
                self._pool.submit(self._do_one, d)

        # --- worker thread side -------------------------------------------
        def _do_one(self, d: Deletion) -> None:
            self.call_from_thread(self._start_row, d)
            # Brief indeterminate pulse so the row reads as active before the
            # (fast) operation completes.
            time.sleep(0.3)
            if dry_run:
                d.success = delete_dry(d)
            else:
                ok, err = remove_volume(d.name)
                d.success = ok
                if not ok:
                    d.error = err
            self.call_from_thread(self._complete_row, d)

        # --- UI thread side -----------------------------------------------
        def _start_row(self, d: Deletion) -> None:
            row = self.rows[d.label]
            row.state("running")
            if dry_run:
                row.set_stage("verify", DUO["cyan"])
            else:
                row.set_stage("deleting", DUO["orange"])
            row.query_one("#bar", ProgressBar).update(total=None)
            row.scroll_visible()

        def _complete_row(self, d: Deletion) -> None:
            row = self.rows[d.label]
            bar = row.query_one("#bar", ProgressBar)
            if d.success:
                bar.update(total=1, progress=1)
                row.state("done")
                row.set_stage("done", DUO["mint"])
            else:
                bar.update(total=1, progress=0)
                row.state("fail")
                row.set_stage("failed", DUO["rose"])
            self.query_one("#overall-bar", ProgressBar).advance(1)
            self.completed += 1
            if self.completed >= len(self.deletions):
                self._finish()
            else:
                self._scroll_frontier()

        def _scroll_frontier(self) -> None:
            for d in self.deletions:
                row = self.rows[d.label]
                if row.has_class("running") or row.has_class("queued"):
                    row.scroll_visible()
                    return

        def _finish(self) -> None:
            self.finished = True
            elapsed = time.monotonic() - self.start_time
            ok = sum(1 for x in self.deletions if x.success)
            fails = len(self.deletions) - ok
            verb = "Verification" if dry_run else "Deletion"
            state, color = ("complete", DUO["mint"]) if fails == 0 \
                else ("failed", DUO["rose"])
            fail_color = DUO["rose"] if fails else DUO["text_muted"]
            lines = [
                f"[bold {color}]{verb} {state}[/]",
                "",
                f"[{DUO['mint']}]{ok}[/] ok    [{fail_color}]{fails}[/] failed",
                f"[{DUO['text_muted']}]elapsed[/] "
                f"[{DUO['cyan']}]{fmt_duration(elapsed)}[/]",
                "",
                f"[{DUO['text_subtle']}]press any key to close[/]",
            ]
            box = self.query_one("#banner-box", Static)
            box.update(Text.from_markup("\n".join(lines)))
            box.styles.border = ("heavy", color)
            box.add_class("-show")
            self.sub_title = (
                f"done - {ok}/{len(self.deletions)} ok · press any key")

        def on_key(self, event) -> None:
            if self.finished:
                self.exit()

    return DeleteApp()


def run_delete_execution(deletions: list[Deletion], dry_run: bool) -> None:
    """Run the deletions on a managed (alt-screen) Textual screen."""
    app = make_delete_execution_app(deletions, dry_run)
    app.run()
    if app._pool is not None:
        app._pool.shutdown(wait=False)


# ===========================================================================
# Flow drivers (designer<->planner loop + execution + console summary)
# ===========================================================================


def run_plain_execution(jobs, worker_count: int, verb: str, process_one) -> None:
    """Headless execution with a plain Rich Progress (no Textual) - an overall
    counter plus a per-job bar that appears when the job starts. Runs fine with
    no TTY (CI / cron / pipes). `process_one(job, update)` runs one job, calls
    update(done, total) to drive its bar, and returns the success bool (setting
    job.error on failure). Workers run in a ThreadPoolExecutor; Rich's
    Progress.update is thread-safe."""
    from rich.progress import (
        BarColumn, Progress, TaskProgressColumn, TextColumn,
    )

    def _label(j) -> str:
        return getattr(j, "src", None) or getattr(j, "name", None) or j.label

    width = min(46, max((len(_label(j)) for j in jobs), default=10))
    progress = Progress(
        TextColumn("{task.description}"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        console=console,
    )
    with progress:
        overall = progress.add_task(f"[bold]{verb}[/]", total=len(jobs))

        def _run(j) -> None:
            name = trunc_left(_label(j), width)
            tid = progress.add_task(f"[{PASTEL['label']}]{name}[/]", total=None)
            seen = {"total": None}

            def update(done: int, total: int) -> None:
                if total and total > 0:
                    seen["total"] = total
                    progress.update(tid, total=total, completed=done)

            try:
                ok = bool(process_one(j, update))
            except Exception as e:  # never let one job kill the whole run
                ok = False
                if not getattr(j, "error", ""):
                    j.error = str(e)
            j.success = ok
            total = seen["total"] or 1
            color = PASTEL["ok"] if ok else PASTEL["err"]
            progress.update(
                tid, total=total, completed=total if ok else 0,
                description=f"[{color}]{name}[/]",
            )
            progress.advance(overall, 1)

        with ThreadPoolExecutor(max_workers=max(1, worker_count)) as ex:
            list(ex.map(_run, jobs))


def _match_volumes(filt: str) -> list[str]:
    """All volumes matching the SELECT regex (empty = all), sorted."""
    try:
        filt_re = re.compile(filt) if filt else None
    except re.error:
        return []
    vols = sorted(list_volumes())
    if filt_re is None:
        return vols
    return [v for v in vols if filt_re.search(v)]


def run_archive_flow(args, candidates: list[str], dry_run: bool) -> int:
    """Own the archive flow. Headless (--yes): match by --filter and run the
    plain executor, no TUI. Interactive: designer<->planner loop then the
    Textual execution screen. Both share the Rich summary."""
    filt = args.user_filter or ""
    workers = args.workers
    template = getattr(args, "template", "{name}-{date}.tar.gz")
    when = datetime.now()
    headless = getattr(args, "yes", False)

    def _archive_one(a, update):
        if dry_run:
            return archive_dry(a)
        return run_archive_worker(a, lambda d, t: update(d, t), lambda s: None)

    if headless:
        names = _match_volumes(filt)
        if not names:
            console.print(
                f"[{PASTEL['warn']}]No volumes match the selection - nothing "
                f"to archive.[/]"
            )
            return 0
        selected = [
            Archive(name=n, target=render_template(n, template, when))
            for n in names
        ]
        run_plain_execution(
            selected, workers,
            "Verifying (dry-run)" if dry_run else "Archiving",
            _archive_one,
        )
    else:
        next_screen = "designer"
        selected = None
        while True:
            if next_screen == "designer":
                designed = run_archive_designer(candidates, filt, workers, template)
                if designed is None:
                    console.print(f"[{PASTEL['err']}]Cancelled.[/]")
                    return 1
                filt, workers, template = designed

            names = _match_volumes(filt)
            if not names:
                console.print(
                    f"[{PASTEL['warn']}]No matching volumes for the current "
                    f"selection - returning to designer.[/]"
                )
                next_screen = "designer"
                continue

            archives = [
                Archive(name=n, target=render_template(n, template, when))
                for n in names
            ]

            plan_result = run_archive_planner(archives, workers, dry_run)
            if plan_result is None or plan_result[0] == "cancel":
                console.print(f"[{PASTEL['err']}]Cancelled.[/]")
                return 1
            if plan_result[0] == "edit":
                next_screen = "designer"
                continue
            if plan_result[0] == "confirm":
                indices = plan_result[1]
                if not indices:
                    console.print(
                        f"[{PASTEL['warn']}]No rows selected - back to designer.[/]"
                    )
                    next_screen = "designer"
                    continue
                selected = [archives[i] for i in indices]
                break

        assert selected is not None
        run_archive_execution(selected, workers, dry_run)

    # Summary
    console.rule("[bold]Summary[/bold]")
    ok_count = sum(1 for a in selected if a.success)
    fail_count = len(selected) - ok_count
    console.print(
        f"Succeeded: [green]{ok_count}[/green]   "
        f"Failed: [red]{fail_count}[/red]"
    )
    for a in selected:
        if not a.success:
            console.print(f"  [red]FAIL[/red] {a.label}: {a.error}")
    if dry_run:
        console.print(
            f"\n[dim]Dry-run: nothing written. Verified {ok_count} "
            f"volume(s) can be archived into {os.getcwd()}.[/dim]"
        )
    else:
        written = [a for a in selected if a.success]
        if written:
            console.print("\n[dim]Tarballs written under "
                          f"{os.getcwd()}:[/dim]")
            for a in written:
                console.print(
                    f"  [dim]{os.path.join(os.getcwd(), a.target)}[/dim]")
    return 0 if fail_count == 0 else 2


def run_delete_flow(args, candidates: list[str], dry_run: bool) -> int:
    """Own the delete flow. Headless (--yes): match by --filter and run the
    plain executor, no TUI - an empty filter is refused so a headless run can
    never wipe every volume by omission. Interactive: designer<->planner loop
    then the Textual execution. Both share the Rich summary."""
    filt = args.user_filter or ""
    headless = getattr(args, "yes", False)

    def _delete_one(d, update):
        if dry_run:
            ok = delete_dry(d)
            update(1, 1)
            return ok
        ok, err = remove_volume(d.name)
        if not ok:
            d.error = err
        update(1, 1)
        return ok

    if headless:
        if not filt:
            console.print(
                f"[{PASTEL['err']}]Refusing to delete every volume "
                f"headlessly.[/]"
            )
            console.print(
                f"[{PASTEL['label']}]Pass --filter to scope the delete "
                f"(--filter '.*' to really target all), or drop --yes for the "
                f"interactive picker.[/]"
            )
            return 2
        names = _match_volumes(filt)
        if not names:
            console.print(
                f"[{PASTEL['warn']}]No volumes match the selection - nothing "
                f"to delete.[/]"
            )
            return 0
        selected = [Deletion(name=n) for n in names]
        run_plain_execution(
            selected, 1,
            "Verifying (dry-run)" if dry_run else "Deleting",
            _delete_one,
        )
    else:
        next_screen = "designer"
        selected = None
        while True:
            if next_screen == "designer":
                designed = run_delete_designer(candidates, filt)
                if designed is None:
                    console.print(f"[{PASTEL['err']}]Cancelled.[/]")
                    return 1
                (filt,) = designed

            names = _match_volumes(filt)
            if not names:
                console.print(
                    f"[{PASTEL['warn']}]No matching volumes for the current "
                    f"selection - returning to designer.[/]"
                )
                next_screen = "designer"
                continue

            deletions = [Deletion(name=n) for n in names]

            plan_result = run_delete_planner(deletions, dry_run)
            if plan_result is None or plan_result[0] == "cancel":
                console.print(f"[{PASTEL['err']}]Cancelled.[/]")
                return 1
            if plan_result[0] == "edit":
                next_screen = "designer"
                continue
            if plan_result[0] == "confirm":
                indices = plan_result[1]
                if not indices:
                    console.print(
                        f"[{PASTEL['warn']}]No rows selected - back to designer.[/]"
                    )
                    next_screen = "designer"
                    continue
                selected = [deletions[i] for i in indices]
                break

        assert selected is not None
        run_delete_execution(selected, dry_run)

    # Summary
    console.rule("[bold]Summary[/bold]")
    ok_count = sum(1 for d in selected if d.success)
    fail_count = len(selected) - ok_count
    console.print(
        f"Succeeded: [green]{ok_count}[/green]   "
        f"Failed: [red]{fail_count}[/red]"
    )
    for d in selected:
        if not d.success:
            console.print(f"  [red]FAIL[/red] {d.label}: {d.error}")
    if dry_run:
        console.print(
            f"\n[dim]Dry-run: nothing deleted. Verified {ok_count} "
            f"volume(s) exist.[/dim]"
        )
    else:
        removed = [d for d in selected if d.success]
        if removed:
            console.print("\n[dim]Volumes removed:[/dim]")
            for d in removed:
                console.print(f"  [dim]{d.name}[/dim]")
    return 0 if fail_count == 0 else 2


def run_migrate_flow(args, candidates: list[str], dry_run: bool) -> int:
    """Own the migrate flow. Headless (--yes): copy from --from/--to/--filter
    via the plain executor, no TUI. Interactive: designer<->planner loop then
    the Textual execution. Both share the conflict check and Rich summary."""
    fp = args.from_prefix or ""
    tp = args.to_prefix or ""
    flt = args.user_filter or ""
    workers = args.workers
    overwrite = args.overwrite
    rm_src = args.rm_src
    headless = getattr(args, "yes", False)
    selected_migs: list[Migration] | None = None

    if headless:
        if not fp or not tp:
            console.print(
                f"[{PASTEL['err']}]--from and --to are required for a headless "
                f"migrate.[/]"
            )
            return 2
        try:
            user_filter = re.compile(flt) if flt else None
        except re.error as e:
            console.print(f"[{PASTEL['err']}]invalid filter regex: {e}[/]")
            return 2
        migs = discover_migrations(fp, tp, user_filter)
        if not migs:
            console.print(
                f"[{PASTEL['warn']}]No volumes match the current "
                f"prefixes/filter - nothing to migrate.[/]"
            )
            return 0
        selected_migs = migs
    else:
        no_args = not (args.from_prefix or args.to_prefix)
        next_screen = "designer" if no_args or not (fp and tp) else "planner"
        while True:
            if next_screen == "designer":
                designed = run_designer(
                    candidates, fp, tp, flt, workers, overwrite, rm_src,
                )
                if designed is None:
                    console.print(f"[{PASTEL['err']}]Cancelled.[/]")
                    return 1
                fp, tp, flt, workers, overwrite, rm_src = designed

            if not fp or not tp:
                console.print(
                    f"[{PASTEL['err']}]--from and --to are both required.[/]"
                )
                next_screen = "designer"
                continue

            try:
                user_filter = re.compile(flt) if flt else None
            except re.error as e:
                console.print(f"[{PASTEL['err']}]invalid filter regex: {e}[/]")
                next_screen = "designer"
                continue

            migs = discover_migrations(fp, tp, user_filter)
            if not migs:
                console.print(
                    f"[{PASTEL['warn']}]No matching volumes for the current "
                    f"prefixes/filter - returning to designer.[/]"
                )
                next_screen = "designer"
                continue

            existing_volumes = list_volumes()

            plan_result = run_planner(
                migs, workers, dry_run, overwrite, existing_volumes,
                fp, tp, user_filter, rm_src,
            )
            if plan_result is None or plan_result[0] == "cancel":
                console.print(f"[{PASTEL['err']}]Cancelled.[/]")
                return 1
            if plan_result[0] == "edit":
                next_screen = "designer"
                continue
            if plan_result[0] == "confirm":
                indices = plan_result[1]
                if not indices:
                    console.print(
                        f"[{PASTEL['warn']}]No rows selected - back to designer.[/]"
                    )
                    next_screen = "designer"
                    continue
                selected_migs = [migs[i] for i in indices]
                break

    assert selected_migs is not None
    migs = selected_migs
    args.from_prefix = fp
    args.to_prefix = tp
    args.user_filter = flt or None
    args.workers = workers
    args.overwrite = overwrite
    args.rm_src = rm_src

    # Check destination state vs. overwrite policy. An existing destination is
    # only ever overwritten in place - rsync --delete mirrors the source into
    # the kept volume (cleaned, never removed/recreated). With overwrite off,
    # any pre-existing destination is a hard error: abort before copying.
    all_volumes = list_volumes()
    conflicts = [m for m in migs if m.dst in all_volumes]
    if conflicts and not args.overwrite:
        console.print(
            f"[{PASTEL['err']}]Error: {len(conflicts)} destination volume(s) "
            f"already exist and overwrite is off:[/]"
        )
        for m in conflicts:
            console.print(f"  [{PASTEL['err']}]·[/] {m.dst}")
        console.print(
            f"[{PASTEL['err']}]Aborting - nothing copied. Enable overwrite to "
            f"clean and replace them (volume kept, contents mirrored from "
            f"source).[/]"
        )
        return 2

    for m in migs:
        ensure_dst_exists(m, all_volumes)

    def _migrate_one(m, update):
        if dry_run:
            return run_dry(m)
        ok = run_copy(m, lambda d, t: update(d, t), lambda s: None)
        if ok and rm_src:
            removed, err = remove_volume(m.src)
            m.removed = removed
            if not removed:
                m.error = f"copied ok, source removal failed: {err}"
        return ok

    if not migs:
        console.print("[yellow]Nothing to run.[/yellow]")
    elif headless:
        run_plain_execution(
            migs, args.workers,
            "Verifying (dry-run)" if dry_run else "Migrating",
            _migrate_one,
        )
    else:
        run_execution(migs, args.workers, dry_run, args.rm_src)

    # Summary
    console.rule("[bold]Summary[/bold]")
    ok_count = sum(1 for m in migs if m.success)
    fail_count = len(migs) - ok_count
    removed_count = sum(1 for m in migs if m.removed)
    summary = (
        f"Succeeded: [green]{ok_count}[/green]   "
        f"Failed: [red]{fail_count}[/red]"
    )
    if args.rm_src and not dry_run:
        summary += f"   Sources removed: [green]{removed_count}[/green]"
    console.print(summary)
    for m in migs:
        if not m.success:
            console.print(f"  [red]FAIL[/red] {m.label}: {m.error}")

    # Sources that copied OK but weren't removed - list manual cleanup commands.
    kept = [m for m in migs if m.success and not m.removed]
    if not dry_run and kept:
        console.print("\n[dim]Source volumes left intact. After verification, remove with:[/dim]")
        for m in kept:
            console.print(f"  [dim]docker volume rm {m.src}[/dim]")

    return 0 if fail_count == 0 else 2


def main() -> int:
    # --dry-run is offered on the top level (for the menu) AND on each command.
    # SUPPRESS as the per-command default means an absent command-level flag does
    # not clobber a top-level `--dry-run` that came before the command.
    dry_flag = dict(action="store_true", default=argparse.SUPPRESS,
                    help="verify only - mount / inspect, write / delete nothing")

    p = argparse.ArgumentParser(
        description=__doc__,
        epilog="No command opens the interactive menu. A command without --yes "
               "opens that mode's TUI pre-filled; with --yes it runs headless "
               "(plain console, no TUI). --dry-run works globally and on every "
               "command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true",
                   help="verify only - write / delete nothing (also applies to "
                        "the menu action)")
    sub = p.add_subparsers(dest="command",
                           metavar="{migrate,archive,delete}")

    mp = sub.add_parser("migrate",
                        help="copy volumes from one name prefix to another")
    mp.add_argument("--from", dest="from_prefix", metavar="PREFIX",
                    help="source volume name prefix (e.g. 'jupyterlab-')")
    mp.add_argument("--to", dest="to_prefix", metavar="PREFIX",
                    help="destination volume name prefix")
    mp.add_argument("--filter", dest="user_filter", metavar="REGEX",
                    help="regex applied to the full source volume name")
    mp.add_argument("--workers", type=int, default=3, metavar="N",
                    help="parallel copy containers (default 3)")
    mp.add_argument("--overwrite", action="store_true",
                    help="overwrite destination volumes that already exist")
    mp.add_argument("--remove-source", dest="rm_src", action="store_true",
                    help="delete each source volume after its successful copy")
    mp.add_argument("--yes", action="store_true",
                    help="run headless from these flags (no TUI)")
    mp.add_argument("--dry-run", **dry_flag)

    ap = sub.add_parser("archive",
                        help="tar.gz selected volumes into the current directory")
    ap.add_argument("--filter", dest="user_filter", metavar="REGEX",
                    help="regex selecting volumes to archive (empty = all)")
    ap.add_argument("--workers", type=int, default=3, metavar="N",
                    help="parallel archive containers (default 3)")
    ap.add_argument("--template", default="{name}-{date}.tar.gz", metavar="TPL",
                    help="archive filename template; placeholders {name} {date} "
                         "{datetime} (default '{name}-{date}.tar.gz')")
    ap.add_argument("--yes", action="store_true",
                    help="run headless from these flags (no TUI)")
    ap.add_argument("--dry-run", **dry_flag)

    dp = sub.add_parser("delete",
                        help="permanently remove selected volumes")
    dp.add_argument("--filter", dest="user_filter", metavar="REGEX",
                    help="regex selecting volumes to delete (required with --yes)")
    dp.add_argument("--yes", action="store_true",
                    help="run headless from these flags (no TUI)")
    dp.add_argument("--dry-run", **dry_flag)

    args = p.parse_args()

    # Fill defaults for attributes the chosen command (or the menu) did not
    # define, so every flow can read args.X regardless of how it was launched.
    for key, default in (
        ("command", None), ("from_prefix", None), ("to_prefix", None),
        ("user_filter", None), ("workers", 3), ("overwrite", False),
        ("rm_src", False), ("yes", False), ("template", "{name}-{date}.tar.gz"),
    ):
        if not hasattr(args, key):
            setattr(args, key, default)

    if args.workers < 1:
        p.error("--workers must be >= 1")

    # Host dependency: Docker must be installed and its daemon reachable before
    # any action touches a volume.
    _preflight_docker()

    dry_run = args.dry_run

    candidates = list_candidate_volumes()
    if not candidates:
        console.print(f"[{PASTEL['warn']}]No Docker volumes found.[/]")
        return 0

    # A command picks the action directly; no command opens the menu.
    if args.command is None:
        action = run_menu(dry_run)
        if action is None:
            console.print(f"[{PASTEL['err']}]Cancelled.[/]")
            return 1
    else:
        action = args.command

    if action == "migrate":
        return run_migrate_flow(args, candidates, dry_run)
    if action == "archive":
        return run_archive_flow(args, candidates, dry_run)
    if action == "delete":
        return run_delete_flow(args, candidates, dry_run)
    return 1


if __name__ == "__main__":
    sys.exit(main())
