#!/usr/bin/env python3
"""Check shared skills using Hermes' own loader; no model or content writes.

Run with the Python interpreter used by the existing gateway launcher.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(Path.home() / '.hermes/hermes-agent'))
    import hermes_bootstrap  # noqa: F401 -- same installed runtime as the gateway
    from tools.skills_tool import skills_list, skill_view
    index = json.loads(skills_list())
    if index.get('error'):
        raise RuntimeError('Hermes skill discovery failed')
    sources = {
        'markdown-library': project / 'skills/markdown-library/SKILL.md',
        'homepage-content': project.parent / 'homepage/skills/homepage-content/SKILL.md',
    }
    checks = []
    for name, source in sources.items():
        loaded = json.loads(skill_view(name, preprocess=False))
        if not loaded.get('success'):
            raise RuntimeError('Hermes could not load skill: ' + name)
        expected = source.read_text(encoding='utf-8')
        path = Path(loaded['_source_path'])
        check = {'name': name, 'source': str(source),
                 'same_source': path.resolve() == source.resolve(),
                 'same_content': loaded['content'] == expected,
                 'sha256': hashlib.sha256(expected.encode()).hexdigest()}
        if not (check['same_source'] and check['same_content']):
            raise RuntimeError('Shared skill differs: ' + name)
        checks.append(check)
    commands = [([str(project / '.venv/bin/markdown-library'), '--help'], project),
                (['node', 'scripts/content-client.mjs', '--help'], project.parent / 'homepage')]
    for command, cwd in commands:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, timeout=20)
        if completed.returncode:
            raise RuntimeError('Shared content tool help failed')
    vault = project.parent / 'personal-records-vault'
    result = {'skills': checks, 'content_tools_available': True,
              'vault_readable_writable': vault.is_dir() and os.access(vault, os.R_OK | os.W_OK),
              'model_calls': 0, 'content_writes': 0,
              'running_chat_reloaded': False}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
