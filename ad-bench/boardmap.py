"""Board map for adscope's guided probing: 3D renders of a KiCad board + pixel
positions of every pad, so a procedure can say "CH1 on C21 pad 1" and the page
can circle it on the picture.

  python boardmap.py <board.kicad_pcb> <name> [--data <folder>]   # -> <folder>/boards/<name>/{top,bottom}.jpg + pads.json

<folder> is the adscope data folder (default: $AD_BENCH_DATA or the current directory).

Needs KiCad 10 (its bundled Python for pcbnew, kicad-cli for the renders) and
Pillow in the Python that runs this script. The board file is only read; a
model-less copy goes to a temp folder to find the board outline in the render.
"""
import argparse
import json
import os
import subprocess
import tempfile

from PIL import Image

KICAD = r'C:\Program Files\KiCad\10.0\bin'
KPY = os.path.join(KICAD, 'python.exe')
KCLI = os.path.join(KICAD, 'kicad-cli.exe')
DATA_ENV = 'AD_BENCH_DATA'     # same as adbench.py (not imported: that needs dwf.dll)
W, H = 1800, 1400

DUMP = r'''
import json, sys, pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
r = b.GetBoardEdgesBoundingBox()
pads = []
for f in b.GetFootprints():
    back = f.IsFlipped()
    for p in f.Pads():
        pos = p.GetPosition()
        pads.append({'ref': f.GetReference(), 'pad': p.GetNumber(), 'net': p.GetNetname(),
                     'value': f.GetValue(), 'tht': p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH,
                     'side': 'bottom' if back else 'top',
                     'x_mm': round(pcbnew.ToMM(pos.x), 4), 'y_mm': round(pcbnew.ToMM(pos.y), 4)})
    f.Models().clear()
json.dump({'edge_mm': [pcbnew.ToMM(r.GetLeft()), pcbnew.ToMM(r.GetTop()),
                       pcbnew.ToMM(r.GetRight()), pcbnew.ToMM(r.GetBottom())], 'pads': pads},
          open(sys.argv[2], 'w', encoding='utf-8'))
pcbnew.SaveBoard(sys.argv[3], b)
'''


def render(pcb, side, out, quality):
    subprocess.run([KCLI, 'pcb', 'render', '--side', side, '-w', str(W), '-h', str(H),
                    '--quality', quality, '-o', out, pcb], check=True, capture_output=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('pcb', help='.kicad_pcb (only read)')
    p.add_argument('name', help='board name = folder under boards/')
    p.add_argument('--data', help='data folder (default: $%s or the current directory)' % DATA_ENV)
    args = p.parse_args()
    pcb, name = os.path.abspath(args.pcb), args.name
    out = os.path.join(os.path.abspath(args.data or os.environ.get(DATA_ENV) or os.getcwd()), 'boards', name)
    os.makedirs(out, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        helper = os.path.join(tmp, 'dump.py')
        with open(helper, 'w', encoding='utf-8') as f:
            f.write(DUMP)
        dump, bare = os.path.join(tmp, 'pads.json'), os.path.join(tmp, 'bare.kicad_pcb')
        subprocess.run([KPY, helper, pcb, dump, bare], check=True)
        info = json.load(open(dump, encoding='utf-8'))
        x0, y0, x1, y1 = info['edge_mm']
        sides = {}
        for side in ('top', 'bottom'):
            full, plain = os.path.join(tmp, side + '.png'), os.path.join(tmp, side + '_bare.png')
            render(pcb, side, full, 'high')
            render(bare, side, plain, 'basic')
            # The model-less render shows only the board, so its alpha bbox is the
            # outline; the full render uses the same camera (fit to the board).
            bx0, by0, bx1, by1 = Image.open(plain).getchannel('A').getbbox()
            sx, sy = (bx1 - bx0) / (x1 - x0), (by1 - by0) / (y1 - y0)
            im = Image.open(full).convert('RGBA')
            bg = Image.new('RGBA', im.size, (250, 250, 250, 255))
            bg.alpha_composite(im)
            bg.convert('RGB').save(os.path.join(out, side + '.jpg'), quality=88)
            sides[side] = {'image': side + '.jpg', 'size': list(im.size), 'px_per_mm': [sx, sy],
                           'board_px': [bx0, by0, bx1, by1], 'mirrored': side == 'bottom'}
        for p in info['pads']:
            for side, s in sides.items():
                if p['side'] != side and not p['tht']:
                    continue
                bx0, by0, bx1, _ = s['board_px']
                u = (p['x_mm'] - x0) if side == 'top' else (x1 - p['x_mm'])
                p.setdefault('px', {})[side] = [round(bx0 + u * s['px_per_mm'][0], 1),
                                                round(by0 + (p['y_mm'] - y0) * s['px_per_mm'][1], 1)]
        meta = {'board': name, 'source': pcb, 'edge_mm': info['edge_mm'], 'sides': sides, 'pads': info['pads']}
        with open(os.path.join(out, 'pads.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
    print('wrote', out, len(info['pads']), 'pads')


if __name__ == '__main__':
    main()
