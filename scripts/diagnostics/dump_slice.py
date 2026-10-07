"""Dump a slice of the 操作步骤.md around L700."""
import glob, sys
path = glob.glob('C:/Users/86159/Desktop/agentforge/????.md')[0]
text = open(path, encoding='utf-8').read()
start = int(sys.argv[1]); end = int(sys.argv[2])
for i, line in enumerate(text.splitlines()[start:end], start=start+1):
    print(f"{i:4d}  {line}")
