"""
file_repair.py — Diagnose and repair common corruption patterns in recovered files.

Supports: JPEG, PNG, MP4/MOV, PDF, ZIP
Strategies: magic-byte detection, header/EOF validation, zero-region scanning,
            ffmpeg remux for video, Pillow re-encode for images, zip -FF, qpdf.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass, field
import json
from enum import Enum
from typing import List, Optional, Callable


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class FileType(Enum):
    JPEG = "jpeg"
    PNG = "png"
    MP4 = "mp4"
    MOV = "mov"
    PDF = "pdf"
    ZIP = "zip"
    UNKNOWN = "unknown"


# Magic byte signatures for file-type detection
MAGIC_SIGNATURES = {
    FileType.JPEG: [b"\xff\xd8\xff"],
    FileType.PNG:  [b"\x89PNG\r\n\x1a\n"],
    FileType.PDF:  [b"%PDF"],
    FileType.ZIP:  [b"PK\x03\x04", b"PK\x05\x06"],  # normal + empty archive
    FileType.MP4:  [b"ftyp"],   # appears at offset 4
    FileType.MOV:  [b"ftyp"],   # MOV also uses ftyp; disambiguated by sub-brand
}

# Extension → FileType fallback
EXT_MAP = {
    ".jpg": FileType.JPEG, ".jpeg": FileType.JPEG,
    ".png": FileType.PNG,
    ".mp4": FileType.MP4,
    ".mov": FileType.MOV,
    ".m4v": FileType.MP4,
    ".pdf": FileType.PDF,
    ".zip": FileType.ZIP,
}

# Minimum viable file sizes (bytes) — anything smaller is almost certainly truncated
MIN_SIZES = {
    FileType.JPEG: 107,
    FileType.PNG:  67,
    FileType.MP4:  128,
    FileType.MOV:  128,
    FileType.PDF:  67,
    FileType.ZIP:  22,
}

# How many bytes to read for zero-region scanning per chunk
ZERO_SCAN_CHUNK = 64 * 1024  # 64 KB
# A region is flagged if it contains this fraction of zero bytes
ZERO_THRESHOLD = 0.95


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class IssueType(Enum):
    TRUNCATED = "Truncated (below minimum viable size)"
    HEADER_MISSING = "Missing or corrupt file header"
    EOF_MISSING = "Missing end-of-file marker"
    ZERO_REGIONS = "Large zero-filled regions detected"
    FFMPEG_ERRORS = "Stream errors detected by ffmpeg"


class RepairStatus(Enum):
    HEALTHY = "healthy"
    REPAIRED = "repaired"
    PARTIALLY_REPAIRED = "partially_repaired"
    UNREPAIRABLE = "unrepairable"
    SKIPPED = "skipped"


@dataclass
class RepairResult:
    """Stores per-file diagnosis and repair outcome."""
    filepath: str
    file_type: FileType
    file_size: int
    issues: List[IssueType] = field(default_factory=list)
    status: RepairStatus = RepairStatus.HEALTHY
    repaired_path: Optional[str] = None
    details: str = ""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_type(filepath: str) -> FileType:
    """Identify file type via magic bytes, falling back to extension."""
    try:
        with open(filepath, "rb") as f:
            header = f.read(12)
    except OSError:
        return _type_from_ext(filepath)

    if not header:
        return _type_from_ext(filepath)

    # JPEG: starts with FF D8 FF
    if header[:3] == b"\xff\xd8\xff":
        return FileType.JPEG

    # PNG: starts with 89 50 4E 47
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return FileType.PNG

    # PDF: starts with %PDF
    if header[:4] == b"%PDF":
        return FileType.PDF

    # ZIP: starts with PK
    if header[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return FileType.ZIP

    # MP4/MOV: 'ftyp' at offset 4
    if header[4:8] == b"ftyp":
        # Check sub-brand to distinguish MOV vs MP4
        brand = header[8:12]
        if brand in (b"qt  ", b"MSNV"):
            return FileType.MOV
        return FileType.MP4

    return _type_from_ext(filepath)


def _type_from_ext(filepath: str) -> FileType:
    ext = os.path.splitext(filepath)[1].lower()
    return EXT_MAP.get(ext, FileType.UNKNOWN)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def diagnose(filepath: str) -> RepairResult:
    """Run all corruption checks on a single file and return a RepairResult."""
    file_type = detect_type(filepath)
    try:
        file_size = os.path.getsize(filepath)
    except OSError:
        return RepairResult(
            filepath=filepath, file_type=file_type, file_size=0,
            issues=[IssueType.TRUNCATED], status=RepairStatus.UNREPAIRABLE,
            details="File is inaccessible."
        )

    result = RepairResult(filepath=filepath, file_type=file_type, file_size=file_size)

    if file_size == 0:
        result.issues.append(IssueType.TRUNCATED)
        result.status = RepairStatus.UNREPAIRABLE
        result.details = "File is empty (0 bytes)."
        return result

    # 1. Truncation check
    min_size = MIN_SIZES.get(file_type, 0)
    if file_size < min_size:
        result.issues.append(IssueType.TRUNCATED)

    # 2. Header validation
    _check_header(filepath, file_type, result)

    # 3. EOF marker check
    _check_eof(filepath, file_type, file_size, result)

    # 4. Zero-region scan
    _check_zero_regions(filepath, file_size, result)

    # 5. Video-specific: ffmpeg error probe
    if file_type in (FileType.MP4, FileType.MOV):
        _check_ffmpeg_errors(filepath, result)

    # Determine overall status
    if not result.issues:
        result.status = RepairStatus.HEALTHY
    else:
        result.status = RepairStatus.UNREPAIRABLE  # will be updated by repair()

    return result


def _check_header(filepath: str, file_type: FileType, result: RepairResult):
    """Verify the file header matches what we expect for its detected type."""
    if file_type == FileType.UNKNOWN:
        return

    try:
        with open(filepath, "rb") as f:
            header = f.read(12)
    except OSError:
        result.issues.append(IssueType.HEADER_MISSING)
        return

    valid = False
    if file_type == FileType.JPEG:
        valid = header[:3] == b"\xff\xd8\xff"
    elif file_type == FileType.PNG:
        valid = header[:8] == b"\x89PNG\r\n\x1a\n"
    elif file_type == FileType.PDF:
        valid = header[:4] == b"%PDF"
    elif file_type == FileType.ZIP:
        valid = header[:4] in (b"PK\x03\x04", b"PK\x05\x06")
    elif file_type in (FileType.MP4, FileType.MOV):
        valid = header[4:8] == b"ftyp"

    if not valid:
        result.issues.append(IssueType.HEADER_MISSING)


def _check_eof(filepath: str, file_type: FileType, file_size: int, result: RepairResult):
    """Check for known end-of-file markers."""
    try:
        with open(filepath, "rb") as f:
            if file_type == FileType.JPEG:
                f.seek(max(0, file_size - 2))
                tail = f.read(2)
                if tail != b"\xff\xd9":
                    result.issues.append(IssueType.EOF_MISSING)

            elif file_type == FileType.PNG:
                # IEND chunk: 00 00 00 00 49 45 4E 44 AE 42 60 82
                f.seek(max(0, file_size - 12))
                tail = f.read(12)
                if b"IEND" not in tail:
                    result.issues.append(IssueType.EOF_MISSING)

            elif file_type == FileType.PDF:
                f.seek(max(0, file_size - 32))
                tail = f.read(32)
                if b"%%EOF" not in tail:
                    result.issues.append(IssueType.EOF_MISSING)
    except OSError:
        pass


def _check_zero_regions(filepath: str, file_size: int, result: RepairResult):
    """Sample a few evenly-spaced chunks to detect zero-filled regions.
    
    Instead of reading the entire file (slow on multi-GB files), we sample
    up to MAX_SAMPLES chunks from evenly-spaced positions across the file.
    """
    MAX_SAMPLES = 10

    if file_size <= ZERO_SCAN_CHUNK:
        # Small file — just read it all
        positions = [0]
    else:
        step = file_size // MAX_SAMPLES
        positions = [i * step for i in range(MAX_SAMPLES)]

    try:
        zero_chunks = 0
        with open(filepath, "rb") as f:
            for pos in positions:
                f.seek(pos)
                chunk = f.read(ZERO_SCAN_CHUNK)
                if not chunk:
                    continue
                if chunk.count(b"\x00") / len(chunk) >= ZERO_THRESHOLD:
                    zero_chunks += 1

        # Flag if more than 25% of sampled chunks are zero-filled
        if positions and zero_chunks / len(positions) > 0.25:
            result.issues.append(IssueType.ZERO_REGIONS)
            result.details += f" {zero_chunks}/{len(positions)} sampled chunks are zero-filled."
    except OSError:
        pass


def _check_ffmpeg_errors(filepath: str, result: RepairResult):
    """Use ffprobe to quickly check if the video container is readable.
    
    This only reads metadata/headers (sub-second even for huge files).
    The full stream decode is deferred to the repair phase.
    """
    ffprobe = shutil.which("ffprobe") or (shutil.which("ffmpeg") and "ffprobe")
    if not ffprobe or not shutil.which(ffprobe):
        # Fall back to ffmpeg with a very short read (first 5 seconds only)
        if not shutil.which("ffmpeg"):
            return
        try:
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-t", "5", "-i", filepath, "-f", "null", "-"],
                capture_output=True, text=True, timeout=15,
            )
            stderr = proc.stderr.strip()
            if stderr:
                result.issues.append(IssueType.FFMPEG_ERRORS)
                lines = stderr.splitlines()
                preview = "\n".join(lines[:3])
                result.details += f" ffmpeg: {preview}"
        except (subprocess.TimeoutExpired, OSError):
            pass
        return

    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration,format_name:stream=codec_type,codec_name",
             "-of", "json", filepath],
            capture_output=True, text=True, timeout=10,
        )
        stderr = proc.stderr.strip()
        if stderr or proc.returncode != 0:
            result.issues.append(IssueType.FFMPEG_ERRORS)
            lines = stderr.splitlines() if stderr else ["Container unreadable"]
            preview = "\n".join(lines[:3])
            result.details += f" ffprobe: {preview}"
    except (subprocess.TimeoutExpired, OSError):
        pass


# ---------------------------------------------------------------------------
# Repair strategies
# ---------------------------------------------------------------------------

def repair(result: RepairResult, progress_callback: Optional[Callable[[str], None]] = None, reference_video: Optional[str] = None) -> RepairResult:
    """
    Attempt to repair a file based on its diagnosis.
    Writes repaired file to <original>.repaired.<ext>, leaving the original intact.
    Updates and returns the RepairResult.
    """
    if result.status == RepairStatus.HEALTHY:
        return result

    if not result.issues:
        return result

    filepath = result.filepath
    base, ext = os.path.splitext(filepath)
    repaired_path = f"{base}.repaired{ext}"

    def _log(msg: str):
        if progress_callback:
            progress_callback(msg)

    success = False

    if result.file_type in (FileType.MP4, FileType.MOV):
        success = _repair_video(filepath, repaired_path, log=_log, reference_video=reference_video)
    elif result.file_type == FileType.JPEG:
        success = _repair_jpeg(filepath, repaired_path, result, _log)
    elif result.file_type == FileType.PNG:
        success = _repair_png(filepath, repaired_path, _log)
    elif result.file_type == FileType.ZIP:
        success = _repair_zip(filepath, repaired_path, _log)
    elif result.file_type == FileType.PDF:
        success = _repair_pdf(filepath, repaired_path, _log)
    else:
        _log(f"No automated repair available for {result.file_type.value} files.")
        result.status = RepairStatus.UNREPAIRABLE
        return result

    if success and os.path.exists(repaired_path) and os.path.getsize(repaired_path) > 0:
        result.repaired_path = repaired_path
        # Check if the repaired file still has issues
        re_diag = diagnose(repaired_path)
        if not re_diag.issues:
            result.status = RepairStatus.REPAIRED
            _log(f"✅ Fully repaired → {os.path.basename(repaired_path)}")
        else:
            result.status = RepairStatus.PARTIALLY_REPAIRED
            remaining = ", ".join(i.value for i in re_diag.issues)
            _log(f"⚠ Partially repaired (remaining: {remaining})")
    else:
        result.status = RepairStatus.UNREPAIRABLE
        _log("❌ Repair failed.")
        # Clean up empty/broken output
        if os.path.exists(repaired_path):
            try:
                os.remove(repaired_path)
            except OSError:
                pass

    return result


# --- Per-type repair implementations ---

def _repair_video_untrunc(src: str, dst: str, ref: str, log: Callable) -> bool:
    """Repair severe MP4/MOV structural corruption using untrunc + reference video."""
    if not shutil.which("untrunc"):
        log("untrunc not found. Skipping advanced video repair.")
        log("Install via: brew tap ottomatic-io/video && brew install untrunc")
        return False
        
    log("Attempting advanced video repair via untrunc...")
    try:
        proc = subprocess.run(
            ["untrunc", "-dst", dst, ref, src],
            capture_output=True, text=True, timeout=300,
        )
        
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            return True
        else:
            log(f"untrunc failed to produce output. Error: {proc.stderr[:100]}...")
            return False
            
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"untrunc error: {e}")
        return False

def _repair_video(src: str, dst: str, log: Callable, reference_video: Optional[str] = None):
    """Attempt video repair. Try untrunc first if ref provided, fallback to ffmpeg."""
    
    if reference_video:
        if _repair_video_untrunc(src, dst, reference_video, log):
            return True
        log("Advanced repair failed. Falling back to ffmpeg re-mux...")

    if not shutil.which("ffmpeg"):
        log("ffmpeg not found. Install with: brew install ffmpeg")
        return False

    log("Attempting ffmpeg remux with moov atom rebuild...")
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", src,
                "-c", "copy",
                "-movflags", "faststart",
                dst,
            ],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode == 0:
            return True
        else:
            # Fallback: try without faststart (some severely corrupt files choke on it)
            log("Retrying without moov rebuild...")
            proc2 = subprocess.run(
                ["ffmpeg", "-y", "-i", src, "-c", "copy", dst],
                capture_output=True, text=True, timeout=300,
            )
            return proc2.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"ffmpeg error: {e}")
        return False


def _repair_jpeg(src: str, dst: str, result: RepairResult, log: Callable):
    """Fix JPEG: append missing EOI marker and/or re-encode via Pillow."""
    repaired = False

    # Strategy 1: Append missing EOI marker (0xFFD9)
    if IssueType.EOF_MISSING in result.issues:
        log("Appending missing JPEG EOI marker...")
        try:
            with open(src, "rb") as f:
                data = f.read()
            if not data.endswith(b"\xff\xd9"):
                data += b"\xff\xd9"
            with open(dst, "wb") as f:
                f.write(data)
            repaired = True
        except OSError as e:
            log(f"EOI fix failed: {e}")

    # Strategy 2: Re-encode through Pillow (fixes internal structure errors)
    source = dst if repaired else src
    try:
        from PIL import Image
        log("Re-encoding JPEG via Pillow...")
        img = Image.open(source)
        img.load()  # force full decode
        img.save(dst, "JPEG", quality=95)
        return True
    except ImportError:
        log("Pillow not installed. Skipping re-encode. (pip install Pillow)")
        return repaired
    except Exception as e:
        log(f"Pillow re-encode failed: {e}")
        return repaired


def _repair_png(src: str, dst: str, log: Callable):
    """Re-encode PNG via Pillow to fix chunk CRC errors."""
    try:
        from PIL import Image
        log("Re-encoding PNG via Pillow...")
        img = Image.open(src)
        img.load()
        img.save(dst, "PNG")
        return True
    except ImportError:
        log("Pillow not installed. Skipping re-encode. (pip install Pillow)")
        return False
    except Exception as e:
        log(f"PNG re-encode failed: {e}")
        return False


def _repair_zip(src: str, dst: str, log: Callable):
    """Attempt to fix a corrupt ZIP archive using zip -FF."""
    if not shutil.which("zip"):
        log("zip utility not found.")
        return False

    log("Attempting ZIP archive repair (zip -FF)...")
    try:
        proc = subprocess.run(
            ["zip", "-FF", src, "--out", dst],
            capture_output=True, text=True, timeout=120,
            input="y\n",  # auto-confirm prompts
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"zip repair error: {e}")
        return False


def _repair_pdf(src: str, dst: str, log: Callable):
    """Attempt PDF repair via qpdf linearization."""
    if not shutil.which("qpdf"):
        log("qpdf not found. Install with: brew install qpdf")
        return False

    log("Attempting PDF repair via qpdf...")
    try:
        proc = subprocess.run(
            ["qpdf", "--linearize", src, dst],
            capture_output=True, text=True, timeout=120,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"qpdf repair error: {e}")
        return False


# ---------------------------------------------------------------------------
# Batch operations (used by the UI)
# ---------------------------------------------------------------------------

def scan_directory(directory: str, progress_callback: Optional[Callable[[str, int, int], None]] = None) -> List[RepairResult]:
    """
    Walk a directory and diagnose every file.
    progress_callback(filepath, current_index, total_count) is called per file.
    Returns a list of RepairResults (only files with issues).
    """
    all_files = []
    for root, _, filenames in os.walk(directory):
        for fname in filenames:
            # Skip temp files and already-repaired files
            if fname.endswith(".drilltemp") or ".repaired." in fname:
                continue
            all_files.append(os.path.join(root, fname))

    results = []
    total = len(all_files)
    for idx, fpath in enumerate(all_files):
        if progress_callback:
            progress_callback(fpath, idx + 1, total)
        result = diagnose(fpath)
        if result.issues:
            results.append(result)

    return results


def repair_batch(results: List[RepairResult],
                 progress_callback: Optional[Callable[[RepairResult, int, int], None]] = None,
                 reference_video: Optional[str] = None) -> List[RepairResult]:
    """
    Attempt to repair all diagnosed files.
    progress_callback(result, current_index, total_count) is called per file.
    Optionally accepts a reference_video path for advanced untrunc repair.
    """
    total = len(results)
    repaired = []
    for idx, result in enumerate(results):
        def _log(msg: str, _idx=idx, _result=result, _total=total):
            if progress_callback:
                progress_callback(_result, _idx + 1, _total)

        repair(result, progress_callback=_log, reference_video=reference_video)
        repaired.append(result)

    return repaired


def save_scan_cache(results: List[RepairResult], cache_file: str):
    """Serialize scan results to a JSON cache file."""
    data = []
    for r in results:
        data.append({
            "filepath": r.filepath,
            "file_type": r.file_type.value,
            "file_size": r.file_size,
            "issues": [i.value for i in r.issues],
            "status": r.status.value,
            "repaired_path": r.repaired_path,
            "details": r.details
        })
    try:
        with open(cache_file, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def load_scan_cache(cache_file: str) -> List[RepairResult]:
    """Load and deserialize scan results from a JSON cache file."""
    try:
        with open(cache_file, "r") as f:
            data = json.load(f)
            
        results = []
        for item in data:
            issues = [IssueType(i) for i in item.get("issues", [])]
            results.append(RepairResult(
                filepath=item["filepath"],
                file_type=FileType(item["file_type"]),
                file_size=item["file_size"],
                issues=issues,
                status=RepairStatus(item["status"]),
                repaired_path=item.get("repaired_path"),
                details=item.get("details", "")
            ))
        return results
    except (OSError, json.JSONDecodeError, ValueError):
        return []
