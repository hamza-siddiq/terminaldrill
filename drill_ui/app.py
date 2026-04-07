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
from drill_engine.discovery import get_macos_disks
from drill_engine.quick_scan import TSKScanner
from drill_engine.deep_scan import DeepScanner, _format_duration
from drill_engine.file_repair import scan_directory as repair_scan_dir, repair_batch, RepairStatus, RepairResult, FileType, save_scan_cache, load_scan_cache
import subprocess
import time

console = Console()

VERSION = "1.0.0"

# Global cleanup state: tracks the disk ID that needs remounting if the app exits unexpectedly
_cleanup_disk_id = None

def _ensure_disk_remounted():
    """Atexit handler: remount the disk if the app exits while it's unmounted."""
    global _cleanup_disk_id
    if _cleanup_disk_id:
        console.print(f"\n[yellow]Safety remount: remounting {_cleanup_disk_id}...[/yellow]")
        subprocess.run(["diskutil", "mount", _cleanup_disk_id], capture_output=True)
        _cleanup_disk_id = None

atexit.register(_ensure_disk_remounted)

# Performance profiles: tuned throttle settings for different workloads
PERFORMANCE_PROFILES = {
    "fast": {
        "label": "Fast",
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

def format_size(size_bytes: int) -> str:
    """Format bytes into a human-readable string."""
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024**3):.1f} GB"
    elif size_bytes >= 1024 ** 2:
        return f"{size_bytes / (1024**2):.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"

def auto_tune_profile(profile: dict, found_files: list) -> dict:
    """Auto-adjust throttle settings based on detected file sizes."""
    tuned = profile.copy()
    actual_files = [f for f in found_files if not f.is_dir]
    if not actual_files:
        return tuned
    
    total_bytes = sum(f.size for f in actual_files)
    avg_size = total_bytes / len(actual_files)
    
    # Large avg file size (>500MB): bigger bursts are more efficient
    if avg_size > 500 * 1024 * 1024:
        tuned["burst_size"] = int(tuned["burst_size"] * 1.5)
    
    return tuned

def display_settings_summary(profile_name: str, settings: dict, total_bytes: int):
    """Display a panel summarizing the active throttle settings."""
    est_sleep_per_gb = (1024 * 1024 * 1024 / settings["burst_size"]) * settings["rest_duration"]
    total_gb = total_bytes / (1024 ** 3)
    est_extra_time = est_sleep_per_gb * total_gb
    
    lines = [
        f"[bold]{PERFORMANCE_PROFILES[profile_name]['label']}[/bold] mode",
        f"Chunk size: [cyan]{format_size(settings['chunk_size'])}[/cyan]",
        f"Burst size: [cyan]{format_size(settings['burst_size'])}[/cyan]",
        f"Rest duration: [cyan]{settings['rest_duration']*1000:.0f}ms[/cyan]",
        f"Scan throttle: [cyan]{settings['scan_throttle']*1000:.0f}ms[/cyan] per inode" if settings['scan_throttle'] > 0 else "Scan throttle: [cyan]off[/cyan]",
        f"Thermal break: [yellow]Every {settings['work_interval']//60}min[/yellow] for [yellow]{settings['break_duration']}s[/yellow]" if settings['work_interval'] else "Thermal break: [cyan]off[/cyan]",
        f"Est. throttle overhead: [yellow]~{est_extra_time/60:.1f} min[/yellow] for {format_size(total_bytes)}",
    ]
    console.print(Panel("\n".join(lines), title="[bold]Performance Settings[/bold]", border_style="dim"))

def display_header():
    console.print("")
    console.print("[bold cyan]TERMINAL DRILL[/bold cyan]")
    console.print(f"[dim]v{VERSION} — Professional File Recovery for the Terminal[/dim]\n")

