import sys
import os
# Inject the parent directory into sys.path so that imports work even when sudo strips PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn, TaskProgressColumn, DownloadColumn
from rich.tree import Tree
from drill_engine.discovery import get_macos_disks
from drill_engine.quick_scan import TSKScanner
from drill_engine.deep_scan import DeepScanner
import subprocess

console = Console()

def display_header():
    console.print("\n[bold cyan]⚡ TERMINAL DRILL ⚡[/bold cyan]")
    console.print("[dim]Professional File Recovery for the Terminal[/dim]\n")

def select_disk():
    disks = get_macos_disks()
    if not disks:
        console.print("[red]No disks found. Are you running as root/sudo?[/red]")
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
        
        # macOS prevents raw block access if the volume is currently mounted
        console.print(f"[dim]Unmounting {disk.device_id} for raw access...[/dim]")
        subprocess.run(["diskutil", "unmount", disk.device_id], capture_output=True)
        
        scanner = TSKScanner(device_path)
        
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
            
        # Remount the disk automatically so it reappears in Finder
        console.print(f"[dim]Remounting {disk.device_id}...[/dim]")
        subprocess.run(["diskutil", "mount", disk.device_id], capture_output=True)
            
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
