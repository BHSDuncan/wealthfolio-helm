#!/usr/bin/env python3
"""Small offline Markdown/YAML/link checker for this single-chart project."""
from pathlib import Path
import re
import yaml

root = Path(__file__).resolve().parents[1]
errors = []
paths = [p for folder in ('docs', 'examples', 'charts/wealthfolio') for p in (root / folder).rglob('*') if p.is_file()]
paths += list(root.glob('*.md'))
for path in paths:
    if path.suffix == '.md':
        text = path.read_text()
        for number, line in enumerate(text.splitlines(), 1):
            if line.rstrip() != line:
                errors.append(f'{path.relative_to(root)}:{number}: trailing whitespace')
        for link in re.findall(r'\[[^\]]*\]\(([^)]+)\)', text):
            target = link.split('#', 1)[0]
            if target and not re.match(r'[a-z]+://|mailto:|<', target) and not (path.parent / target).exists():
                errors.append(f'{path.relative_to(root)}: broken local link {target}')
    elif path.suffix in ('.yaml', '.yml') and 'templates' not in path.parts:
        try:
            list(yaml.safe_load_all(path.read_text()))
        except yaml.YAMLError:
            errors.append(f'{path.relative_to(root)}: malformed YAML')
for error in errors:
    print(error)
if errors:
    raise SystemExit(1)
print('Documentation local links/whitespace and non-template YAML passed')
