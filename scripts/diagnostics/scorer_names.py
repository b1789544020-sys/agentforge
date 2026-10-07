import subprocess
out = subprocess.check_output(
    ['docker', 'exec', 'agentforge-api', 'grep', '-nE', r'name\s*[:=]', '/app/backend/app/eval/scorer.py'],
    text=True)
print(out)
