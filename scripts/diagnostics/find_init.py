"""Find where SQLite schema is initialised in the backend."""
import os, re
hits = []
for root, _, files in os.walk(r'C:\Users\86159\Desktop\agentforge\backend\app'):
    for f in files:
        if not f.endswith('.py'):
            continue
        p = os.path.join(root, f)
        t = open(p, encoding='utf-8').read()
        for kw in ('create_all', 'create_async', 'Base.metadata', 'init_db', 'migrate', 'alembic'):
            for m in re.finditer(re.escape(kw), t):
                line = t[:m.start()].count('\n') + 1
                hits.append((p, line, kw))
for h in hits[:25]:
    print(h)
