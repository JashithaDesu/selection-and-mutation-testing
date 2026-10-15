"""record.py - write one environment's measurements to env-results/<env>.json

Usage: python3 env/record.py <env_name> <setup_s> <run_s> <exit_code> [python_version]

Combines the timings measured by the workflow with the fingerprint of the
experiment's results (env/fingerprint.py), so every environment produces
one comparable row.
"""
import json, os, subprocess, sys

env, setup_s, run_s, code = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
py = sys.argv[5] if len(sys.argv) > 5 else "unknown"

row = {"env": env, "python": py, "setup_s": setup_s, "run_s": run_s, "exit": code}
if code == 0:
    fp = subprocess.run([sys.executable, "env/fingerprint.py", "results"],
                        capture_output=True, text=True, check=True)
    row.update(json.loads(fp.stdout))

os.makedirs("env-results", exist_ok=True)
with open(f"env-results/{env}.json", "w") as f:
    json.dump(row, f, indent=2, sort_keys=True)
print(json.dumps(row, indent=2, sort_keys=True))
