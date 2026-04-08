import sys
import os
import atexit
# Inject the parent directory into sys.path so that imports work even when sudo strips PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn, TaskProgressColumn, DownloadColumn
from rich.tree import Tree
from rich.rule import Rule
from rich import box
from drill_engine.discovery import get_macos_disks
from drill_engine.quick_scan import TSKScanner
from drill_engine.deep_scan import DeepScanner, _format_duration
from drill_engine.file_repair import scan_directory as repair_scan_dir, repair_batch, RepairStatus, RepairResult, FileType, save_scan_cache, load_scan_cache
import subprocess
import time

console = Console()

VERSION = "1.1.0"

# ─── Branding colors ─────────────────────────────────────────────────────────
ACCENT = "bright_cyan"
ACCENT2 = "bright_magenta"
DIM = "dim"
SUCCESS = "bold green"
WARN = "bold yellow"
ERR = "bold red"

# ─── Global cleanup state ────────────────────────────────────────────────────
_cleanup_disk_id = None

def _ensure_disk_remounted():
    """Atexit handler: remount the disk if the app exits while it's unmounted."""
    global _cleanup_disk_id
    if _cleanup_disk_id:
        console.print(f"\n[{WARN}]Safety remount: remounting {_cleanup_disk_id}...[/{WARN}]")
        subprocess.run(["diskutil", "mount", _cleanup_disk_id], capture_output=True)
        _cleanup_disk_id = None

atexit.register(_ensure_disk_remounted)

# ─── Performance profiles ────────────────────────────────────────────────────
PERFORMANCE_PROFILES = {
    "fast": {
        "label": "Fast",
        "icon": ">>",
        "description": "Maximum speed, may heat up your Mac",
        "scan_throttle": 0,
        "chunk_size": 2 * 1024 * 1024,
        "burst_size": 100 * 1024 * 1024,
        "rest_duration": 0.05,
        "work_interval": None,
        "break_duration": 0,
    },
    "balanced": {
        "label": "Balanced",
        "icon": "<>",
        "description": "Good speed with moderate heat management",
        "scan_throttle": 0.001,
        "chunk_size": 1024 * 1024,
        "burst_size": 50 * 1024 * 1024,
        "rest_duration": 0.1,
        "work_interval": 600,
        "break_duration": 30,
    },
    "cool": {
        "label": "Cool",
        "icon": "~~",
        "description": "Slower but keeps your Mac cool",
        "scan_throttle": 0.002,
        "chunk_size": 512 * 1024,
        "burst_size": 25 * 1024 * 1024,
        "rest_duration": 0.2,
        "work_interval": 300,
        "break_duration": 60,
    },
    "siberia": {
        "label": "Siberia",
        "icon": "**",
        "description": "Maximum thermal safety for huge jobs",
        "scan_throttle": 0.005,
        "chunk_size": 256 * 1024,
        "burst_size": 10 * 1024 * 1024,
        "rest_duration": 0.5,
        "work_interval": 180,
        "break_duration": 60,
    },
}

# Known file types that PhotoRec can recover
PHOTOREC_FILE_TYPES = [
    "jpg", "png", "gif", "bmp", "tiff", "webp", "psd", "raw", "cr2", "nef",
    "mp4", "mov", "avi", "mkv", "wmv", "flv", "webm", "m4v", "3gp",
    "mp3", "wav", "flac", "aac", "ogg", "wma", "m4a",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "odt",
    "zip", "rar", "7z", "tar", "gz", "bz2",
    "html", "css", "js", "py", "java", "c", "cpp", "h",
    "db", "sqlite", "sql",
    "dmg", "iso", "img",
    "exe", "dll", "so", "dylib",
    "eml", "pst", "mbox",
]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def format_size(size_bytes: int) -> str:
    """Format bytes into a human-readable string."""
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024**3):.1f} GB"
    elif size_bytes >= 1024 ** 2:
        return f"{size_bytes / (1024**2):.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _section(title: str):
    """Print a styled section divider."""
    console.print()
    console.print(Rule(f"[bold {ACCENT}] {title} [/bold {ACCENT}]", style="dim cyan"))
    console.print()


def auto_tune_profile(profile: dict, found_files: list) -> dict:
    """Auto-adjust throttle settings based on detected file sizes."""
    tuned = profile.copy()
    actual_files = [f for f in found_files if not f.is_dir]
    if not actual_files:
        return tuned

    total_bytes = sum(f.size for f in actual_files)
    avg_size = total_bytes / len(actual_files)

    if avg_size > 500 * 1024 * 1024:
        tuned["burst_size"] = int(tuned["burst_size"] * 1.5)

    return tuned


