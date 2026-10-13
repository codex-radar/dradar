"""Read the actual task package Git pin; no fetch or worker execution."""
import subprocess

def local_deep_swe_commit(tasks_root):
    try:
        p=subprocess.run(['git','-C',str(tasks_root),'rev-parse','HEAD'],capture_output=True,text=True,timeout=10)
    except (OSError,subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode==0 else None
