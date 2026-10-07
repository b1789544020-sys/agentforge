"""Dump recent backend errors with context."""
import subprocess
out = subprocess.check_output(
    ["docker", "logs", "--tail", "2000", "agentforge-api"],
    text=True, stderr=subprocess.STDOUT)
lines = out.splitlines()
for i, line in enumerate(lines):
    if "ASGI" in line:
        print("===", i)
        for j in range(max(0, i-2), min(len(lines), i+30)):
            print(j, ":", lines[j][:300])