def display_settings_summary(profile_name: str, settings: dict, total_bytes: int):
    """Display a panel summarizing the active throttle settings."""
    est_sleep_per_gb = (1024 * 1024 * 1024 / settings["burst_size"]) * settings["rest_duration"]
    total_gb = total_bytes / (1024 ** 3)
    est_extra_time = est_sleep_per_gb * total_gb

    p = PERFORMANCE_PROFILES[profile_name]

    lines = [
        f"  [bold {ACCENT}]{p['icon']}[/bold {ACCENT}]  [bold]{p['label']}[/bold] mode",
        "",
        f"  Chunk size        [bold]{format_size(settings['chunk_size'])}[/bold]",
        f"  Burst size        [bold]{format_size(settings['burst_size'])}[/bold]",
        f"  Rest duration     [bold]{settings['rest_duration']*1000:.0f}ms[/bold]",
        f"  Scan throttle     [bold]{settings['scan_throttle']*1000:.0f}ms[/bold] / inode" if settings['scan_throttle'] > 0 else "  Scan throttle     [bold]off[/bold]",
        f"  Thermal break     [{WARN}]every {settings['work_interval']//60}min[/{WARN}] for [{WARN}]{settings['break_duration']}s[/{WARN}]" if settings['work_interval'] else f"  Thermal break     [bold]off[/bold]",
        "",
        f"  [{DIM}]Est. overhead: ~{est_extra_time/60:.1f} min for {format_size(total_bytes)}[/{DIM}]",
    ]
    console.print(Panel(
        "\n".join(lines),
        title=f"[bold]Performance[/bold]",
        border_style="dim cyan",
        padding=(1, 2),
    ))


# ─── Header & Navigation ─────────────────────────────────────────────────────

LOGO = r"""
  _____ _____ ____  __  __ ___ _   _    _    _       ____  ____  ___ _     _
 |_   _| ____|  _ \|  \/  |_ _| \ | |  / \  | |     |  _ \|  _ \|_ _| |   | |
   | | |  _| | |_) | |\/| || ||  \| | / _ \ | |     | | | | |_) || || |   | |
   | | | |___|  _ <| |  | || || |\  |/ ___ \| |___  | |_| |  _ < | || |___| |___
   |_| |_____|_| \_\_|  |_|___|_| \_/_/   \_\_____| |____/|_| \_\___|_____|_____|
"""

def display_header():
    console.print()
    # Render logo lines with gradient-style coloring
    lines = LOGO.strip("\n").split("\n")
    colors = ["bright_cyan", "cyan", "blue", "bright_magenta", "magenta"]
    for i, line in enumerate(lines):
        color = colors[i % len(colors)]
        console.print(f"[{color}]{line}[/{color}]")
    console.print()
    console.print(f"  [{DIM}]v{VERSION}  --  Professional File Recovery for the Terminal[/{DIM}]")
    console.print(f"  [{DIM}]Recover deleted files, carve raw disk signatures, repair corruption[/{DIM}]")
    console.print()


def show_mode_selection():
    """Display mode selection and return the chosen mode."""
    table = Table(
        show_header=True,
        header_style=f"bold {ACCENT2}",
        box=box.ROUNDED,
        border_style="dim",
        padding=(0, 2),
        expand=True,
        title=f"[bold]Choose a Recovery Mode[/bold]",
        title_style="bold",
    )
    table.add_column("#", style=f"bold {ACCENT}", width=3, justify="center")
    table.add_column("Mode", style=f"bold", no_wrap=True)
    table.add_column("Description")
    table.add_column("Best For", style=f"{DIM}")

    table.add_row(
        "1",
        f"[{ACCENT}]quick[/{ACCENT}]",
        "Scan filesystem metadata for deleted files",
        "Recently deleted files on intact volumes",
    )
    table.add_row(
        "2",
        f"[{ACCENT}]deep[/{ACCENT}]",
        "Carve raw disk blocks for file signatures",
        "Formatted, corrupted, or unrecognizable volumes",
    )
    table.add_row(
        "3",
        f"[{ACCENT}]repair[/{ACCENT}]",
        "Diagnose and repair corrupted files",
        "Files recovered from any source",
    )

    console.print(table)
    console.print()
    choice = Prompt.ask(
        f"  [{ACCENT}]>[/{ACCENT}] Select mode",
        choices=["quick", "deep", "repair", "1", "2", "3"],
        default="quick",
    )
    return {"1": "quick", "2": "deep", "3": "repair"}.get(choice, choice)


def show_performance_modes():
    """Display performance modes as a compact table."""
    table = Table(
        show_header=True,
        header_style=f"bold {ACCENT2}",
        box=box.ROUNDED,
        border_style="dim",
        padding=(0, 1),
        title="[bold]Performance Mode[/bold]",
        title_style="bold",
    )
    table.add_column("Mode", style=f"bold {ACCENT}", no_wrap=True)
    table.add_column("Profile")
    table.add_column("Description", style=f"{DIM}")

    for key, p in PERFORMANCE_PROFILES.items():
        table.add_row(key, f"{p['icon']}  {p['label']}", p["description"])

    console.print(table)


