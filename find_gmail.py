import json, sys, os, glob
sys.stdout.reconfigure(encoding='utf-8')

projects = r'C:/Users/donal/.claude/projects'
results = []

for project_dir in os.listdir(projects):
    path = os.path.join(projects, project_dir)
    if not os.path.isdir(path):
        continue
    for jf in glob.glob(os.path.join(path, '*.jsonl')):
        try:
            turns = []
            pending = None
            with open(jf, encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    t = obj.get('type')
                    if t == 'user' and not obj.get('isMeta'):
                        c = obj.get('message', {}).get('content', '')
                        if isinstance(c, str) and c and not c.startswith('<'):
                            pending = {'u': c, 'a': '', 'ts': obj.get('timestamp', '')}
                    elif t == 'assistant' and pending:
                        parts = [b.get('text', '') for b in (obj.get('message', {}).get('content', []) or [])
                                 if isinstance(b, dict) and b.get('type') == 'text']
                        pending['a'] = ' '.join(parts)
                        turns.append(pending)
                        pending = None
            for turn in turns:
                hay = (turn['u'] + ' ' + turn['a']).lower()
                if 'gmail' in hay and any(w in hay for w in ('disappear', 'missing', 'logout', 'login', 'gone', 'lost', 'removed')):
                    results.append({'file': jf, 'project': project_dir, 'ts': turn['ts'], 'u': turn['u'][:400], 'a': turn['a'][:800]})
        except Exception:
            pass

results.sort(key=lambda x: x['ts'])
for r in results:
    print('===', r['project'], '|', r['ts'][:16], '===')
    print('YOU:', r['u'])
    print('CLAUDE:', r['a'])
    print()
