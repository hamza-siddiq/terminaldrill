import subprocess
import os
import shutil

class DeepScanner:
    """Wraps PhotoRec for signature-based deep scanning."""
    
    def __init__(self, device_path: str):
        self.device_path = device_path
        
    def check_photorec_installed(self) -> bool:
        """Verifies photorec is available on the system."""
        return shutil.which("photorec") is not None
        
    def run_deep_scan(self, output_dir: str, output_callback=None) -> bool:
        """
        Executes PhotoRec in CLI batch mode and streams the output.
        PhotoRec does not keep filenames, it dumps found files into recup_dir.* folders.
        """
        if not self.check_photorec_installed():
            if output_callback:
                output_callback("Error: photorec is not installed. Run 'brew install testdisk'\n")
            return False
            
        os.makedirs(output_dir, exist_ok=True)
        
        try:
            if output_callback:
                output_callback(f"Starting PhotoRec Deep Scan on {self.device_path}...\n")
                output_callback("This may take a long time and will recover raw file signatures without names.\n")
            
            # photorec /d <outdir> /cmd <device>
            cmd = [
                "photorec",
                "/d", output_dir,
                "/cmd", self.device_path,
            ]
            
            # PhotoRec batch commands: select all partitions, search all file types, quit
            stdin_commands = "partitions\nsearch\nquit\n"
            
            # Use Popen to stream the output live and feed commands via stdin
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            
            # Write commands to stdin and close it so PhotoRec knows input is done
            process.stdin.write(stdin_commands)
            process.stdin.close()
            
            for line in process.stdout:
                if output_callback:
                    output_callback(line)
                    
            process.wait()
            
            # Check if any files were actually recovered
            has_recovered = False
            for item in os.listdir(output_dir):
                item_path = os.path.join(output_dir, item)
                if item.startswith("recup_dir") and os.path.isdir(item_path):
                    if os.listdir(item_path):
                        has_recovered = True
                        break
                elif os.path.isfile(item_path) and not item.startswith("."):
                    has_recovered = True
                    break

            if has_recovered:
                if output_callback:
                    output_callback(f"\nDeep scan finished. Files dumped in: {output_dir}\n")
                return True
            else:
                if output_callback:
                    output_callback("\nNo files recovered.\n")
                return False
                
        except Exception as e:
            if output_callback:
                output_callback(f"An error occurred running PhotoRec: {e}\n")
            return False
            
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python3 deep_scan.py <disk_image_or_device> <output_dir>")
        sys.exit(1)
        
    scanner = DeepScanner(sys.argv[1])
    scanner.run_deep_scan(sys.argv[2])
