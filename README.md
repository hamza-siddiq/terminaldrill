# Terminal Drill

**Professional File Recovery for the Terminal**

Terminal Drill is a CLI-based file recovery utility for macOS. It provides an interactive terminal interface for recovering deleted files, carving data from disk images or physical drives, and repairing corrupted files.

## Installation

### 1. Install System Dependencies

```bash
brew install sleuthkit testdisk ffmpeg qpdf
brew tap ottomatic-io/video && brew install untrunc
```

### 2. Clone & Setup

```bash
git clone https://github.com/hamza-siddiq/terminaldrill.git
cd terminaldrill
python3 -m venv venv
source venv/bin/activate
pip install pytsk3 rich
```

## Usage

**Terminal Drill requires root privileges** to access raw disk blocks and unmount volumes.

```bash
sudo venv/bin/python3 drill_ui/app.py
```

### Workflow

1. **Select Mode** — Choose `quick`, `deep`, or `repair`
2. **Select Drive** — Pick the target disk from the enumerated table
3. **Configure Scan** — Set performance mode, scan scope, and file type filters
4. **Review Results** — Browse the recovered file tree
5. **Extract Files** — Filter by name, sort by size, choose destination
6. **Repair (Optional)** — Scan recovered files for corruption and attempt automated repairs

---

## Scan Modes

### Quick Scan (TSK)

Uses The Sleuth Kit to identify deleted files by analyzing filesystem metadata. Best for recently deleted files on intact filesystems.

- **Performance modes**: `fast`, `balanced`, `cool`, `siberia` — tune speed vs. thermal management
- **Auto-tuning**: Adjusts chunk/burst sizes based on detected file sizes
- **Thermal intermissions**: Automatic cooling breaks for long extractions
- **Sanity check**: Verifies extracted files match expected sizes, auto-retries failures
- **File filtering**: Extract specific files or all, sorted by size (asc/desc)

### Deep Scan (PhotoRec)

Uses PhotoRec for signature-based file carving. Best for formatted, corrupted, or unrecognizable volumes.

- **Scan scope**: `wholespace` (entire disk) or `freespace` (deleted files only, faster)
- **File type filtering**: Recover specific extensions (e.g. `jpg,png,mp4`) or all known types
- **Pre-flight checks**: Validates device accessibility and output disk space before starting
- **Live progress**: Real-time output streaming with Rich Live panel
- **Recovery statistics**: Post-scan table showing file counts, total size, and breakdown by type
- **Configurable timeout**: 24-hour default, adjustable via `PHOTOREC_TIMEOUT` environment variable
- **Graceful cancellation**: Ctrl+C safely terminates PhotoRec and remounts the disk

### Repair Mode

Diagnoses and repairs corrupted files without needing disk access. Useful for files from any recovery source.

- **Supported types**: JPEG, PNG, MP4/MOV, PDF, ZIP
- **Detection**: Magic-byte analysis, header/EOF validation, zero-region scanning, ffmpeg probe
- **Repair strategies**:
  - **Video**: ffmpeg remux with moov atom rebuild; advanced repair via `untrunc` + reference video
  - **Images**: Pillow re-encoding for JPEG/PNG; auto-append missing JPEG EOI markers
  - **ZIP**: `zip -FF` archive reconstruction
  - **PDF**: `qpdf` linearization repair
- **Diagnosis cache**: Skips rescanning if a previous diagnosis exists
- **Batch summary**: Table showing per-file status (repaired / partially repaired / unrepairable)

---

## Key Features

| Feature | Description |
|---------|-------------|
| **Smart disk discovery** | Enumerates physical disks and logical volumes with size info |
| **Performance profiles** | 4 modes from maximum speed to maximum thermal safety |
| **Thermal management** | Burst-and-rest extraction with configurable cooling breaks |
| **Nested progress bars** | Per-file and total extraction progress with ETA and speed |
| **Conditional extraction** | Filter by filename, sort by size, atomic writes with resume support |
| **Sanity check + retry** | Verifies file sizes post-extraction, auto-retries mismatches |
| **Corruption repair** | Automated diagnosis and repair for 5 file types |
| **Safety remount** | Atexit handler ensures disks are remounted on crash or Ctrl+C |

---

## External Tool Requirements

| Tool | Required For | Install |
|------|--------------|---------|
| `sleuthkit` | Quick scan (core) | `brew install sleuthkit` |
| `testdisk` | Deep scan (PhotoRec) | `brew install testdisk` |
| `ffmpeg` | Video repair | `brew install ffmpeg` |
| `qpdf` | PDF repair | `brew install qpdf` |
| `untrunc` | Advanced video repair (optional) | `brew tap ottomatic-io/video && brew install untrunc` |
| `Pillow` | Image repair (JPEG/PNG, optional) | `pip install Pillow` |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PHOTOREC_TIMEOUT` | `86400` (24h) | Maximum duration for deep scans in seconds |

---

## Disclaimer

File recovery is not guaranteed. To prevent overwriting data, **never recover files to the same physical disk being scanned**. Always extract to an external drive or separate volume.
