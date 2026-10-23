"""Dump what the review page needs to know about a board: outline box, copper layers, footprints and pads.

Run with KiCad's bundled python (it imports pcbnew), on a copy of the board, never in the project folder:

    "<kicad-bin>/python.exe" kireview_dump.py <board.kicad_pcb> <out.json>

All lengths in board mm (the editor's coordinates, y down).
"""
import json
import sys

import pcbnew

MM = 1e6


def mm(v):
    return round(v / MM, 4)


def box(b):
    return [mm(b.GetX()), mm(b.GetY()), mm(b.GetX() + b.GetWidth()), mm(b.GetY() + b.GetHeight())]


def main():
    src, out = sys.argv[1:3]
    board = pcbnew.LoadBoard(src)
    copper = [board.GetLayerName(l) for l in board.GetEnabledLayers().CuStack()]
    cu_ids = set(board.GetEnabledLayers().CuStack())
    fps, pads = [], []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        pos = fp.GetPosition()
        fps.append({'ref': ref, 'value': fp.GetValue(), 'side': 'B' if fp.IsFlipped() else 'F',
                    'x': mm(pos.x), 'y': mm(pos.y), 'bbox': box(fp.GetBoundingBox(False, False))})
        for p in fp.Pads():
            layers = [l for l in p.GetLayerSet().Seq() if l in cu_ids]
            if not layers:
                continue            # paste / mask apertures only
            pp = p.GetPosition()
            w, h = p.GetSize(layers[0])
            item = {'ref': ref, 'num': p.GetNumber(), 'net': p.GetNetname(), 'x': mm(pp.x), 'y': mm(pp.y),
                    'size': [mm(w), mm(h)], 'layers': [board.GetLayerName(l) for l in layers]}
            try:
                o = p.GetEffectivePolygon(layers[0], pcbnew.ERROR_INSIDE).Outline(0)
                item['poly'] = [[mm(o.CPoint(i).x), mm(o.CPoint(i).y)] for i in range(o.PointCount())]
            except Exception:       # shape is optional: the page falls back to the pad centre
                pass
            pads.append(item)
    edges = board.GetBoardEdgesBoundingBox()
    data = {'bbox': box(edges) if edges.GetWidth() > 0 else None, 'layers': copper, 'footprints': fps, 'pads': pads}
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


if __name__ == '__main__':
    main()
