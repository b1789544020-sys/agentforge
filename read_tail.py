import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
t = open(r'C:\Users\86159\Desktop\agentforge\README.md', encoding='utf-8').read()
lines = t.splitlines()
for i, l in enumerate(lines[449:472], start=450):
    print(f'{i:4d}: {l}')
