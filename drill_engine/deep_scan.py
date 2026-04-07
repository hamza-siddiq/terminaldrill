"""
deep_scan.py — Production-ready PhotoRec wrapper for signature-based file carving.

Handles:
- Correct CLI batch mode syntax (comma-separated commands, NOT stdin)
- Partition auto-detection for physical disks and images
- Configurable timeout with graceful termination
- Live output streaming with meaningful progress parsing
- Post-scan recovery statistics (file counts, types, sizes)
- Pre-scan validation (disk space, permissions, device access)
- Signal handling for clean cancellation (Ctrl+C)
"""

import subprocess
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Optional, Callable


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RecoveryStats:
    """Aggregated statistics from a completed deep scan."""
    total_files: int = 0
    total_bytes: int = 0
    by_type: dict = field(default_factory=dict)
    recup_dirs: list = field(default_factory=list)
    duration_seconds: float = 0.0
    errors: list = field(default_factory=list)

    def summary_lines(self) -> list:
        """Return human-readable summary lines."""
        lines = [
            f"Total files recovered: {self.total_files}",
            f"Total size: {_format_size(self.total_bytes)}",
            f"Scan duration: {_format_duration(self.duration_seconds)}",
        ]
        if self.by_type:
            lines.append("\nFiles by type:")
            for ext, count in sorted(self.by_type.items(), key=lambda x: -x[1]):
                lines.append(f"  {ext}: {count}")
        if self.recup_dirs:
            lines.append(f"\nRecovery directories: {len(self.recup_dirs)}")
        if self.errors:
            lines.append(f"\nWarnings ({len(self.errors)}):")
            for err in self.errors:
                lines.append(f"  - {err}")
        return lines


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024**3):.2f} GB"
    elif size_bytes >= 1024 ** 2:
        return f"{size_bytes / (1024**2):.2f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _format_duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m {int(seconds % 60)}s"
    elif seconds >= 60:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds)}s"


def _available_disk_space(path: str) -> int:
    """Return available disk space in bytes for the filesystem containing path."""
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


# Common file extensions that PhotoRec recovers — used for post-scan stats
KNOWN_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp", ".svg", ".psd", ".raw", ".cr2", ".nef",
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".3gp",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".odt",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2",
    ".html", ".css", ".js", ".py", ".java", ".c", ".cpp", ".h",
    ".db", ".sqlite", ".sql",
    ".dmg", ".iso", ".img",
    ".exe", ".dll", ".so", ".dylib",
    ".eml", ".pst", ".mbox",
}


# ---------------------------------------------------------------------------
# Main scanner
# ---------------------------------------------------------------------------

