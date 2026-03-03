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
from drill_engine.deep_scan import DeepScanner
import subprocess

console = Console()

# Global cleanup state: tracks the disk ID that needs remounting if the app exits unexpectedly
_cleanup_disk_id = None

def _ensure_disk_remounted():
    """Atexit handler: remount the disk if the app exits while it's unmounted."""
    global _cleanup_disk_id
    if _cleanup_disk_id:
        console.print(f"\n[yellow]⚠ Safety remount: remounting {_cleanup_disk_id}...[/yellow]")
        subprocess.run(["diskutil", "mount", _cleanup_disk_id], capture_output=True)
        _cleanup_disk_id = None

atexit.register(_ensure_disk_remounted)

# Performance profiles: tuned throttle settings for different workloads
PERFORMANCE_PROFILES = {
    "fast": {
        "label": "⚡ Fast",
        "description": "Maximum speed, may heat up your Mac",
        "scan_throttle": 0,
        "chunk_size": 2 * 1024 * 1024,
        "burst_size": 100 * 1024 * 1024,
        "rest_duration": 0.05,
        "break_threshold": None,
        "break_duration": 0,
    },
    "balanced": {
        "label": "⚖️  Balanced",
        "description": "Good speed with moderate heat management",
        "scan_throttle": 0.001,
        "chunk_size": 1024 * 1024,
        "burst_size": 50 * 1024 * 1024,
        "rest_duration": 0.1,
        "break_threshold": 5 * 1024 * 1024 * 1024, # 5GB
        "break_duration": 30,
    },
    "cool": {
        "label": "❄️  Cool",
        "description": "Slower but keeps your Mac cool",
        "scan_throttle": 0.002,
        "chunk_size": 512 * 1024,
        "burst_size": 25 * 1024 * 1024,
        "rest_duration": 0.2,
        "break_threshold": 2 * 1024 * 1024 * 1024, # 2GB
        "break_duration": 60,
    },
    "siberia": {
        "label": "🏔️  Siberia",
        "description": "Maximum thermal safety for huge jobs",
        "scan_throttle": 0.005,
        "chunk_size": 256 * 1024,
        "burst_size": 10 * 1024 * 1024,
        "rest_duration": 0.5,
        "break_threshold": 512 * 1024 * 1024, # 500MB
        "break_duration": 60,
    },
}

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
    
    # Huge total data (>100GB): longer rests since heat accumulates over time
    if total_bytes > 100 * 1024 * 1024 * 1024:
        tuned["rest_duration"] = tuned["rest_duration"] * 1.5
    
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
        f"Thermal break: [yellow]Every {format_size(settings['break_threshold'])}[/yellow] for [yellow]{settings['break_duration']}s[/yellow]" if settings['break_threshold'] else "Thermal break: [cyan]off[/cyan]",
        f"Est. throttle overhead: [yellow]~{est_extra_time/60:.1f} min[/yellow] for {format_size(total_bytes)}",
    ]
    console.print(Panel("\n".join(lines), title="[bold]Performance Settings[/bold]", border_style="dim"))

def display_header():
    console.print("\n[bold cyan]⚡ TERMINAL DRILL ⚡[/bold cyan]")
    console.print("[dim]Professional File Recovery for the Terminal[/dim]\n")

def select_disk():
    try:
        disks = get_macos_disks()
    except subprocess.TimeoutExpired:
        console.print("\n[bold red]⌛ Error: Disk discovery timed out (diskutil is hanging).[/bold red]")
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
        type_str = "Physical" if d.is_physical else " Logical ↳"
        size_gb = f"{d.size / (1024**3):.2f} GB"
        table.add_row(d.device_id, d.name, type_str, size_gb)

    console.print(table)
    
    valid_ids = [d.device_id for d in disks]
    selected_id = Prompt.ask("Enter the [bold cyan]Disk ID[/bold cyan] you want to scan", choices=valid_ids)
    
    return next(d for d in disks if d.device_id == selected_id)

def build_vfs_tree(files, root_name="Recovered Files"):
    tree = Tree(f"[bold cyan]{root_name}[/bold cyan]")
    nodes = {"/": tree}
    
    # Sort paths
    files.sort(key=lambda x: x.path)
    
    for f in files:
        if f.path == "/": continue
        
        parent_dir = os.path.dirname(f.path)
        if parent_dir == "": parent_dir = "/"
        
        # Build intermediate paths
        parts = parent_dir.strip("/").split("/")
        current_path = ""
        for part in parts:
            if not part: continue
            current_path = current_path + "/" + part if current_path else "/" + part
            if current_path not in nodes:
                parent_path = os.path.dirname(current_path) or "/"
                nodes[current_path] = nodes[parent_path].add(f"[bold blue]{part}[/bold blue]")
                
        # Formatting
        size_str = f" ({f.size / 1024:.2f} KB)" if f.size < 1024 * 1024 else f" ({f.size / (1024*1024):.2f} MB)"
        label = f"[dim red][DELETED][/dim red] {f.name}{size_str}" if f.is_deleted else f"{f.name}{size_str}"
        
        node = nodes[parent_dir].add(label)
        if f.is_dir:
            nodes[f.path] = node
            
    return tree

