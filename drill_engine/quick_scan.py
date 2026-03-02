import pytsk3
import os
import stat
from typing import List, Dict, Optional

class RecoverableFile:
    """Represents a file found during a scan."""
    def __init__(self, name: str, path: str, size: int, inode: int, is_deleted: bool, is_dir: bool):
        self.name = name
        self.path = path
        self.size = size
        self.inode = inode
        self.is_deleted = is_deleted
        self.is_dir = is_dir
        
    def __repr__(self):
        status = "[DELETED]" if self.is_deleted else "[ACTIVE]"
        type_str = "DIR" if self.is_dir else "FILE"
        return f"{status} {type_str} {self.path} (Size: {self.size} bytes, Inode: {self.inode})"

class TSKScanner:
    """Uses The Sleuth Kit to scan disk images or block devices."""
    
    def __init__(self, device_path: str):
        self.device_path = device_path
        self.img_info = None
        self.fs_info = None
        
    def open(self) -> bool:
        """Opens the disk image/device and initializes the FS parser."""
        try:
            self.img_info = pytsk3.Img_Info(self.device_path)
            
            # Try parsing it as a raw filesystem first
            try:
                self.fs_info = pytsk3.FS_Info(self.img_info)
                return True
            except IOError:
                # If it fails, maybe it's a volume system (partition map)
                try:
                    vol_info = pytsk3.Volume_Info(self.img_info)
                    print(f"Detected partition map on {self.device_path}. Opening first FAT/NTFS/EXT partition...")
                    # For MVP, just grab the first valid filesystem partition
                    for part in vol_info:
                        if part.flags & pytsk3.TSK_VS_PART_FLAG_ALLOC: # Allocated partition
                            try:
                                offset = part.start * vol_info.info.block_size
                                self.fs_info = pytsk3.FS_Info(self.img_info, offset=offset)
                                print(f"Successfully opened filesystem on partition: {part.addr} at offset {offset}")
                                return True
                            except IOError:
                                continue # Not a recognized filesystem, keep trying
                except IOError:
                    print(f"Failed to open filesystem or partition map on {self.device_path}.")
                    return False
                
            print(f"No recognizable filesystems found on {self.device_path}")
            return False
            
        except Exception as e:
            print(f"Error opening image {self.device_path}: {e}")
            return False
            
    def _is_deleted(self, flags) -> bool:
        """Helper to check if a file is unallocated/deleted."""
        return bool(flags & pytsk3.TSK_FS_META_FLAG_UNALLOC)
        
    def _is_dir(self, meta_type) -> bool:
        """Helper to check if the meta type is a directory."""
        return meta_type == pytsk3.TSK_FS_META_TYPE_DIR

    def scan_directory(self, directory: pytsk3.Directory, current_path: str = "/") -> List[RecoverableFile]:
        """Recursively scans a TSK directory for files (both allocated and deleted)."""
        found_files = []
        
        for entry in directory:
            try:
                if not entry.info.name or not entry.info.meta:
                    continue
                    
                name = entry.info.name.name.decode("utf-8", "replace")
                
                # Skip current and parent directory pointers
                if name in [".", ".."]:
                    continue
                    
                full_path = os.path.join(current_path, name)
                
                meta = entry.info.meta
                is_deleted = self._is_deleted(meta.flags)
                is_dir = self._is_dir(meta.type)
                size = meta.size
                inode = meta.addr
                
                # Only care about strictly deleted files for now
                if is_deleted:
                    found_files.append(
                        RecoverableFile(
                            name=name,
                            path=full_path,
                            size=size,
                            inode=inode,
                            is_deleted=is_deleted,
                            is_dir=is_dir
                        )
                    )
                
                # Recursively scan directories
                if is_dir:
                    try:
                        sub_dir = self.fs_info.open_dir(inode=inode)
                        found_files.extend(self.scan_directory(sub_dir, full_path))
                    except IOError:
                        # Sometimes deleted directories can't be parsed further
                        pass
                        
            except Exception as e:
                # Often occurs with highly corrupted inodes
                continue
                
        return found_files

    def quick_scan(self) -> List[RecoverableFile]:
        """Perform a quick scan by walking the root directory."""
        if not self.fs_info:
            print("Filesystem not opened. Call open() first.")
            return []
            
        try:
            root_dir = self.fs_info.open_dir(path="/")
            return self.scan_directory(root_dir)
        except Exception as e:
            print(f"Error scanning root directory: {e}")
            return []

    def extract_file(self, file_meta: RecoverableFile, destination_dir: str, progress_callback=None) -> bool:
        """Extracts the data from an inode and writes it to the destination."""
        if not self.fs_info:
            return False
            
        try:
            # We don't want to recover directories as files, we just recreate the path
            if file_meta.is_dir:
                return False
                
            # Create the necessary subdirectories in the destination
            # Remove leading slash to make it relative to destination_dir
            rel_path = file_meta.path.lstrip("/")
            
            # Prevent creation of hidden literal .Trashes folders which confuses macOS/users
            if rel_path.startswith(".Trashes"):
                rel_path = rel_path.replace(".Trashes", "Recovered_Trash", 1)
                
            full_dest_path = os.path.join(destination_dir, rel_path)
            
            os.makedirs(os.path.dirname(full_dest_path), exist_ok=True)
            
            # Check if file already exists and is complete
            if os.path.exists(full_dest_path) and os.path.getsize(full_dest_path) == file_meta.size:
                if progress_callback:
                    progress_callback(file_meta.size)
                return True
            
            # Open the file via inode
            f = self.fs_info.open_meta(inode=file_meta.inode)
            
            # Read the file's data block by block
            offset = 0
            size = file_meta.size
            chunk_size = 1024 * 1024 # 1MB chunks
            
            with open(full_dest_path, "wb") as out_file:
                while offset < size:
                    available = min(chunk_size, size - offset)
                    data = f.read_random(offset, available)
                    if not data:
                        break
                    out_file.write(data)
                    offset += len(data)
                    if progress_callback:
                        progress_callback(len(data))
                    
            return True
            
        except Exception as e:
            print(f"Failed to extract {file_meta.name}: {e}")
            return False
            
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 quick_scan.py <disk_image_or_device> [output_dir]")
        sys.exit(1)
        
    scanner = TSKScanner(sys.argv[1])
    if scanner.open():
        print(f"Scanning {sys.argv[1]}...")
        files = scanner.quick_scan()
        for f in files:
            print(f)
        print(f"Total deleted files found: {len(files)}")
        
        if len(sys.argv) == 3:
            out_dir = sys.argv[2]
            print(f"Extracting to {out_dir}...")
            os.makedirs(out_dir, exist_ok=True)
            for f in files:
                if not f.is_dir:
                    scanner.extract_file(f, out_dir)
                    print(f"Recovered: {f.name}")