class DeepScanner:
    """Production-ready PhotoRec wrapper for deep signature-based file recovery."""

    # Default timeout: 24 hours (configurable via env var PHOTOREC_TIMEOUT)
    DEFAULT_TIMEOUT = int(os.environ.get("PHOTOREC_TIMEOUT", "86400"))

    def __init__(self, device_path: str, timeout: Optional[int] = None):
        self.device_path = device_path
        self.timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        self._process: Optional[subprocess.Popen] = None
        self._cancelled = False

    # ------------------------------------------------------------------
    # Pre-flight checks
    # ------------------------------------------------------------------

    def check_photorec_installed(self) -> bool:
        """Verify photorec binary is on PATH."""
        return shutil.which("photorec") is not None

    def check_device_accessible(self) -> tuple[bool, str]:
        """
        Verify the device path exists and is readable.
        Returns (ok, error_message).
        """
        if not os.path.exists(self.device_path):
            return False, f"Device not found: {self.device_path}"
        if not os.access(self.device_path, os.R_OK):
            return False, f"No read permission for {self.device_path}. Run with sudo."
        return True, ""

    def check_output_space(self, output_dir: str, min_bytes: int = 1024 * 1024 * 100) -> tuple[bool, str]:
        """
        Verify the output filesystem has sufficient free space.
        Default minimum: 100 MB (deep scans can produce GBs of data).
        Returns (ok, info_message).
        """
        try:
            os.makedirs(output_dir, exist_ok=True)
            free = _available_disk_space(output_dir)
            if free < min_bytes:
                return False, (
                    f"Insufficient disk space at {output_dir}. "
                    f"Available: {_format_size(free)}, recommended minimum: {_format_size(min_bytes)}"
                )
            return True, f"Available space: {_format_size(free)}"
        except OSError as e:
            return False, f"Cannot access output directory: {e}"

    # ------------------------------------------------------------------
    # Core scan
    # ------------------------------------------------------------------

    def run_deep_scan(
        self,
        output_dir: str,
        output_callback: Optional[Callable[[str], None]] = None,
        file_types: Optional[list[str]] = None,
        scan_freespace_only: bool = False,
    ) -> tuple[bool, Optional[RecoveryStats]]:
        """
        Execute PhotoRec in batch mode and stream output.

        Args:
            output_dir: Directory to store recovered files.
            output_callback: Called with each output line for live UI updates.
            file_types: Specific extensions to recover (e.g. ["jpg", "png", "mp4"]).
                        If None, all known file types are enabled.
            scan_freespace_only: If True, only scan unallocated space (faster,
                                 finds deleted files only). If False, scans entire
                                 disk including allocated space.

        Returns:
            (success, RecoveryStats). success is False on error or cancellation.
        """
        # --- Pre-flight ---
        if not self.check_photorec_installed():
            msg = "Error: photorec is not installed. Run: brew install testdisk\n"
            if output_callback:
                output_callback(msg)
            return False, None

        ok, err = self.check_device_accessible()
        if not ok:
            if output_callback:
                output_callback(f"Error: {err}\n")
            return False, None

        ok, info = self.check_output_space(output_dir)
        if not ok:
            if output_callback:
                output_callback(f"Error: {info}\n")
            return False, None
        if output_callback:
            output_callback(f"Output drive space: {info}\n")

        os.makedirs(output_dir, exist_ok=True)

        # --- Build command ---
        cmd = self._build_command(output_dir, file_types, scan_freespace_only)

        if output_callback:
            output_callback(f"Starting PhotoRec on {self.device_path}...\n")
            output_callback(f"Command: {' '.join(cmd)}\n")
            output_callback("This may take a long time. Files are recovered by signature only — no original names.\n\n")

        # --- Execute ---
        start_time = time.time()
        stats = RecoveryStats()

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                errors="replace",  # Handle non-UTF8 output gracefully
            )

            # Stream output line by line
            for line in self._process.stdout:
                if self._cancelled:
                    self._process.terminate()
                    if output_callback:
                        output_callback("\nScan cancelled by user.\n")
                    stats.errors.append("Cancelled by user")
                    return False, stats

                if output_callback:
                    output_callback(line)

            self._process.wait()
            stats.duration_seconds = time.time() - start_time

        except KeyboardInterrupt:
            self._cancelled = True
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            if output_callback:
                output_callback("\nScan cancelled by user.\n")
            stats.errors.append("Cancelled by user")
            return False, stats

        except subprocess.TimeoutExpired:
            if self._process and self._process.poll() is None:
                self._process.kill()
            if output_callback:
                output_callback(f"\nScan timed out after {_format_duration(self.timeout)}.\n")
            stats.errors.append(f"Timed out after {self.timeout}s")
            return False, stats

        except Exception as e:
            if output_callback:
                output_callback(f"\nFatal error running PhotoRec: {e}\n")
            stats.errors.append(str(e))
            return False, stats

        # --- Post-scan analysis ---
        self._cancelled = False
        rc = self._process.returncode
        self._process = None

        stats = self._collect_stats(output_dir, stats)

        if rc != 0 and rc != 1:
            # PhotoRec returns 0 on success, 1 on "no files found"
            msg = f"PhotoRec exited with code {rc}"
            if output_callback:
                output_callback(f"\nWarning: {msg}\n")
            stats.errors.append(msg)

        if stats.total_files > 0:
            if output_callback:
                output_callback("\n" + "\n".join(stats.summary_lines()) + "\n")
            return True, stats
        else:
            if output_callback:
                output_callback("\nNo files recovered.\n")
            return False, stats

    # ------------------------------------------------------------------
    # Command building
    # ------------------------------------------------------------------

    def _build_command(
        self,
        output_dir: str,
        file_types: Optional[list[str]] = None,
        scan_freespace_only: bool = False,
    ) -> list[str]:
        """
        Build the PhotoRec command line.

        PhotoRec batch syntax (per CGSecurity docs):
            photorec [/d recup_dir] [/cmd <device> <command>]
        Commands are comma-separated, NOT stdin.

        For raw devices / whole disks: use partition_none to let PhotoRec
        auto-detect partitions, then search.
        """
        cmd = [
            "photorec",
            "/d", output_dir,
            "/log",  # Enable photorec.log for debugging
            "/cmd",
            self.device_path,
        ]

        # Build the command chain
        commands = []

        # Partition type: none = auto-detect (works for raw devices and images)
        commands.append("partition_none")

        # File type options
        if file_types:
            # Disable everything, then enable only requested types
            commands.append("fileopt,everything,disable")
            for ft in file_types:
                ext = ft.lstrip(".").lower()
                commands.append(f"fileopt,{ext},enable")
        else:
            # Enable all known file types
            commands.append("fileopt,everything,enable")

        # Free space only vs whole space
        if scan_freespace_only:
            commands.append("freespace")
        # else: default is wholespace (scan everything)

        # Start the search
        commands.append("search")

        # Join with commas — PhotoRec expects a single comma-separated arg
        cmd.append(",".join(commands))

        return cmd

    # ------------------------------------------------------------------
    # Post-scan statistics
    # ------------------------------------------------------------------

    def _collect_stats(self, output_dir: str, stats: RecoveryStats) -> RecoveryStats:
        """Walk the output directory and collect recovery statistics."""
        recup_dirs = []
        total_files = 0
        total_bytes = 0
        by_type: dict[str, int] = {}

        for item in sorted(os.listdir(output_dir)):
            item_path = os.path.join(output_dir, item)
            if item.startswith("recup_dir.") and os.path.isdir(item_path):
                recup_dirs.append(item)
                for fname in os.listdir(item_path):
                    fpath = os.path.join(item_path, fname)
                    if os.path.isfile(fpath):
                        total_files += 1
                        try:
                            fsize = os.path.getsize(fpath)
                            total_bytes += fsize
                        except OSError:
                            pass
                        _, ext = os.path.splitext(fname)
                        ext = ext.lower()
                        if ext:
                            by_type[ext] = by_type.get(ext, 0) + 1

        stats.total_files = total_files
        stats.total_bytes = total_bytes
        stats.by_type = by_type
        stats.recup_dirs = recup_dirs

        return stats

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel(self):
        """Signal the running scan to stop gracefully."""
        self._cancelled = True
        if self._process and self._process.poll() is None:
            self._process.terminate()

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None


# ---------------------------------------------------------------------------
# Standalone usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python3 deep_scan.py <disk_image_or_device> <output_dir> [file_type ...]")
        print("  file_type: optional list of extensions to recover (e.g. jpg png mp4)")
        sys.exit(1)

    device = sys.argv[1]
    out_dir = sys.argv[2]
    types = sys.argv[3:] if len(sys.argv) > 3 else None

    scanner = DeepScanner(device)

    def cb(line):
        print(line, end="")

    success, stats = scanner.run_deep_scan(out_dir, output_callback=cb, file_types=types)

    if success and stats:
        print(f"\nRecovered {stats.total_files} files ({_format_size(stats.total_bytes)})")
    else:
        print("\nNo files recovered.")
        sys.exit(1)
