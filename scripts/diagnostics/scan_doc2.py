"""Find stale path / scorer strings in 操作步骤.md, ignore encoding."""
import glob, re
path = glob.glob('C:/Users/86159/Desktop/agentforge/????.md')[0]
text = open(path, encoding='utf-8').read()
patterns = [
    r'/api/v1/runs/',
    r'/api/v1/evals/',
    r'/api/v1/memory',
    r'/cases\b',
    r'/progress',
    r'GET\s+.*/runs/',
    r'scorer_name"\s*:\s*"exact"',
    r'scorer_name":"exact"',
    r'\"composite\"',
]
for pat in patterns:
    matches = list(re.finditer(pat, text))
    if matches:
        print(f'{pat!r}: {len(matches)} hits')
        for m in matches[:5]:
            line = text[:m.start()].count('\n')+1
            print(f'  L{line}: ...{text[max(0,m.start()-25):m.end()+25]}...')
