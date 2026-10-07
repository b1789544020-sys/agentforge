"""Scan the 操作说明 document for stale API/scorer references."""
import re, glob
path = glob.glob('C:/Users/86159/Desktop/agentforge/????.md')[0]
text = open(path, encoding='utf-8').read()
print('PATH:', path)
print('LINES:', text.count('\n'))
print('---')
patterns = [
    (r'/api/v1/evals[^\s\`"]*', 'evals path'),
    (r'/api/v1/runs/[^\s\`"]*', 'runs path'),
    (r'/api/v1/agents/[^\s\`"]*', 'agents path'),
    (r'\"scorer_name\"\s*:\s*\"[a-z_]+\"', 'scorer_name'),
    (r'/cases\b', '/cases endpoint'),
    (r'/progress\b', '/progress endpoint'),
    (r'scorer[^\s\`",]*\"[a-z_]+\"', 'scorer name'),
    (r'expected_tool', 'expected_tool'),
]
for pat, label in patterns:
    matches = list(re.finditer(pat, text))
    if not matches:
        continue
    print(f'\n## {label} ({len(matches)} hits)')
    for m in matches:
        line = text[:m.start()].count('\n') + 1
        # show 60 chars context
        start = max(0, m.start()-30); end = min(len(text), m.end()+30)
        ctx = text[start:end].replace('\n', ' ')
        print(f'  L{line:>4}  {ctx}')