def show_mode_selection():
    """Display a rich table for mode selection and return the chosen mode."""
    table = Table(
        title="Select Recovery Mode",
        show_header=True,
        header_style="bold magenta",
        expand=True,
    )
    table.add_column("Mode", style="cyan", no_wrap=True)
    table.add_column("Description", style="green")
    table.add_column("Best For", style="yellow")
    
    table.add_row(
        "quick",
        "Scan filesystem metadata for deleted files",
        "Recently deleted files on intact volumes",
    )
    table.add_row(
        "deep",
        "Carve raw disk blocks for file signatures (PhotoRec)",
        "Formatted, corrupted, or unrecognizable volumes",
    )
    table.add_row(
        "repair",
        "Diagnose and repair corrupted files",
        "Files from any recovery source",
    )
    
    console.print(table)
    console.print("")
    return Prompt.ask("Mode", choices=["quick", "deep", "repair"], default="quick")

def show_performance_modes():
    """Display performance modes as a compact table."""
    table = Table(
        title="Performance Mode",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Mode", style="cyan", no_wrap=True)
    table.add_column("Label")
    table.add_column("Description", style="dim")
    
    for key, p in PERFORMANCE_PROFILES.items():
        table.add_row(key, p["label"], p["description"])
    
    console.print(table)

def show_available_file_types():
    """Display available PhotoRec file types grouped by category."""
    groups = {
        "Images": ["jpg", "png", "gif", "bmp", "tiff", "webp", "psd", "raw", "cr2", "nef"],
        "Video": ["mp4", "mov", "avi", "mkv", "wmv", "flv", "webm", "m4v", "3gp"],
        "Audio": ["mp3", "wav", "flac", "aac", "ogg", "wma", "m4a"],
        "Documents": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "odt"],
        "Archives": ["zip", "rar", "7z", "tar", "gz", "bz2"],
        "Code/Web": ["html", "css", "js", "py", "java", "c", "cpp", "h"],
        "Database": ["db", "sqlite", "sql"],
        "Disk Images": ["dmg", "iso", "img"],
        "System": ["exe", "dll", "so", "dylib"],
        "Email": ["eml", "pst", "mbox"],
    }
    
    table = Table(
        title="Available File Types",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Category", style="cyan")
    table.add_column("Extensions", style="green")
    
    for category, exts in groups.items():
        table.add_row(category, ", ".join(exts))
    
    console.print(table)

def unmount_disk(device_id: str) -> bool:
    """Unmount a disk, using unmountDisk for physical disks and unmount for volumes.
    Returns True on success, False on failure."""
    # Physical disks (diskN) need unmountDisk, volumes (diskNsN) need unmount
    if "s" not in device_id:
        result = subprocess.run(["diskutil", "unmountDisk", device_id], capture_output=True)
    else:
        result = subprocess.run(["diskutil", "unmount", device_id], capture_output=True)
    return result.returncode == 0

def select_disk():
    try:
        disks = get_macos_disks()
    except subprocess.TimeoutExpired:
        console.print("\n[bold red]Error: Disk discovery timed out (diskutil is hanging).[/bold red]")
        console.print(Panel(
            "[yellow]This usually happens when a faulty drive is connected and macOS stops responding while trying to read it.[/yellow]\n\n"
            "[bold white]Troubleshooting Steps:[/bold white]\n"
            "1. Try running: [bold cyan]sudo pkill -f diskarbitrationd[/bold cyan] in another terminal.\n"
            "2. If it's an external drive, try unplugging and replugging it.\n"
            "3. If you already know the Disk ID (e.g., disk4), you can enter it manually below.",
            title="Disk Hang Detected",
            border_style="red"
        ))
        
        manual_id = Prompt.ask("Enter [bold cyan]Disk ID[/bold cyan] manually (e.g., disk4) or press Enter to exit")
        if not manual_id:
            sys.exit(1)
        # Create a dummy disk object for manual entry
        from drill_engine.discovery import Disk
        return Disk(device_id=manual_id, name="Manual Entry", size=0, is_physical=True)

    if not disks:
        console.print("[red]No disks found. Are you running as root/sudo?[/red]")
        # Offer manual entry even if list is empty
        manual_id = Prompt.ask("Enter [bold cyan]Disk ID[/bold cyan] manually (e.g., disk4) or press Enter to exit")
        if manual_id:
            from drill_engine.discovery import Disk
            return Disk(device_id=manual_id, name="Manual Entry", size=0, is_physical=True)
        sys.exit(1)

    table = Table(title="Available Drives to Recover", show_header=True, header_style="bold magenta")
    table.add_column("Disk ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="green")
    table.add_column("Type")
    table.add_column("Size", justify="right")

    for d in disks:
        type_str = "Physical" if d.is_physical else " Logical"
        size_gb = f"{d.size / (1024**3):.2f} GB"
        table.add_row(d.device_id, d.name, type_str, size_gb)

    console.print(table)
    
    valid_ids = [d.device_id for d in disks]
    selected_id = Prompt.ask("Enter the [bold cyan]Disk ID[/bold cyan] you want to scan", choices=valid_ids)
    
    return next(d for d in disks if d.device_id == selected_id)

def _ensure_parent_node(nodes, parent_dir, tree):
    """Ensure a parent directory path exists in the VFS tree nodes dict.
    Creates any missing intermediate directories."""
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
            nodes[current_path] = nodes[parent_path].add(f"[bold blue]{part}[/bold blue]")

def build_vfs_tree(files, root_name="Recovered Files"):
    tree = Tree(f"[bold cyan]{root_name}[/bold cyan]")
    nodes = {"/": tree}
    
    # Sort paths
    files.sort(key=lambda x: x.path)
    
    for f in files:
        if f.path == "/":
            continue
        
        parent_dir = os.path.dirname(f.path)
        if parent_dir == "":
            parent_dir = "/"
        
        # Build intermediate paths
        _ensure_parent_node(nodes, parent_dir, tree)
            
        # Formatting
        size_str = f" ({f.size / 1024:.2f} KB)" if f.size < 1024 * 1024 else f" ({f.size / (1024*1024):.2f} MB)"
        label = f"[dim red][DELETED][/dim red] {f.name}{size_str}" if f.is_deleted else f"{f.name}{size_str}"
        
        node = nodes[parent_dir].add(label)
        if f.is_dir:
            nodes[f.path] = node
            
    return tree

def run_quick_scan(disk):
    """Run the quick scan flow. Returns True if user wants to scan again."""
    device_path = f"/dev/{disk.device_id}"
    
    # Show performance modes
    show_performance_modes()
    perf_mode = Prompt.ask("Select performance mode", choices=["fast", "balanced", "cool", "siberia"], default="balanced")
    profile = PERFORMANCE_PROFILES[perf_mode].copy()
    
    # macOS prevents raw block access if the volume is currently mounted
    console.print(f"\n[dim]Unmounting {disk.device_id} for raw access...[/dim]")
    if not unmount_disk(disk.device_id):
        console.print(f"[yellow]Warning: Failed to unmount {disk.device_id}, scan may fail[/yellow]")
    
    # Register disk for safety remount in case of unexpected exit (Ctrl+C, crash, etc.)
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
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
        ) as progress:
            progress.add_task(description=f"Scanning {device_path}...", total=None)
            
            if not scanner.open():
                console.print(f"[red]Failed to open filesystem on {device_path}. Need sudo/root permissions?[/red]")
                return False
                
            found_files = scanner.quick_scan()
    finally:
        # Always remount the disk so it reappears in Finder
        console.print(f"[dim]Remounting {disk.device_id}...[/dim]")
        subprocess.run(["diskutil", "mount", disk.device_id], capture_output=True)
        _cleanup_disk_id = None
        
    console.print(f"[bold green]Scan Complete![/bold green] Found {len(found_files)} deleted files.\n")
    
    if not found_files:
        console.print("[yellow]No deleted files found on this volume.[/yellow]")
        return True
    
    vfs_tree = build_vfs_tree(found_files, root_name=device_path)
    console.print(vfs_tree)
    console.print("")
    
    if not Confirm.ask("Do you want to extract these files?"):
        return True
    
    # Ask for specific files
    file_input = Prompt.ask(
        "Enter specific filenames to extract (comma-separated) or 'all' to extract everything",
        default="all"
    )
    
    if file_input.lower() != "all":
        requested_names = [f.strip() for f in file_input.split(",")]
        # Filter found_files based on requested names
        # Retain directories as they might be needed for the path
        files_to_extract = [f for f in found_files if f.is_dir or f.name in requested_names]
    else:
        files_to_extract = found_files

    # Ask for extraction order
    order_choice = Prompt.ask(
        "Select extraction order",
        choices=["current", "asc", "desc"],
        default="current"
    )
    
    # Separate files and directories since sorting directories by size doesn't make sense for extraction order
    dirs = [f for f in files_to_extract if f.is_dir]
    files = [f for f in files_to_extract if not f.is_dir]
    
    if order_choice == "asc":
        files.sort(key=lambda x: x.size)
    elif order_choice == "desc":
        files.sort(key=lambda x: x.size, reverse=True)
        
    files_to_extract = dirs + files

    if not any(not f.is_dir for f in files_to_extract):
         console.print("[yellow]No files matched your criteria to extract.[/yellow]")
         return True
         
    out_dir = Prompt.ask("Enter destination path", default="./recovered_files")
    os.makedirs(out_dir, exist_ok=True)
    
    total_bytes = sum(f.size for f in files_to_extract if not f.is_dir)
    
    # Auto-tune settings based on actual file sizes
    tuned = auto_tune_profile(profile, files_to_extract)
    scanner.chunk_size = tuned["chunk_size"]
    scanner.burst_size = tuned["burst_size"]
    scanner.rest_duration = tuned["rest_duration"]
    
    # Show final settings
    display_settings_summary(perf_mode, tuned, total_bytes)
    console.print("")
    
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        DownloadColumn(),
        transient=True
    ) as progress:
        # Overall progress bar
        total_task = progress.add_task(f"[bold cyan]Total Extraction Progress...", total=total_bytes)
        
        # Time-based thermal break tracking — only actual work time counts
        work_start_time = time.time()
        
        for f in files_to_extract:
            if not f.is_dir:
                # Per-file progress bar
                file_task = progress.add_task(f"[cyan]Extracting:[/] {f.name} ...", total=f.size)
                
                def update_progress(bytes_written):
                    progress.advance(total_task, bytes_written)
                    progress.advance(file_task, bytes_written)
                    
                scanner.extract_file(f, out_dir, progress_callback=update_progress)
                
                # Remove file task when done to keep ui clean
                progress.remove_task(file_task)
                progress.print(f"[dim]Recovered:[/] {f.path}")
                
                # Time-based Thermal Intermission check
                if tuned["work_interval"] and (time.time() - work_start_time) >= tuned["work_interval"]:
                    progress.print(f"\n[bold yellow]Thermal Safety Intermission: Cooling for {tuned['break_duration']}s...[/bold yellow]")
                    for remaining in range(tuned["break_duration"], 0, -1):
                        progress.update(total_task, description=f"[bold yellow]COOLING... {remaining}s remaining[/bold yellow]")
                        time.sleep(1)
                    progress.update(total_task, description=f"[bold cyan]Total Extraction Progress...")
                    work_start_time = time.time()  # Reset the work timer
                    progress.print("[green]Cooling complete. Resuming...[/green]\n")
                
    console.print(f"\nAll files written to [bold green]{os.path.abspath(out_dir)}[/bold green]")
    
    # --- Post-Extraction Sanity Check ---
    console.print("\n[bold]Running Sanity Check...[/bold]")
    verified_count = 0
    failed_file_metas = []
    
    for f in files_to_extract:
        if f.is_dir:
            continue
            
        # Use the exact same logic as extract_file to find the path
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
        console.print(f"[bold green]Sanity Check Passed![/bold green] All {verified_count} extracted files match expected sizes.")
    else:
        console.print(f"[yellow]{len(failed_file_metas)} files have mismatched or missing sizes. Re-extracting...[/yellow]\n")
        
        # Re-extract the failed files
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            DownloadColumn(),
            transient=True
        ) as retry_progress:
            retry_total = sum(f.size for f in failed_file_metas)
            retry_task = retry_progress.add_task("[bold yellow]Re-extracting failed files...", total=retry_total)
            
            for f in failed_file_metas:
                # Delete the incomplete file so extract_file doesn't skip it
                rel_path = f.path.lstrip("/")
                if rel_path.startswith(".Trashes"):
                    rel_path = rel_path.replace(".Trashes", "Recovered_Trash", 1)
                bad_path = os.path.join(out_dir, rel_path)
                if os.path.exists(bad_path):
                    os.remove(bad_path)
                
                def retry_cb(bytes_written):
                    retry_progress.advance(retry_task, bytes_written)
                    
                scanner.extract_file(f, out_dir, progress_callback=retry_cb)
                retry_progress.print(f"[dim]Re-extracted:[/] {f.path}")
        
        # Second-pass verification
        console.print("\n[bold]Running final verification...[/bold]")
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
            console.print(f"[bold green]All {len(failed_file_metas)} files recovered on retry! Sanity Check Passed.[/bold green]")
        else:
            console.print(f"[bold red]{len(still_failed)} files still failed after retry.[/bold red]")
            error_table = Table(show_header=True, header_style="bold red")
            error_table.add_column("Filename")
            error_table.add_column("Expected Size", justify="right")
            error_table.add_column("Actual Size", justify="right")
            for name, expected, actual in still_failed:
                error_table.add_row(name, format_size(expected), format_size(actual))
            console.print(error_table)
            console.print("[dim]These files may be too corrupted to recover at their original size.[/dim]")
    
    # --- Post-extraction repair offer ---
    if Confirm.ask("\n[bold]Scan recovered files for corruption and attempt repairs?[/bold]", default=True):
        run_repair_flow(out_dir)
    
    return True