def main():
    if os.geteuid() != 0:
        console.print("\n[bold red]❌ Error: Terminal Drill requires root privileges to read raw disk blocks.[/bold red]")
        console.print("Please run the application using sudo:")
        console.print("  [bold cyan]sudo venv/bin/python3 drill_ui/app.py[/bold cyan]\n")
        sys.exit(1)

    # Lower process priority to reduce CPU heat and keep the system responsive
    os.nice(10)

    display_header()
    disk = select_disk()
    
    console.print(f"\n[bold]Selected:[/bold] {disk.device_id} ({disk.name})")
    
    scan_type = Prompt.ask("Select Scan Type", choices=["quick", "deep"], default="quick")
    
    if scan_type == "quick":
        device_path = f"/dev/{disk.device_id}"
        
        # Ask performance preference
        console.print("\n[bold]Performance Mode[/bold]")
        for key, p in PERFORMANCE_PROFILES.items():
            console.print(f"  [cyan]{key:>8}[/cyan] → {p['label']}  [dim]{p['description']}[/dim]")
        perf_mode = Prompt.ask("Select performance mode", choices=["fast", "balanced", "cool", "siberia"], default="balanced")
        profile = PERFORMANCE_PROFILES[perf_mode].copy()
        
        # macOS prevents raw block access if the volume is currently mounted
        console.print(f"\n[dim]Unmounting {disk.device_id} for raw access...[/dim]")
        subprocess.run(["diskutil", "unmount", disk.device_id], capture_output=True)
        
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
                    sys.exit(1)
                    
                found_files = scanner.quick_scan()
        finally:
            # Always remount the disk so it reappears in Finder
            console.print(f"[dim]Remounting {disk.device_id}...[/dim]")
            subprocess.run(["diskutil", "mount", disk.device_id], capture_output=True)
            _cleanup_disk_id = None
            
        console.print(f"✅ [bold green]Scan Complete![/bold green] Found {len(found_files)} deleted files.\n")
        
        if found_files:
            vfs_tree = build_vfs_tree(found_files, root_name=device_path)
            console.print(vfs_tree)
            console.print("")
            
            if Confirm.ask("Do you want to extract these files?"):
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
                else:
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
                        
                        bytes_since_break = 0
                        
                        for f in files_to_extract:
                            if not f.is_dir:
                                # Per-file progress bar
                                file_task = progress.add_task(f"[cyan]Extracting:[/] {f.name} ...", total=f.size)
                                
                                def update_progress(bytes_written):
                                    nonlocal bytes_since_break
                                    progress.advance(total_task, bytes_written)
                                    progress.advance(file_task, bytes_written)
                                    bytes_since_break += bytes_written
                                    
                                scanner.extract_file(f, out_dir, progress_callback=update_progress)
                                
                                # Remove file task when done to keep ui clean
                                progress.remove_task(file_task)
                                progress.print(f"[dim]Recovered:[/] {f.path}")
                                
                                # Thermal Intermission check
                                if tuned["break_threshold"] and bytes_since_break >= tuned["break_threshold"]:
                                    progress.print(f"\n[bold yellow]❄️  Thermal Safety Intermission: Cooling for {tuned['break_duration']}s...[/bold yellow]")
                                    import time
                                    for remaining in range(tuned["break_duration"], 0, -1):
                                        progress.update(total_task, description=f"[bold yellow]❄️  COOLING... {remaining}s remaining[/bold yellow]")
                                        time.sleep(1)
                                    progress.update(total_task, description=f"[bold cyan]Total Extraction Progress...")
                                    bytes_since_break = 0
                                    progress.print("[green]Cooling complete. Resuming...[/green]\n")
                                
                    console.print(f"\n✅ All files written to [bold green]{os.path.abspath(out_dir)}[/bold green]")
                
    else:
        # Deep Scan (PhotoRec)
        console.print(f"\n[bold yellow]🔥 Initiating Deep Scan Engine (PhotoRec)[/bold yellow]")
        
        device_path = f"/dev/{disk.device_id}"
        scanner = DeepScanner(device_path)
        
        if not scanner.check_photorec_installed():
            console.print("\n[red]PhotoRec is missing. Please run: brew install testdisk[/red]")
            sys.exit(1)
            
        out_dir = Prompt.ask("\nEnter destination path for recovered fragments", default="./recovered_files")
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=False, # Keep progress visible as photorec can take hours
        ) as progress:
            progress.add_task(description=f"[yellow]Carving files from {device_path}... (This might take a while)[/yellow]", total=None)
            
            success = scanner.run_deep_scan(out_dir)
            
        if success:
            console.print(f"\n✅ [bold green]Deep Scan Complete![/bold green] All found fragments dumped into {out_dir}\n")
            console.print("[dim]Note: Deep scans cannot reconstruct filenames or folder structures.[/dim]")
        else:
            console.print(f"\n❌ [bold red]Deep Scan Failed or was Cancelled.[/bold red]\n")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[red]Exiting...[/red]")
        sys.exit(0)