def show_available_file_types():
    """Display available PhotoRec file types grouped by category."""
    groups = {
        "Images":     ["jpg", "png", "gif", "bmp", "tiff", "webp", "psd", "raw", "cr2", "nef"],
        "Video":      ["mp4", "mov", "avi", "mkv", "wmv", "flv", "webm", "m4v", "3gp"],
        "Audio":      ["mp3", "wav", "flac", "aac", "ogg", "wma", "m4a"],
        "Documents":  ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "odt"],
        "Archives":   ["zip", "rar", "7z", "tar", "gz", "bz2"],
        "Code/Web":   ["html", "css", "js", "py", "java", "c", "cpp", "h"],
        "Database":   ["db", "sqlite", "sql"],
        "Disk Images":["dmg", "iso", "img"],
        "System":     ["exe", "dll", "so", "dylib"],
        "Email":      ["eml", "pst", "mbox"],
    }

    table = Table(
        show_header=True,
        header_style=f"bold {ACCENT2}",
        box=box.ROUNDED,
        border_style="dim",
        padding=(0, 2),
        title="[bold]Supported File Types[/bold]",
        title_style="bold",
    )
    table.add_column("Category", style=f"bold {ACCENT}")
    table.add_column("Extensions")

    for category, exts in groups.items():
        table.add_row(category, f"[{DIM}]{', '.join(exts)}[/{DIM}]")

    console.print(table)


# ─── Disk Management ─────────────────────────────────────────────────────────

def unmount_disk(device_id: str) -> bool:
    """Unmount a disk. Returns True on success."""
    if "s" not in device_id:
        result = subprocess.run(["diskutil", "unmountDisk", device_id], capture_output=True)
    else:
        result = subprocess.run(["diskutil", "unmount", device_id], capture_output=True)
    return result.returncode == 0


def select_disk():
    _section("Disk Selection")

    try:
        disks = get_macos_disks()
    except subprocess.TimeoutExpired:
        console.print(Panel(
            "[bold red]Disk discovery timed out[/bold red] — diskutil is hanging.\n\n"
            "This usually happens when a faulty drive is connected.\n\n"
            "[bold]Troubleshooting:[/bold]\n"
            f"  1. Run [bold {ACCENT}]sudo pkill -f diskarbitrationd[/bold {ACCENT}] in another terminal\n"
            "  2. Unplug and replug the external drive\n"
            "  3. Enter the Disk ID manually below",
            title="[bold red]Disk Hang Detected[/bold red]",
            border_style="red",
            padding=(1, 2),
        ))

        manual_id = Prompt.ask(f"  [{ACCENT}]>[/{ACCENT}] Enter Disk ID manually (e.g. disk4), or Enter to exit")
        if not manual_id:
            sys.exit(1)
        from drill_engine.discovery import Disk
        return Disk(device_id=manual_id, name="Manual Entry", size=0, is_physical=True)

    if not disks:
        console.print(f"[{ERR}]No disks found. Are you running as root/sudo?[/{ERR}]")
        manual_id = Prompt.ask(f"  [{ACCENT}]>[/{ACCENT}] Enter Disk ID manually (e.g. disk4), or Enter to exit")
        if manual_id:
            from drill_engine.discovery import Disk
            return Disk(device_id=manual_id, name="Manual Entry", size=0, is_physical=True)
        sys.exit(1)

    table = Table(
        show_header=True,
        header_style=f"bold {ACCENT2}",
        box=box.ROUNDED,
        border_style="dim",
        padding=(0, 2),
        title="[bold]Available Drives[/bold]",
        title_style="bold",
    )
    table.add_column("Disk ID", style=f"bold {ACCENT}", no_wrap=True)
    table.add_column("Name", style="bold")
    table.add_column("Type", style=f"{DIM}")
    table.add_column("Size", justify="right")

    for d in disks:
        type_str = "Physical" if d.is_physical else "Logical"
        size_str = f"{d.size / (1024**3):.2f} GB"
        table.add_row(d.device_id, d.name, type_str, size_str)

    console.print(table)
    console.print()

    valid_ids = [d.device_id for d in disks]
    selected_id = Prompt.ask(f"  [{ACCENT}]>[/{ACCENT}] Select a Disk ID", choices=valid_ids)

    return next(d for d in disks if d.device_id == selected_id)


# ─── VFS Tree ────────────────────────────────────────────────────────────────

def _ensure_parent_node(nodes, parent_dir, tree):
    """Ensure a parent directory path exists in the VFS tree nodes dict."""
    if parent_dir in nodes:
        return
    parts = parent_dir.strip("/").split("/")
    current_path = ""
    for part in parts:
        if not part:
            continue
        current_path = current_path + "/" + part if current_path else "/" + part
        if current_path not in nodes:
            parent_path = os.path.dirname(current_path) or "/"
            if parent_path not in nodes:
                nodes[parent_path] = tree
            nodes[current_path] = nodes[parent_path].add(f"[bold blue]{part}/[/bold blue]")