def run_deep_scan(disk):
    """Run the deep scan flow. Returns True if user wants to scan again."""
    console.print(f"\n[bold yellow]Deep Scan Engine (PhotoRec)[/bold yellow]")
    
    device_path = f"/dev/{disk.device_id}"
    scanner = DeepScanner(device_path)
    
    if not scanner.check_photorec_installed():
        console.print("\n[red]PhotoRec is missing. Please run: brew install testdisk[/red]")
        return False
    
    # Pre-flight: check device accessibility
    ok, err = scanner.check_device_accessible()
    if not ok:
        console.print(f"\n[bold red]Error: {err}[/bold red]")
        return False
    
    out_dir = Prompt.ask("\nEnter destination path for recovered fragments", default="./recovered_files")
    
    # Pre-flight: check output space
    ok, info = scanner.check_output_space(out_dir)
    if not ok:
        console.print(f"\n[bold red]Error: {info}[/bold red]")
        return False
    console.print(f"[dim]Output directory: {os.path.abspath(out_dir)} ({info})[/dim]")
    
    # Ask about scan scope
    scan_scope = Prompt.ask(
        "Scan scope",
        choices=["wholespace", "freespace"],
        default="wholespace"
    )
    scan_freespace_only = scan_scope == "freespace"
    
    # Ask about file types
    console.print("\n[dim]Leave blank to recover all known file types.[/dim]")
    file_types_input = Prompt.ask(
        "File types to recover (comma-separated, e.g. jpg,png,mp4,pdf) or 'list' to see all available types",
        default=""
    ).strip()
    
    if file_types_input.lower() == "list":
        show_available_file_types()
        file_types_input = Prompt.ask(
            "\nFile types to recover (comma-separated, e.g. jpg,png,mp4,pdf) or blank for all",
            default=""
        ).strip()
    
    file_types = [t.strip().lstrip(".") for t in file_types_input.split(",") if t.strip()] if file_types_input else None
    
    if file_types:
        console.print(f"[dim]Recovering: {', '.join(file_types)}[/dim]")
    else:
        console.print("[dim]Recovering all known file types[/dim]")
    
    # macOS prevents raw block access if the volume is currently mounted
    console.print(f"\n[dim]Unmounting {disk.device_id} for deep scan...[/dim]")
    if not unmount_disk(disk.device_id):
        console.print(f"[yellow]Warning: Failed to unmount {disk.device_id}, scan may fail[/yellow]")
    
    global _cleanup_disk_id
    _cleanup_disk_id = disk.device_id
    
    from rich.live import Live

    try:
        with Live(Panel("Starting PhotoRec...", title="Deep Scan Progress", border_style="yellow"), refresh_per_second=4) as live:

            def output_callback(line):
                display = line.strip()
                if display:
                    live.update(Panel(display, title="Deep Scan Progress", border_style="yellow"))

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
        console.print(f"\n[dim]Remounting {disk.device_id}...[/dim]")
        subprocess.run(["diskutil", "mount", disk.device_id], capture_output=True)
        _cleanup_disk_id = None
        
    if success and stats:
        console.print(f"\n[bold green]Deep Scan Complete![/bold green]\n")
        
        # Show recovery stats table
        if stats.total_files > 0:
            stats_table = Table(title="Recovery Statistics", show_header=True, header_style="bold magenta")
            stats_table.add_column("Metric", style="cyan")
            stats_table.add_column("Value", style="green")
            
            stats_table.add_row("Files Recovered", str(stats.total_files))
            stats_table.add_row("Total Size", format_size(stats.total_bytes))
            stats_table.add_row("Duration", _format_duration(stats.duration_seconds))
            stats_table.add_row("Recovery Dirs", str(len(stats.recup_dirs)))
            
            console.print(stats_table)
            
            if stats.by_type:
                type_table = Table(title="Files by Type", show_header=True, header_style="bold magenta")
                type_table.add_column("Extension", style="cyan")
                type_table.add_column("Count", style="green", justify="right")
                
                for ext, count in sorted(stats.by_type.items(), key=lambda x: -x[1]):
                    type_table.add_row(ext, str(count))
                
                console.print(type_table)
            
            console.print(f"\n[dim]Files are in: {os.path.abspath(out_dir)}[/dim]")
            console.print("[dim]Note: Deep scans cannot reconstruct filenames or folder structures.[/dim]")
            
            # Offer post-scan repair
            if Confirm.ask("\n[bold]Scan recovered files for corruption and attempt repairs?[/bold]", default=False):
                run_repair_flow(out_dir)
        else:
            console.print("[yellow]No files were recovered.[/yellow]")
    else:
        console.print(f"\n[bold red]Deep Scan Failed or was Cancelled.[/bold red]\n")
        if stats and stats.errors:
            for err in stats.errors:
                console.print(f"[dim]  - {err}[/dim]")
    
    return True

