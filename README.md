# Terminal Drill
**Professional File Recovery for the Terminal**

Terminal Drill is a CLI-based file recovery utility for macOS. It provides an interface for recovering deleted files and carving data from disk images or physical drives.

## Installation and Setup

1. **Install System Dependencies**:
    `brew install sleuthkit testdisk ffmpeg qpdf`
    `brew tap ottomatic-io/video && brew install untrunc`

2. **Clone and Setup Environment**:
    `git clone https://github.com/hamza-siddiq/terminaldrill.git && cd terminaldrill && python3 -m venv venv && source venv/bin/activate && pip install pytsk3 rich`

## Usage

**Terminal Drill requires root privileges (sudo)** to access raw disk blocks and unmount volumes.

Run the application:
`sudo venv/bin/python3 drill_ui/app.py`

### Workflow

1. **Select Drive**: Choose the target disk from the generated table.
2. **Select Scan Type**: 
    - `quick`: Best for recently deleted files.
    - `deep`: Best for formatted or corrupted volumes.
3. **Review Results**: Browse the tree of found files.
4. **Configure Extraction**:
    - Set destination path (default: `./recovered_files`).
    - Filter by filename or extract everything.
    - Choose extraction order (current, asc, or desc).
5. **Monitor Progress**: View real-time status via nested progress bars.
6. **Repair Corrupted Files**: After extraction, scan for corruption and attempt automated repairs. You can also select `repair` mode to fix files from a previous recovery.

## Key Features

- **Quick Scan (TSK)**: Uses The Sleuth Kit (pytsk3) to identify deleted files by analyzing filesystem metadata.
- **Deep Scan (PhotoRec)**: Integrates with PhotoRec for file carving based on signatures when metadata is missing.
- **Smart Disk Discovery**: Enumerates physical disks and logical volumes on macOS with size information.
- **Conditional Extraction**: Filter by filename and sort by size (Ascending/Descending).
- **Progress Tracking**: Nested progress bars and real-time extraction speed monitoring.
- **Corrupted File Repair**: Detects corruption (truncated headers, missing EOF markers, zero-filled regions) and repairs files using ffmpeg (video), Pillow (images), zip/qpdf (archives/PDFs).

## External Tool Requirements

| Tool | Required For | Install |
|------|--------------|---------|
| `sleuthkit` | Quick scan (core) | `brew install sleuthkit` |
| `testdisk` | Deep scan (PhotoRec) | `brew install testdisk` |
| `untrunc` | Advanced video repair | `brew tap ottomatic-io/video && brew install untrunc` |
| `ffmpeg` | Basic video repair | `brew install ffmpeg` |
| `qpdf` | PDF repair | `brew install qpdf` |
| `Pillow` | Image repair (JPEG/PNG) | `pip install Pillow` |

## Disclaimer

File recovery is not guaranteed. To prevent overwriting data, avoid recovering files to the same physical disk being scanned. Always extract to an external drive or separate volume.
