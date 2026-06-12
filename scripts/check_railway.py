import os
import subprocess
import sys
import webbrowser

# ANSI Color Codes
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

# Force UTF-8 encoding for stdout/stderr so unicode characters/emojis print correctly on Windows
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

RAILWAY_EXE = r"C:\Users\min\.railway\bin\railway.exe"
RAILWAY_URL = "https://railway.app/login"

def print_banner():
    print(f"""
{BLUE}{BOLD}╔══════════════════════════════════════════════╗
║                                              ║
║        🚂  RAILWAY STATUS & LOG CHECK        ║
║                                              ║
╚══════════════════════════════════════════════╝{RESET}
""")

def run_command(args):
    """Run a subprocess command and return code, stdout, and stderr."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            shell=True
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return -1, "", str(e)

def check_railway_cli():
    if not os.path.exists(RAILWAY_EXE):
        print(f"  {RED}[ERROR]{RESET} Railway CLI executable not found at:")
        print(f"          {RAILWAY_EXE}")
        print(f"  {YELLOW}[INFO]{RESET}  Please install Railway CLI or place it in the correct directory.")
        return False

    # Check whoami
    print(f"  {CYAN}[INFO]{RESET}  Checking Railway CLI connection...")
    code, stdout, stderr = run_command([RAILWAY_EXE, "whoami"])
    
    if code != 0 or "not logged in" in stdout.lower() or not stdout:
        print(f"  {RED}[ERROR]{RESET} Railway CLI is NOT connected / logged in.")
        if stdout:
            print(f"          CLI output: {stdout}")
        if stderr:
            print(f"          CLI error: {stderr}")
        
        print(f"  {YELLOW}[ACTION]{RESET} Opening browser to Railway login immediately...")
        webbrowser.open(RAILWAY_URL)
        return False

    print(f"  {GREEN}[SUCCESS]{RESET} {stdout}")
    return True

def fetch_and_analyze_logs():
    print(f"  {CYAN}[INFO]{RESET}  Fetching latest application deployment logs...")
    # Fetch latest 150 lines of deploy logs (non-streaming)
    code, stdout, stderr = run_command([RAILWAY_EXE, "logs", "--lines", "150"])
    
    if code != 0:
        print(f"  {RED}[ERROR]{RESET} Failed to fetch Railway logs.")
        if stderr:
            print(f"          CLI error: {stderr}")
        print(f"  {YELLOW}[ACTION]{RESET} Opening browser to Railway dashboard...")
        webbrowser.open("https://railway.app")
        return

    if not stdout:
        print(f"  {YELLOW}[WARN]{RESET}  No log output was returned from Railway.")
        return

    lines = stdout.splitlines()
    print(f"\n{BLUE}{BOLD}--- LATEST DEPLOYMENT LOGS (Last {len(lines)} lines) ---{RESET}\n")
    
    errors_found = []
    for line in lines:
        # Highlight lines with common error markers
        lower_line = line.lower()
        is_error = False
        
        if "[error]" in lower_line or "traceback" in lower_line or "exception" in lower_line or "timeout" in lower_line:
            is_error = True
            errors_found.append(line)

        if is_error:
            print(f"{RED}{BOLD}{line}{RESET}")
        elif "[warn]" in lower_line or "warning" in lower_line:
            print(f"{YELLOW}{line}{RESET}")
        elif "[success]" in lower_line or "online" in lower_line:
            print(f"{GREEN}{line}{RESET}")
        else:
            print(line)
            
    print(f"\n{BLUE}{BOLD}-------------------------------------------------------{RESET}")
    
    if errors_found:
        print(f"\n  {RED}{BOLD}[ALERT]{RESET} Found {len(errors_found)} error/warning/exception line(s) in the logs:")
        for idx, err in enumerate(errors_found[-5:], 1):
            print(f"          {idx}. {RED}{err}{RESET}")
        if len(errors_found) > 5:
            print(f"          ... and {len(errors_found) - 5} more.")
    else:
        print(f"\n  {GREEN}[SUCCESS]{RESET} No obvious errors/exceptions detected in the last {len(lines)} log lines!")

def main():
    # Set console code page to UTF-8 on Windows for proper emojis
    if sys.platform == "win32":
        os.system("chcp 65001 > nul")
        
    print_banner()
    if check_railway_cli():
        fetch_and_analyze_logs()

if __name__ == "__main__":
    main()