def run_repair_flow(directory: str):
    """Diagnose and repair corrupted files in the given directory."""
    console.print(f"\n[bold]Scanning [cyan]{os.path.abspath(directory)}[/cyan] for corrupted files...[/bold]")
    
    cache_file = os.path.join(directory, ".drill_repair_cache.json")
    corrupt_files = None
    
    if os.path.exists(cache_file):
        if Confirm.ask(f"\n[bold yellow]Found a previous diagnosis cache[/bold yellow]. Skip rescan?", default=True):
            corrupt_files = load_scan_cache(cache_file)
            console.print(f"[dim]Loaded {len(corrupt_files)} corrupted files from cache.[/dim]")
            
    if corrupt_files is None:
        # Phase 1: Diagnose
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            transient=True,
        ) as progress:
            scan_task = progress.add_task("Diagnosing files...", total=None)
            
            def on_scan(filepath, current, total):
                progress.update(scan_task, total=total, completed=current,
                                description=f"Diagnosing [{current}/{total}] {os.path.basename(filepath)}")
            
            corrupt_files = repair_scan_dir(directory, progress_callback=on_scan)
            save_scan_cache(corrupt_files, cache_file)
    
    if not corrupt_files:
        console.print("\n[bold green]All files look healthy! No corruption detected.[/bold green]\n")
        return
    
    # Show diagnostics table
    console.print(f"\n[bold yellow]Found {len(corrupt_files)} file(s) with potential corruption:[/bold yellow]\n")
    
    diag_table = Table(show_header=True, header_style="bold magenta", expand=True)
    diag_table.add_column("#", style="dim", width=4)
    diag_table.add_column("File", style="cyan", no_wrap=True, max_width=40)
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
    
    if not Confirm.ask("\n[bold]Attempt to repair these files?[/bold]", default=True):
        console.print("[dim]Skipped repair.[/dim]")
        return
    
    reference_video = None
    if any(r.file_type in (FileType.MP4, FileType.MOV) for r in corrupt_files):
        console.print("\n[bold yellow]Advanced Video Repair (Optional)[/bold yellow]")
        console.print("Severely corrupted videos require a 'reference video' to repair.")
        console.print("This must be a healthy video recorded on the same device/software.")
        
        ref_input = Prompt.ask("Enter path to reference video (or press Enter to skip)")
        if ref_input:
            ref_input = ref_input.strip("'\"").strip()
            
        if ref_input and os.path.isfile(ref_input):
            reference_video = ref_input
        elif ref_input:
            console.print(f"[red]Reference file not found:[/red] {ref_input}. Proceeding without it.")

    # Phase 2: Repair
    console.print("")
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        transient=True,
    ) as progress:
        repair_task = progress.add_task("Repairing files...", total=len(corrupt_files))
        
        def on_repair(result, current, total):
            progress.update(repair_task, completed=current,
                            description=f"Repairing [{current}/{total}] {os.path.basename(result.filepath)}")
        
        repair_batch(corrupt_files, progress_callback=on_repair, reference_video=reference_video)
    
    # Phase 3: Summary
    repaired = [r for r in corrupt_files if r.status == RepairStatus.REPAIRED]
    partial = [r for r in corrupt_files if r.status == RepairStatus.PARTIALLY_REPAIRED]
    failed = [r for r in corrupt_files if r.status == RepairStatus.UNREPAIRABLE]
    
    console.print("")
    
    summary_table = Table(title="Repair Summary", show_header=True, header_style="bold", expand=True)
    summary_table.add_column("File", style="cyan", no_wrap=True, max_width=40)
    summary_table.add_column("Status")
    summary_table.add_column("Repaired File", style="dim", max_width=40)
    
    for r in corrupt_files:
        if r.status == RepairStatus.REPAIRED:
            status_str = "[bold green]Repaired[/bold green]"
        elif r.status == RepairStatus.PARTIALLY_REPAIRED:
            status_str = "[bold yellow]Partially Repaired[/bold yellow]"
        else:
            status_str = "[bold red]Unrepairable[/bold red]"
        
        repaired_name = os.path.basename(r.repaired_path) if r.repaired_path else "—"
        summary_table.add_row(os.path.basename(r.filepath), status_str, repaired_name)
    
    console.print(summary_table)
    console.print("")
    
    if repaired:
        console.print(f"[bold green]{len(repaired)} file(s) fully repaired.[/bold green]")
    if partial:
        console.print(f"[bold yellow]{len(partial)} file(s) partially repaired.[/bold yellow]")
    if failed:
        console.print(f"[bold red]{len(failed)} file(s) could not be repaired.[/bold red]")
    
    console.print("\n[dim]Repaired files are saved alongside originals with a '.repaired' suffix.[/dim]\n")