def build_vfs_tree(files, root_name="Recovered Files"):
    tree = Tree(f"[bold {ACCENT}]{root_name}[/bold {ACCENT}]")
    nodes = {"/": tree}

    files.sort(key=lambda x: x.path)

    for f in files:
        if f.path == "/":
            continue

        parent_dir = os.path.dirname(f.path)
        if parent_dir == "":
            parent_dir = "/"

        _ensure_parent_node(nodes, parent_dir, tree)

        size_str = format_size(f.size)
        if f.is_deleted:
            label = f"[red]{f.name}[/red]  [{DIM}]{size_str}[/{DIM}]"
        else:
            label = f"{f.name}  [{DIM}]{size_str}[/{DIM}]"

        node = nodes[parent_dir].add(label)
        if f.is_dir:
            nodes[f.path] = node

    return tree


# ─── Quick Scan ───────────────────────────────────────────────────────────────

def run_quick_scan(disk):
    """Run the quick scan flow. Returns True if user wants to scan again."""
    device_path = f"/dev/{disk.device_id}"

    _section("Quick Scan")

    show_performance_modes()
    console.print()
    perf_mode = Prompt.ask(
        f"  [{ACCENT}]>[/{ACCENT}] Select performance mode",
        choices=["fast", "balanced", "cool", "siberia"],
        default="balanced",
    )
    profile = PERFORMANCE_PROFILES[perf_mode].copy()

    console.print(f"\n  [{DIM}]Unmounting {disk.device_id} for raw access...[/{DIM}]")
    if not unmount_disk(disk.device_id):
        console.print(f"  [{WARN}]Could not unmount {disk.device_id} — scan may fail[/{WARN}]")

    global _cleanup_disk_id
    _cleanup_disk_id = disk.device_id

    scanner = TSKScanner(
        device_path,
        scan_throttle=profile["scan_throttle"],
        chunk_size=profile["chunk_size"],
        burst_size=profile["burst_size"],
        rest_duration=profile["rest_duration"],
    )

    try:
        with Progress(
            SpinnerColumn(style=f"{ACCENT}"),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
        ) as progress:
            progress.add_task(description=f"  Scanning {device_path}...", total=None)

            if not scanner.open():
                console.print(f"\n  [{ERR}]Failed to open filesystem on {device_path}.[/{ERR}]")
                console.print(f"  [{DIM}]Ensure you are running with sudo and the disk is valid.[/{DIM}]")
                return False

            found_files = scanner.quick_scan()
    finally:
        console.print(f"  [{DIM}]Remounting {disk.device_id}...[/{DIM}]")
        subprocess.run(["diskutil", "mount", disk.device_id], capture_output=True)
        _cleanup_disk_id = None

    # Results
    _section("Scan Results")

    if not found_files:
        console.print(Panel(
            "No deleted files were found on this volume.\n\n"
            f"[{DIM}]The filesystem may have been overwritten, or files may have been\n"
            f"securely erased. Try a [bold]deep scan[/bold] for signature-based recovery.[/{DIM}]",
            title="[bold yellow]Nothing Found[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        ))
        return True

    file_count = sum(1 for f in found_files if not f.is_dir)
    total_size = sum(f.size for f in found_files if not f.is_dir)
    console.print(Panel(
        f"  Found [bold green]{file_count}[/bold green] deleted files  ([bold]{format_size(total_size)}[/bold] total)",
        border_style="green",
        padding=(0, 1),
    ))
    console.print()

    vfs_tree = build_vfs_tree(found_files, root_name=device_path)
    console.print(vfs_tree)
    console.print()

    if not Confirm.ask(f"  [{ACCENT}]>[/{ACCENT}] Extract these files?"):
        return True

    # File filtering
    file_input = Prompt.ask(
        f"  [{ACCENT}]>[/{ACCENT}] Filenames to extract (comma-separated) or 'all'",
        default="all",
    )

    if file_input.lower() != "all":
        requested_names = [f.strip() for f in file_input.split(",")]
        files_to_extract = [f for f in found_files if f.is_dir or f.name in requested_names]
    else:
        files_to_extract = found_files

    # Sort order
    order_choice = Prompt.ask(
        f"  [{ACCENT}]>[/{ACCENT}] Extraction order",
        choices=["current", "asc", "desc"],
        default="current",
    )

    dirs = [f for f in files_to_extract if f.is_dir]
    files = [f for f in files_to_extract if not f.is_dir]

    if order_choice == "asc":
        files.sort(key=lambda x: x.size)
    elif order_choice == "desc":
        files.sort(key=lambda x: x.size, reverse=True)

    files_to_extract = dirs + files

    if not any(not f.is_dir for f in files_to_extract):
        console.print(f"  [{WARN}]No files matched your filter.[/{WARN}]")
        return True

    out_dir = Prompt.ask(f"  [{ACCENT}]>[/{ACCENT}] Destination path", default="./recovered_files")
    os.makedirs(out_dir, exist_ok=True)

    total_bytes = sum(f.size for f in files_to_extract if not f.is_dir)

    tuned = auto_tune_profile(profile, files_to_extract)
    scanner.chunk_size = tuned["chunk_size"]
    scanner.burst_size = tuned["burst_size"]
    scanner.rest_duration = tuned["rest_duration"]

    console.print()
    display_settings_summary(perf_mode, tuned, total_bytes)

    _section("Extracting Files")

    with Progress(
        SpinnerColumn(style=f"{ACCENT}"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=40, complete_style=f"{ACCENT}", finished_style="green"),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        DownloadColumn(),
    ) as progress:
        total_task = progress.add_task(f"[bold]Extracting {file_count} files...", total=total_bytes)

        work_start_time = time.time()

        for f in files_to_extract:
            if not f.is_dir:
                file_task = progress.add_task(f"  [{DIM}]{f.name}[/{DIM}]", total=f.size)

                def update_progress(bytes_written):
                    progress.advance(total_task, bytes_written)
                    progress.advance(file_task, bytes_written)

                scanner.extract_file(f, out_dir, progress_callback=update_progress)

                progress.remove_task(file_task)

                # Thermal break
                if tuned["work_interval"] and (time.time() - work_start_time) >= tuned["work_interval"]:
                    progress.print(f"\n  [{WARN}]Thermal break: cooling for {tuned['break_duration']}s...[/{WARN}]")
                    for remaining in range(tuned["break_duration"], 0, -1):
                        progress.update(total_task, description=f"[{WARN}]Cooling... {remaining}s[/{WARN}]")
                        time.sleep(1)
                    progress.update(total_task, description=f"[bold]Extracting files...")
                    work_start_time = time.time()
                    progress.print(f"  [green]Cooling complete. Resuming...[/green]\n")

    console.print(f"\n  All files saved to [bold green]{os.path.abspath(out_dir)}[/bold green]")

    # ── Sanity Check ──
    _section("Sanity Check")

    verified_count = 0
    failed_file_metas = []

    for f in files_to_extract:
        if f.is_dir:
            continue

        rel_path = f.path.lstrip("/")
        if rel_path.startswith(".Trashes"):
            rel_path = rel_path.replace(".Trashes", "Recovered_Trash", 1)
        full_dest_path = os.path.join(out_dir, rel_path)

        if os.path.exists(full_dest_path):
            actual_size = os.path.getsize(full_dest_path)
            if actual_size == f.size:
                verified_count += 1
            else:
                failed_file_metas.append(f)
        else:
            failed_file_metas.append(f)

    if not failed_file_metas:
        console.print(Panel(
            f"  All [bold green]{verified_count}[/bold green] files match their expected sizes.",
            title="[bold green]Verification Passed[/bold green]",
            border_style="green",
            padding=(0, 1),
        ))
    else:
        console.print(f"  [{WARN}]{len(failed_file_metas)} file(s) need re-extraction...[/{WARN}]\n")

        with Progress(
            SpinnerColumn(style="yellow"),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=40, complete_style="yellow", finished_style="green"),
            TaskProgressColumn(),
            DownloadColumn(),
        ) as retry_progress:
            retry_total = sum(f.size for f in failed_file_metas)
            retry_task = retry_progress.add_task("[bold yellow]Re-extracting...", total=retry_total)

            for f in failed_file_metas:
                rel_path = f.path.lstrip("/")
                if rel_path.startswith(".Trashes"):
                    rel_path = rel_path.replace(".Trashes", "Recovered_Trash", 1)
                bad_path = os.path.join(out_dir, rel_path)
                if os.path.exists(bad_path):
                    os.remove(bad_path)

                def retry_cb(bytes_written):
                    retry_progress.advance(retry_task, bytes_written)

                scanner.extract_file(f, out_dir, progress_callback=retry_cb)

        # Second-pass verification
        still_failed = []
        for f in failed_file_metas:
            rel_path = f.path.lstrip("/")
            if rel_path.startswith(".Trashes"):
                rel_path = rel_path.replace(".Trashes", "Recovered_Trash", 1)
            full_dest_path = os.path.join(out_dir, rel_path)

            if os.path.exists(full_dest_path) and os.path.getsize(full_dest_path) == f.size:
                verified_count += 1
            else:
                actual = os.path.getsize(full_dest_path) if os.path.exists(full_dest_path) else 0
                still_failed.append((f.name, f.size, actual))

        if not still_failed:
            console.print(Panel(
                f"  All [bold green]{len(failed_file_metas)}[/bold green] files recovered on retry!",
                title="[bold green]Verification Passed[/bold green]",
                border_style="green",
                padding=(0, 1),
            ))
        else:
            console.print(f"\n  [{ERR}]{len(still_failed)} file(s) could not be fully recovered:[/{ERR}]\n")
            error_table = Table(
                show_header=True,
                header_style="bold red",
                box=box.SIMPLE,
                padding=(0, 2),
            )
            error_table.add_column("Filename", style="bold")
            error_table.add_column("Expected", justify="right")
            error_table.add_column("Actual", justify="right")
            for name, expected, actual in still_failed:
                error_table.add_row(name, format_size(expected), format_size(actual))
            console.print(error_table)
            console.print(f"  [{DIM}]These files may be too corrupted to recover at their original size.[/{DIM}]")

    # Post-extraction repair offer
    console.print()
    if Confirm.ask(f"  [{ACCENT}]>[/{ACCENT}] Scan recovered files for corruption and attempt repairs?", default=True):
        run_repair_flow(out_dir)

    return True


# ─── Deep Scan ────────────────────────────────────────────────────────────────

def run_deep_scan(disk):
    """Run the deep scan flow. Returns True if user wants to scan again."""
    _section("Deep Scan (PhotoRec)")

    device_path = f"/dev/{disk.device_id}"
    scanner = DeepScanner(device_path)

    if not scanner.check_photorec_installed():
        console.print(Panel(
            "PhotoRec is required for deep scanning but was not found.\n\n"
            f"  Install it with:  [bold {ACCENT}]brew install testdisk[/bold {ACCENT}]",
            title=f"[{ERR}]Missing Dependency[/{ERR}]",
            border_style="red",
            padding=(1, 2),
        ))
        return False

    ok, err = scanner.check_device_accessible()
    if not ok:
        console.print(f"  [{ERR}]{err}[/{ERR}]")
        return False

    out_dir = Prompt.ask(f"  [{ACCENT}]>[/{ACCENT}] Destination path for recovered fragments", default="./recovered_files")

    ok, info = scanner.check_output_space(out_dir)
    if not ok:
        console.print(f"  [{ERR}]{info}[/{ERR}]")
        return False
    console.print(f"  [{DIM}]Output: {os.path.abspath(out_dir)} ({info})[/{DIM}]")

    # Scan scope
    console.print()
    scan_scope = Prompt.ask(
        f"  [{ACCENT}]>[/{ACCENT}] Scan scope",
        choices=["wholespace", "freespace"],
        default="wholespace",
    )
    scan_freespace_only = scan_scope == "freespace"

    # File types
    console.print(f"\n  [{DIM}]Leave blank to recover all known file types.[/{DIM}]")
    file_types_input = Prompt.ask(
        f"  [{ACCENT}]>[/{ACCENT}] File types (comma-separated, e.g. jpg,png,mp4) or 'list'",
        default="",
    ).strip()

    if file_types_input.lower() == "list":
        console.print()
        show_available_file_types()
        file_types_input = Prompt.ask(
            f"\n  [{ACCENT}]>[/{ACCENT}] File types (comma-separated) or blank for all",
            default="",
        ).strip()

    file_types = [t.strip().lstrip(".") for t in file_types_input.split(",") if t.strip()] if file_types_input else None

    if file_types:
        console.print(f"  [{DIM}]Recovering: {', '.join(file_types)}[/{DIM}]")
    else:
        console.print(f"  [{DIM}]Recovering all known file types[/{DIM}]")

    console.print(f"\n  [{DIM}]Unmounting {disk.device_id} for deep scan...[/{DIM}]")
    if not unmount_disk(disk.device_id):
        console.print(f"  [{WARN}]Could not unmount {disk.device_id} — scan may fail[/{WARN}]")

    global _cleanup_disk_id
    _cleanup_disk_id = disk.device_id

    from rich.live import Live

    _section("Scanning")

    try:
        output_lines = []
        with Live(
            Panel(f"[{DIM}]Starting PhotoRec...[/{DIM}]", title=f"[bold {ACCENT}]Deep Scan[/bold {ACCENT}]", border_style="dim cyan", padding=(1, 2)),
            refresh_per_second=4,
        ) as live:
            def output_callback(line):
                display = line.strip()
                if display:
                    output_lines.append(display)
                    # Show last 6 lines for context
                    visible = output_lines[-6:]
                    content = "\n".join(visible)
                    live.update(Panel(content, title=f"[bold {ACCENT}]Deep Scan[/bold {ACCENT}]", border_style="dim cyan", padding=(1, 2)))

            success, stats = scanner.run_deep_scan(
                out_dir,
                output_callback=output_callback,
                file_types=file_types,
                scan_freespace_only=scan_freespace_only,
            )
    except KeyboardInterrupt:
        scanner.cancel()
        success = False
        stats = None
    finally:
        console.print(f"  [{DIM}]Remounting {disk.device_id}...[/{DIM}]")
        subprocess.run(["diskutil", "mount", disk.device_id], capture_output=True)
        _cleanup_disk_id = None

    # Results
    _section("Deep Scan Results")

    if success and stats and stats.total_files > 0:
        console.print(Panel(
            f"  Recovered [bold green]{stats.total_files}[/bold green] files  ([bold]{format_size(stats.total_bytes)}[/bold])  in [bold]{_format_duration(stats.duration_seconds)}[/bold]",
            border_style="green",
            padding=(0, 1),
        ))
        console.print()

        if stats.by_type:
            type_table = Table(
                show_header=True,
                header_style=f"bold {ACCENT2}",
                box=box.ROUNDED,
                border_style="dim",
                padding=(0, 2),
                title="[bold]Recovered File Types[/bold]",
                title_style="bold",
            )
            type_table.add_column("Extension", style=f"bold {ACCENT}")
            type_table.add_column("Count", style="bold", justify="right")

            for ext, count in sorted(stats.by_type.items(), key=lambda x: -x[1]):
                type_table.add_row(ext, str(count))

            console.print(type_table)

        console.print(f"\n  [{DIM}]Files saved to: {os.path.abspath(out_dir)}[/{DIM}]")
        console.print(f"  [{DIM}]Note: Deep scans cannot reconstruct original filenames or folder structures.[/{DIM}]")

        console.print()
        if Confirm.ask(f"  [{ACCENT}]>[/{ACCENT}] Scan recovered files for corruption and attempt repairs?", default=False):
            run_repair_flow(out_dir)
    elif success and stats and stats.total_files == 0:
        console.print(Panel(
            "No files were recovered.\n\n"
            f"[{DIM}]The disk may have been fully overwritten or the selected file types\n"
            f"were not present in the scanned area.[/{DIM}]",
            title=f"[{WARN}]No Results[/{WARN}]",
            border_style="yellow",
            padding=(1, 2),
        ))
    else:
        console.print(Panel(
            "The deep scan failed or was cancelled.\n" +
            ("\n".join(f"  - {err}" for err in stats.errors) if stats and stats.errors else ""),
            title=f"[{ERR}]Scan Failed[/{ERR}]",
            border_style="red",
            padding=(1, 2),
        ))

    return True


# ─── Repair Flow ──────────────────────────────────────────────────────────────

def run_repair_flow(directory: str):
    """Diagnose and repair corrupted files in the given directory."""
    _section("File Repair")

    console.print(f"  Scanning [bold {ACCENT}]{os.path.abspath(directory)}[/bold {ACCENT}]...\n")

    cache_file = os.path.join(directory, ".drill_repair_cache.json")
    corrupt_files = None

    if os.path.exists(cache_file):
        if Confirm.ask(f"  [{ACCENT}]>[/{ACCENT}] Found a previous diagnosis cache. Use it?", default=True):
            corrupt_files = load_scan_cache(cache_file)
            console.print(f"  [{DIM}]Loaded {len(corrupt_files)} results from cache.[/{DIM}]")

    if corrupt_files is None:
        with Progress(
            SpinnerColumn(style=f"{ACCENT}"),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=40, complete_style=f"{ACCENT}", finished_style="green"),
            TaskProgressColumn(),
            TimeRemainingColumn(),
        ) as progress:
            scan_task = progress.add_task("  Diagnosing files...", total=None)

            def on_scan(filepath, current, total):
                progress.update(scan_task, total=total, completed=current,
                                description=f"  Diagnosing [{current}/{total}] {os.path.basename(filepath)}")

            corrupt_files = repair_scan_dir(directory, progress_callback=on_scan)
            save_scan_cache(corrupt_files, cache_file)

    if not corrupt_files:
        console.print(Panel(
            "  All files look healthy. No corruption detected.",
            title="[bold green]All Clear[/bold green]",
            border_style="green",
            padding=(0, 1),
        ))
        return

    console.print(f"\n  [{WARN}]Found {len(corrupt_files)} file(s) with potential issues:[/{WARN}]\n")

    diag_table = Table(
        show_header=True,
        header_style=f"bold {ACCENT2}",
        box=box.ROUNDED,
        border_style="dim",
        padding=(0, 1),
        expand=True,
    )
    diag_table.add_column("#", style=f"{DIM}", width=4, justify="right")
    diag_table.add_column("File", style=f"bold {ACCENT}", no_wrap=True, max_width=40)
    diag_table.add_column("Type", width=6)
    diag_table.add_column("Size", justify="right", width=10)
    diag_table.add_column("Issues", style="yellow")

    for i, r in enumerate(corrupt_files, 1):
        issues_str = ", ".join(i_type.value for i_type in r.issues)
        diag_table.add_row(
            str(i),
            os.path.basename(r.filepath),
            r.file_type.value.upper(),
            format_size(r.file_size),
            issues_str,
        )

    console.print(diag_table)

    console.print()
    if not Confirm.ask(f"  [{ACCENT}]>[/{ACCENT}] Attempt to repair these files?", default=True):
        console.print(f"  [{DIM}]Repair skipped.[/{DIM}]")
        return

    reference_video = None
    if any(r.file_type in (FileType.MP4, FileType.MOV) for r in corrupt_files):
        console.print(Panel(
            "Severely corrupted videos can be repaired using a 'reference video'\n"
            "recorded on the same device or software.\n\n"
            f"[{DIM}]This is optional — basic ffmpeg repair will be attempted either way.[/{DIM}]",
            title=f"[{WARN}]Advanced Video Repair[/{WARN}]",
            border_style="yellow",
            padding=(1, 2),
        ))

        ref_input = Prompt.ask(f"  [{ACCENT}]>[/{ACCENT}] Path to reference video (Enter to skip)")
        if ref_input:
            ref_input = ref_input.strip("'\"").strip()

        if ref_input and os.path.isfile(ref_input):
            reference_video = ref_input
        elif ref_input:
            console.print(f"  [{ERR}]File not found: {ref_input}. Continuing without reference.[/{ERR}]")

    _section("Repairing")

    with Progress(
        SpinnerColumn(style="green"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=40, complete_style="green", finished_style="bold green"),
        TaskProgressColumn(),
    ) as progress:
        repair_task = progress.add_task("  Repairing files...", total=len(corrupt_files))

        def on_repair(result, current, total):
            progress.update(repair_task, completed=current,
                            description=f"  Repairing [{current}/{total}] {os.path.basename(result.filepath)}")

        repair_batch(corrupt_files, progress_callback=on_repair, reference_video=reference_video)

    # Summary
    _section("Repair Results")

    repaired = [r for r in corrupt_files if r.status == RepairStatus.REPAIRED]
    partial = [r for r in corrupt_files if r.status == RepairStatus.PARTIALLY_REPAIRED]
    failed = [r for r in corrupt_files if r.status == RepairStatus.UNREPAIRABLE]

    summary_table = Table(
        show_header=True,
        header_style=f"bold {ACCENT2}",
        box=box.ROUNDED,
        border_style="dim",
        padding=(0, 2),
        expand=True,
        title="[bold]Repair Summary[/bold]",
        title_style="bold",
    )
    summary_table.add_column("File", style=f"bold {ACCENT}", no_wrap=True, max_width=40)
    summary_table.add_column("Status")
    summary_table.add_column("Output", style=f"{DIM}", max_width=40)

    for r in corrupt_files:
        if r.status == RepairStatus.REPAIRED:
            status_str = "[bold green]Repaired[/bold green]"
        elif r.status == RepairStatus.PARTIALLY_REPAIRED:
            status_str = "[bold yellow]Partial[/bold yellow]"
        else:
            status_str = "[bold red]Failed[/bold red]"

        repaired_name = os.path.basename(r.repaired_path) if r.repaired_path else "-"
        summary_table.add_row(os.path.basename(r.filepath), status_str, repaired_name)

    console.print(summary_table)
    console.print()

    # Stats line
    parts = []
    if repaired:
        parts.append(f"[bold green]{len(repaired)} repaired[/bold green]")
    if partial:
        parts.append(f"[bold yellow]{len(partial)} partial[/bold yellow]")
    if failed:
        parts.append(f"[bold red]{len(failed)} failed[/bold red]")

    console.print(f"  {' | '.join(parts)}")
    console.print(f"\n  [{DIM}]Repaired files are saved alongside originals with a '.repaired' suffix.[/{DIM}]\n")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    global _cleanup_disk_id

    if os.geteuid() != 0:
        console.print(Panel(
            f"Terminal Drill requires [bold]root privileges[/bold] to read raw disk blocks.\n\n"
            f"  Run with:  [bold {ACCENT}]sudo venv/bin/python3 drill_ui/app.py[/bold {ACCENT}]",
            title=f"[{ERR}]Permission Denied[/{ERR}]",
            border_style="red",
            padding=(1, 2),
        ))
        sys.exit(1)

    # Lower process priority to reduce CPU heat
    try:
        os.nice(10)
    except OSError:
        pass  # May fail if already niced or permissions issue

    while True:
        display_header()

        scan_type = show_mode_selection()

        if scan_type == "repair":
            repair_dir = Prompt.ask(f"\n  [{ACCENT}]>[/{ACCENT}] Path to the directory of files to repair", default="./recovered_files")
            if not os.path.isdir(repair_dir):
                console.print(f"  [{ERR}]Directory not found: {repair_dir}[/{ERR}]")
                if not Confirm.ask(f"  [{ACCENT}]>[/{ACCENT}] Try another directory?"):
                    break
                continue
            run_repair_flow(repair_dir)
        else:
            disk = select_disk()
            console.print(f"\n  [bold]Selected:[/bold] [{ACCENT}]{disk.device_id}[/{ACCENT}] ({disk.name})")

            if scan_type == "quick":
                run_quick_scan(disk)
            elif scan_type == "deep":
                run_deep_scan(disk)

        console.print()
        if not Confirm.ask(f"  [{ACCENT}]>[/{ACCENT}] Run another scan?", default=False):
            break

    console.print()
    console.print(Panel(
        f"  [{DIM}]Thank you for using Terminal Drill. Your files are in good hands.[/{DIM}]",
        border_style="dim cyan",
        padding=(0, 1),
    ))
    console.print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print(f"\n  [{DIM}]Interrupted. Exiting...[/{DIM}]\n")
