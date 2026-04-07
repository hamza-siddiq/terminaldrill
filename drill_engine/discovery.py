import subprocess
from typing import List, Dict, Any

class Disk:
    """Represents a physical or logical disk/volume."""
    def __init__(self, device_id: str, name: str, size: int, is_physical: bool):
        self.device_id = device_id
        self.name = name
        self.size = size  # in bytes
        self.is_physical = is_physical
        
    def __repr__(self):
        type_str = "Physical" if self.is_physical else "Logical"
        size_gb = self.size / (1024**3)
        return f"<Disk {self.device_id} ({self.name}) - {size_gb:.2f} GB - {type_str}>"

def get_macos_disks() -> List[Disk]:
    """
    Calls `diskutil list -plist` and parses the output to return 
    a list of available physical disks and logical volumes.
    """
    try:
        # diskutil can output plist, which we can parse with plistlib
        # For simplicity in testing right now without importing plistlib 
        # (which requires parsing XML/Binary), let's use the -plist flag 
        # and Python's built-in plistlib.
        import plistlib
        
        result = subprocess.run(
            ["diskutil", "list", "-plist"],
            capture_output=True,
            text=False,
            check=True,
            timeout=10  # Prevent hanging indefinitely on faulty drives
        )
        
        plist_data = plistlib.loads(result.stdout)
        disks = []
        
        # AllDisksAndPartitions contains the hierarchical mapping
        for item in plist_data.get("AllDisksAndPartitions", []):
            # The top level items in AllDisksAndPartitions are usually the physical disks
            # or the top-level virtual disks (like APFS containers).
            
            # For simplicity, let's grab the device identifier of the whole disk
            device_id = item.get("DeviceIdentifier")
            size = item.get("Size", 0)
            
            # Usually /dev/diskN is physical unless it's a synthesized APFS container
            # The VolumeName isn't always present on the whole disk, but we can try to find it
            name = item.get("VolumeName", "Whole Disk")
            
            if not device_id:
                continue
                
            phys_disk = Disk(
                device_id=device_id,
                name=name,
                size=size,
                is_physical=True
            )
            disks.append(phys_disk)
            
            # Now let's grab the logical volumes inside this partition map
            for part in item.get("Partitions", []):
                part_id = part.get("DeviceIdentifier")
                part_size = part.get("Size", 0)
                part_name = part.get("VolumeName", "Unknown Volume")
                
                if part_id:
                    log_disk = Disk(
                        device_id=part_id,
                        name=part_name,
                        size=part_size,
                        is_physical=False
                    )
                    disks.append(log_disk)
                    
        return disks
        
    except Exception as e:
        print(f"Error enumerating disks: {e}")
        return []

if __name__ == "__main__":
    disks = get_macos_disks()
    for d in disks:
        print(d)
