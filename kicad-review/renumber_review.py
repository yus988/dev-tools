"""Carry a schematic review over a re-annotation: item keys, group keys, group titles and reasons get the new part
references (comp:R33 -> comp:R15, pin:U6 -> pin:U3, net:Net-(U6-EN) -> net:Net-(U3-EN)). The comment threads stay
as written. Stop the kireview server first (it keeps the review in memory and could write the old one back).

    python renumber_review.py <data>/reviews/sch/<slug>.json map.json

map.json is {old: new} from annotate.py (core skill kicad: ~/.claude/skills/kicad/scripts) --map.
"""
import json
import os
import sys

sys.path.insert(0, os.path.expanduser('~/.claude/skills/kicad/scripts'))
from annotate import renumber_text  # noqa: E402


def main():
    path, map_path = sys.argv[1:3]
    with open(map_path, encoding='utf-8') as f:
        rmap = json.load(f)
    with open(path, encoding='utf-8') as f:
        d = json.load(f)

    def key(k):
        kind, _, rest = k.partition(':')
        return k if kind == 'group' or not rest else kind + ':' + renumber_text(rest, rmap)

    d['items'] = {key(k): v for k, v in d['items'].items()}
    for g in d.get('groups', []):
        g['keys'] = [key(k) for k in g['keys']]
        g['title'] = renumber_text(g['title'], rmap)
        g['rationale'] = renumber_text(g['rationale'], rmap)
    d['rev'] = int(d.get('rev', 0)) + 1
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
        f.write('\n')
    os.replace(tmp, path)
    for g in d.get('groups', []):
        print(g['id'], g['title'], g['keys'])


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
