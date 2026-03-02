import subprocess
import os
import shutil
from typing import List

class DeepScanner:
    """Wraps PhotoRec for signature-based deep scanning."""
    
    def __init__(self, device_path: str):
        self.device_path = device_path
        
    def check_photorec_installed(self) -> bool:
        """Verifies photorec is available on the system."""
        return shutil.which("photorec") is not None
        
    def run_deep_scan(self, output_dir: str) -> bool:
        """
        Executes PhotoRec in CLI batch mode.
        PhotoRec does not keep filenames, it dumps found files into recup_dir.* folders.
        """
        if not self.check_photorec_installed():
            print("Error: photorec is not installed. Run 'brew install testdisk'")
            return False
            
        os.makedirs(output_dir, exist_ok=True)
        
        try:
            # photorec /d <outdir> /cmd <device> search
            print(f"Starting PhotoRec Deep Scan on {self.device_path}...")
            print("This may take a long time and will recover raw file signatures without names.")
            
            # Since photorec requires sudo for physical disks, we assume the python script
            # was also invoked with sudo, so the subprocess inherits it.
            
            cmd = [
                "photorec",
                "/d", output_dir,
                "/cmd", self.device_path,
                "search"
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False # We don't check=True because photorec returns non-zero if user cancels
            )
            
            # Check if any recup_dir folders were created
            recup_dirs = [d for d in os.listdir(output_dir) if d.startswith("recup_dir")]
            
            if recup_dirs:
                print(f"Deep scan finished. Files dumped in: {output_dir}")
                return True
            else:
                print("No files recovered.")
                print("PhotoRec output:", result.stdout)
                return False
                
        except Exception as e:
            print(f"An error occurred running PhotoRec: {e}")
            return False
            
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python3 deep_scan.py <disk_image_or_device> <output_dir>")
        sys.exit(1)
        
    scanner = DeepScanner(sys.argv[1])
    scanner.run_deep_scan(sys.argv[2])
