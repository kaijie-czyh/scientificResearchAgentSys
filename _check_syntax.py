import ast
files = [
    'core/tools/discovery_metrics.py',
    'stages/research/agents.py',
    'stages/discovery/agents.py',
]
for f in files:
    try:
        ast.parse(open(f, encoding='utf-8').read())
        print(f'OK: {f}')
    except SyntaxError as e:
        print(f'FAIL: {f}: line {e.lineno}: {e.msg}')