def main():
    global _cleanup_disk_id
    
    if os.geteuid() != 0:
        console.print("\n[bold red]Error: Terminal Drill requires root privileges to read raw disk blocks.[/bold red]")
        console.print("Please run the application using sudo:")
        console.print("  [bold cyan]sudo venv/bin/python3 drill_ui/app.py[/bold cyan]\n")
        sys.exit(1)

    # Lower process priority to reduce CPU heat and keep the system responsive
    try:
        os.nice(10)
    except AttributeError:
        # os.nice() is not available on macOS
        pass

    while True:
        display_header()
        
        scan_type = show_mode_selection()
        
        # Repair mode doesn't need disk access — short-circuit before disk selection
        if scan_type == "repair":
            repair_dir = Prompt.ask("\nEnter path to the directory of corrupt files", default="./recovered_files")
            if not os.path.isdir(repair_dir):
                console.print(f"[red]Directory not found: {repair_dir}[/red]")
                if not Confirm.ask("Try another directory?"):
                    break
                continue
            run_repair_flow(repair_dir)
        else:
            disk = select_disk()
            console.print(f"\n[bold]Selected:[/bold] {disk.device_id} ({disk.name})")
            
            if scan_type == "quick":
                run_quick_scan(disk)
            elif scan_type == "deep":
                run_deep_scan(disk)
        
        console.print("")
        if not Confirm.ask("Run another scan?", default=False):
            break
    
    console.print("\n[dim]Goodbye![/dim]\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[red]Exiting...[/red]")
        sys.exit(0)
