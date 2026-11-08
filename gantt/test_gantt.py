"""Tests of gantt.py: ops, render, dumps, embed, changes, the page API, the HTTP guards, share / png / pdf.
python test_gantt.py   (the png / pdf tests are skipped when no Edge / Chrome is found)"""
import argparse
import contextlib
import copy
import hashlib
import io
import json
import math
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import gantt

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, 'sample.gantt.json')
SVG = '{http://www.w3.org/2000/svg}'


def sample():
    return gantt.load(SAMPLE)


def small():
    return gantt.normalize({
        'title': 'T', 'from': '2026-11-02', 'to': '2026-11-29',
        'rows': [{'id': 'a', 'title': 'Row A', 'owner': 'X', 'items': [
                     {'from': '2026-11-09', 'to': '2026-11-13', 'label': 'Job', 'kind': 'plan'},
                     {'on': '2026-11-20', 'label': 'Due', 'kind': 'due'}]},
                 {'id': 'g', 'group': 'Group'},
                 {'id': 'b', 'title': 'Row B'}]})


class OpsTest(unittest.TestCase):
    def test_move_and_resize_with_log_text(self):
        d = small()
        t = gantt.apply(d, [{'op': 'item', 'row': 'a', 'i': 0, 'expect': {'from': '2026-11-09', 'to': '2026-11-13'},
                             'set': {'to': '2026-11-20'}}])
        self.assertEqual(d['rows'][0]['items'][0]['to'], '2026-11-20')
        self.assertEqual(t, ['「Row A」の「Job」: 11/9〜11/13 → 11/9〜11/20'])
        self.assertEqual(d['log'][-1]['by'], 'user')
        self.assertEqual(d['log'][-1]['text'], t[0])
        gantt.apply(d, [{'op': 'item', 'row': 'a', 'i': 0, 'set': {'from': '2026-11-16', 'to': '2026-11-17'}}])
        self.assertEqual(d['rows'][0]['items'][0]['from'], '2026-11-16')

    def test_reversed_dates_are_swapped(self):
        d = small()
        gantt.apply(d, [{'op': 'item', 'row': 'a', 'i': 0, 'set': {'from': '2026-11-25'}}])
        it = d['rows'][0]['items'][0]
        self.assertEqual((it['from'], it['to']), ('2026-11-13', '2026-11-25'))

    def test_conflict_on_stale_expect_changes_nothing(self):
        d = small()
        before = copy.deepcopy(d)
        with self.assertRaises(gantt.Conflict):
            gantt.apply(d, [{'op': 'item', 'row': 'a', 'i': 0, 'expect': {'from': '2026-11-10'}, 'set': {'label': 'x'}}])
        with self.assertRaises(gantt.Conflict):
            gantt.apply(d, [{'op': 'del_item', 'row': 'a', 'i': 7}])
        self.assertEqual(d, before)

    def test_a_failing_op_in_a_batch_applies_nothing(self):
        d = small()
        before = copy.deepcopy(d)
        with self.assertRaises(ValueError):
            gantt.apply(d, [{'op': 'row', 'row': 'a', 'set': {'owner': 'Y'}}, {'op': 'row', 'row': 'zz', 'set': {}}])
        self.assertEqual(d, before)

    def test_null_removes_keys_and_converts_bar_and_milestone(self):
        d = small()
        gantt.apply(d, [{'op': 'item', 'row': 'a', 'i': 0, 'set': {'label': None}}])
        self.assertNotIn('label', d['rows'][0]['items'][0])
        gantt.apply(d, [{'op': 'item', 'row': 'a', 'i': 0, 'set': {'on': '2026-11-10', 'from': None, 'to': None}}])
        self.assertEqual(d['rows'][0]['items'][0], {'kind': 'plan', 'on': '2026-11-10'})
        gantt.apply(d, [{'op': 'item', 'row': 'a', 'i': 1, 'set': {'on': None, 'from': '2026-11-23', 'to': '2026-11-24'}}])
        self.assertEqual(d['rows'][0]['items'][1]['to'], '2026-11-24')
        with self.assertRaises(ValueError):                      # neither a bar nor a milestone
            gantt.apply(d, [{'op': 'item', 'row': 'a', 'i': 1, 'set': {'to': None}}])
        with self.assertRaises(ValueError):                      # both
            gantt.apply(d, [{'op': 'item', 'row': 'a', 'i': 1, 'set': {'on': '2026-11-23'}}])

    def test_add_and_delete_items(self):
        d = small()
        t = gantt.apply(d, [{'op': 'add_item', 'row': 'b', 'item': {'label': 'New', 'to': '2026-11-20', 'from': '2026-11-16'}}])
        self.assertEqual(t, ['「Row B」に「New」（11/16〜11/20）を追加'])
        self.assertEqual(list(d['rows'][2]['items'][0]), ['from', 'to', 'label'])
        t = gantt.apply(d, [{'op': 'del_item', 'row': 'b', 'i': 0, 'expect': {'label': 'New'}}])
        self.assertEqual(t, ['「Row B」の「New」（11/16〜11/20）を削除'])
        with self.assertRaises(ValueError):
            gantt.apply(d, [{'op': 'add_item', 'row': 'g', 'item': {'on': '2026-11-16'}}])      # group rows have no items
        with self.assertRaises(ValueError):
            gantt.apply(d, [{'op': 'add_item', 'row': 'b', 'item': {'on': 'soon'}}])

    def test_rows(self):
        d = small()
        self.assertEqual(gantt.apply(d, [{'op': 'row', 'row': 'a', 'set': {'owner': 'Y'}}]), ['「Row A」の担当: X → Y'])
        gantt.apply(d, [{'op': 'row', 'row': 'a', 'set': {'owner': None}}])
        self.assertNotIn('owner', d['rows'][0])
        t = gantt.apply(d, [{'op': 'add_row', 'after': 'a', 'row': {'title': 'Mid'}}])
        self.assertEqual(t, ['行「Mid」を追加'])
        self.assertEqual([r['id'] for r in d['rows']], ['a', 'r1', 'g', 'b'])
        t = gantt.apply(d, [{'op': 'add_row', 'after': None, 'row': {'group': 'End'}}])
        self.assertEqual(d['rows'][-1]['group'], 'End')
        with self.assertRaises(ValueError):
            gantt.apply(d, [{'op': 'add_row', 'row': {'id': 'a', 'title': 'dup'}}])
        self.assertEqual(gantt.apply(d, [{'op': 'move_row', 'row': 'b', 'dir': -1}]), ['「Row B」を上へ移動'])
        self.assertEqual([r['id'] for r in d['rows']][:4], ['a', 'r1', 'b', 'g'])
        self.assertEqual(gantt.apply(d, [{'op': 'move_row', 'row': 'a', 'dir': -1}]), [])      # already first: nothing
        self.assertEqual(gantt.apply(d, [{'op': 'del_row', 'row': 'r1'}]), ['行「Mid」を削除'])
        with self.assertRaises(ValueError):                      # a row with items cannot become a group
            gantt.apply(d, [{'op': 'row', 'row': 'a', 'set': {'group': 'G'}}])

    def test_meta(self):
        d = small()
        self.assertEqual(gantt.apply(d, [{'op': 'meta', 'set': {'to': '2026-12-06'}}]), ['全体の終了日: 11/29 → 12/6'])
        with self.assertRaises(ValueError):
            gantt.apply(d, [{'op': 'meta', 'set': {'from': '2027-01-01'}}])

    def test_log_times_increase_and_are_trimmed(self):
        d = small()
        for _ in range(3):
            gantt.apply(d, [{'op': 'row', 'row': 'b', 'set': {'note': str(len(d.get('log', [])))}}])
        times = [e['time'] for e in d['log']]
        self.assertEqual(times, sorted(set(times)))
        d['log'] = [{'time': '2026-01-01 00:00:00', 'by': 'user', 'text': str(i)} for i in range(gantt.LOG_KEEP)]
        gantt.apply(d, [{'op': 'row', 'row': 'b', 'set': {'note': 'x'}}])
        self.assertEqual(len(d['log']), gantt.LOG_KEEP)

    def test_rows_without_id_get_unique_ids(self):
        d = gantt.normalize({'rows': [{'id': 'r1'}, {'title': 'x'}, {'title': 'y'}]})
        self.assertEqual([r['id'] for r in d['rows']], ['r1', 'r2', 'r3'])


class DumpsTest(unittest.TestCase):
    def test_one_item_per_line_and_round_trip(self):
        d = small()
        d['off'] = [{'from': '2026-11-23', 'to': '2026-11-24', 'label': 'Off'}]
        d['log'] = [{'time': '2026-11-04 10:12:00', 'by': 'user', 'text': '日本語'}]
        text = gantt.dumps(d)
        self.assertEqual(json.loads(text), d)
        self.assertIn('\n    {"from": "2026-11-09", "to": "2026-11-13", "label": "Job", "kind": "plan"},\n', text)
        self.assertIn('\n  {"id": "g", "group": "Group"},\n', text)
        self.assertIn('日本語', text)                             # not escaped
        self.assertTrue(text.endswith('}\n'))
        self.assertTrue(text.startswith('{\n "title"'))

    def test_sample_is_canonical(self):
        with open(SAMPLE, encoding='utf-8') as f:
            text = f.read()
        self.assertEqual(gantt.dumps(json.loads(text)), text)

    def test_save_is_atomic_and_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, 'a.gantt.json')
            gantt.save(p, small())
            self.assertEqual(os.listdir(t), ['a.gantt.json'])
            self.assertEqual(gantt.load(p), small())


class RenderTest(unittest.TestCase):
    def test_sample_parses_and_is_small(self):
        for kw in ({}, {'interactive': True}, {'legend': True}):
            svg = gantt.render(sample(), **kw)
            root = ET.fromstring(svg)
            self.assertEqual(root.tag, SVG + 'svg')
            self.assertIn('gantt', root.get('class'))
        self.assertLess(len(gantt.render(sample())), 20000)

    def test_interactive_hooks_only_when_interactive(self):
        plain, ui = gantt.render(sample()), gantt.render(sample(), interactive=True)
        for s in ('data-r=', 'data-x0', 'class="hit"', 'class="row"', 'class="ttl"', 'data-from=', 'data-on='):
            self.assertNotIn(s, plain)
            self.assertIn(s, ui)
        root = ET.fromstring(ui)
        self.assertEqual(root.get('data-d0'), '2026-11-02')
        self.assertEqual(root.get('data-d1'), '2027-01-10')
        self.assertEqual(root.get('data-dw'), '14')
        rows = [g for g in root.iter(SVG + 'g') if g.get('class') == 'row']
        self.assertEqual(len(rows), len(sample()['rows']))
        its = [g for g in root.iter(SVG + 'g') if (g.get('class') or '').startswith('it ')]
        self.assertEqual(len(its), 17)
        enc = [g for g in its if g.get('data-r') == 'encl']
        self.assertEqual([g.get('data-i') for g in enc], ['0', '1', '2'])
        self.assertEqual(enc[0].get('data-from'), '2026-11-02')

    def test_geometry(self):
        d = small()
        root = ET.fromstring(gantt.render(d, day=14, interactive=True, today='2026-11-03'))
        x0 = int(root.get('data-x0'))
        self.assertEqual(x0, 240 + 80)
        bar = [r for r in root.iter(SVG + 'rect') if (r.get('class') or '').startswith('b k-plan')][0]
        self.assertEqual(float(bar.get('x')), x0 + 7 * 14)       # 11/9 is 7 days after 11/2
        self.assertEqual(float(bar.get('width')), 5 * 14)
        self.assertEqual(float(root.get('width')), x0 + 28 * 14)
        d['rows'][0].pop('owner')
        self.assertEqual(int(ET.fromstring(gantt.render(d, interactive=True)).get('data-x0')), 240)
        d['label_width'] = 300
        self.assertEqual(int(ET.fromstring(gantt.render(d, interactive=True)).get('data-x0')), 300)

    def test_items_outside_the_range_are_clipped_or_skipped(self):
        d = small()
        d['rows'][2]['items'] = [{'from': '2026-10-20', 'to': '2026-11-04', 'label': 'Edge'},
                                 {'from': '2026-12-20', 'to': '2026-12-24', 'label': 'Gone'},
                                 {'on': '2026-12-01', 'label': 'Gone too'}]
        svg = gantt.render(d, interactive=True)
        self.assertIn('Edge', svg)
        self.assertNotIn('Gone', svg)
        root = ET.fromstring(svg)
        bar = [r for r in root.iter(SVG + 'rect') if r.get('class') == 'b k-plan' and r.get('x') == '320'][0]
        self.assertEqual(float(bar.get('width')), 3 * 14)

    def test_truncated_text_gets_a_title(self):
        d = small()
        d['rows'][2]['items'] = [{'from': '2026-11-02', 'to': '2026-11-04', 'label': 'A rather long label'},
                                 {'from': '2026-11-05', 'to': '2026-11-06', 'label': 'Next'}]
        svg = gantt.render(d)
        self.assertIn('…<title>A rather long label</title>', svg)
        ET.fromstring(svg)
        d['rows'][2]['items'] = [{'from': '2026-11-02', 'to': '2026-11-04', 'label': 'Fits'}]      # inside the bar
        self.assertNotIn('<title>', gantt.render(d).split('Row B')[1])

    def test_label_goes_right_of_the_bar_when_there_is_room(self):
        d = small()
        d['rows'][2]['items'] = [{'from': '2026-11-02', 'to': '2026-11-03', 'label': 'Outside the bar'}]
        svg = gantt.render(d)
        self.assertIn('class="bt mu" x="%s"' % gantt.f(320 + 2 * 14 + 4), svg)
        self.assertNotIn('…', svg)

    def test_legend_lists_used_kinds_only(self):
        d = small()
        d['legend'] = {'due': 'Order'}
        svg = gantt.render(d, legend=True)
        self.assertIn('>Order<', svg)
        self.assertIn('>作業<', svg)
        self.assertNotIn('>待ち<', svg)
        self.assertNotIn('>Order<', gantt.render(d))
        self.assertGreater(float(ET.fromstring(svg).get('height')), float(ET.fromstring(gantt.render(d)).get('height')))

    def test_links_are_resolved_and_only_web_urls_pass(self):
        d = small()
        d['link_base'] = 'http://example.localhost/'
        d['rows'][0]['link'] = 'plans/a.html'
        self.assertIn('<a class="lnk" href="http://example.localhost/plans/a.html" target="_blank">', gantt.render(d))
        self.assertIn('class="go" data-href="http://example.localhost/plans/a.html"', gantt.render(d, interactive=True))
        self.assertNotIn('<a ', gantt.render(d, interactive=True))
        d['rows'][0]['link'] = 'https://example.com/x'
        self.assertIn('href="https://example.com/x"', gantt.render(d))
        d['rows'][0]['link'] = 'javascript:alert(1)'
        self.assertNotIn('javascript', gantt.render(d))
        del d['link_base']
        d['rows'][0]['link'] = 'plans/a.html'
        self.assertIn('href="file:///', gantt.render(d, base_dir=HERE))

    def test_text_is_escaped(self):
        d = small()
        d['rows'][2]['title'] = '<b>&"x"'
        ET.fromstring(gantt.render(d))

    def test_week_and_month_headers_and_today(self):
        svg = gantt.render(small(), today='2026-11-10')
        for s in ('>11月<', '>11/2<', '>11/9<', 'class="td"'):
            self.assertIn(s, svg)
        self.assertNotIn('class="td"', gantt.render(small(), today='2026-12-31'))

    def test_day_lines_only_when_wide(self):
        self.assertNotIn('class="gd"', gantt.render(small(), day=8))
        self.assertIn('class="gd"', gantt.render(small(), day=14))

    def test_no_style_attributes_and_short_numbers(self):
        svg = gantt.render(sample(), interactive=True, legend=True)
        self.assertNotIn(' style="', svg)
        self.assertNotRegex(svg, r'\d\.\d{3}')
        self.assertEqual(gantt.f(1.50), '1.5')
        self.assertEqual(gantt.f(-0.001), '0')

    def test_off_and_lines(self):
        d = small()
        d['off'] = [{'from': '2026-11-23', 'to': '2026-11-27', 'label': 'Break'}]
        d['lines'] = [{'on': '2026-11-20', 'label': 'Freeze'}, {'on': '2027-01-01', 'label': 'Out'}]
        svg = gantt.render(d)
        ET.fromstring(svg)
        for s in ('class="off"', 'class="ln"', '>Break<', '>Freeze<'):
            self.assertIn(s, svg)
        self.assertNotIn('Out', svg.split('</style>')[1])

    def test_a_broken_item_does_not_break_the_render(self):
        d = small()
        d['rows'][0]['items'].append({'from': 'x', 'to': 'y'})
        d['rows'][0]['items'].append({'on': 5})
        ET.fromstring(gantt.render(d))
        ET.fromstring(gantt.render({'rows': []}))
        ET.fromstring(gantt.render({'rows': [{'id': 'a', 'title': 'x', 'items': [{'on': '2026-11-03'}]}]}))


class ScaleTest(unittest.TestCase):
    # sha256 of render(sample(), today='2026-11-10', **kw) for the outputs of svg / png / pdf / embed / share, taken with
    # the renderer before the window's sizes, search and people existed
    GOLDEN = [({}, 'efe1eb4265c88eb1050e5768c6015e77fdb2df137cd6ba38676aab9fa39cb7e4'),
              ({'legend': True}, 'f672d41a1e7d500a22d24b36c3c059a25e2560a5d50d0f6ad6507381e345caa3'),
              ({'legend': True, 'zoom': True}, 'e4bb35e0bb66b6d006df44fa2ed0a850269fdeb41dcc30454381f851b3561eb4'),
              ({'day': 8}, '53367ec513de4c8813ded2ebc09241b9358824ec2c50551a84d2ac379aeba421'),
              ({'day': 32, 'legend': True}, '37a0b09a2e1cb52fa5063226d0dd50f0e6a2aa86208f84d3136b22fcde07ed37'),
              ({'day': 6}, 'b16579f6e6f88a184668aae9de4da32c68b9d265b6edb6c88e697b98ac396479'),
              ({'day': 40, 'legend': True, 'zoom': True}, 'cb3e1204a847dcc4b98ad37fae9fa85683419d22e22e7b63917f498cb45226e5')]

    def test_defaults_are_byte_identical_to_before(self):
        for kw, digest in self.GOLDEN:
            for extra in ({}, {'text_scale': 1.0, 'row_scale': 1.0},
                          {'text_scale': 1, 'row_scale': 1, 'label_width': None, 'query': '', 'owner': None, 'by_owner': False}):
                svg = gantt.render(sample(), today='2026-11-10', **dict(kw, **extra))
                self.assertEqual(hashlib.sha256(svg.encode('utf-8')).hexdigest(), digest, (kw, extra))
        svg, _, _ = gantt._draw(sample(), 14, False, True, '2026-11-10')          # what png / pdf / share draw
        self.assertEqual(hashlib.sha256(svg.encode('utf-8')).hexdigest(), self.GOLDEN[1][1])

    def rows_of(self, root):
        return [g for g in root.iter(SVG + 'g') if g.get('class') == 'row']

    def test_text_scale(self):
        one = ET.fromstring(gantt.render(small(), interactive=True))
        big = ET.fromstring(gantt.render(small(), interactive=True, text_scale=1.5))
        css = big.find(SVG + 'style').text
        self.assertIn('font-size:17.25px', css)                  # 11.5 x 1.5
        self.assertIn('.gantt .sl,.gantt .hw{font-size:15px}', css)
        self.assertEqual(int(big.get('data-x0')), int(one.get('data-x0')) * 3 // 2)     # title and owner columns
        self.assertEqual((big.get('data-tc'), big.get('data-hh')), ('360', '54'))       # the date header too
        self.assertEqual(big.get('data-dw'), '14')                # the day width is separate
        self.assertEqual([g.get('data-h') for g in self.rows_of(big)], ['36', '33', '36'])    # rows as high as the text
        self.assertEqual(float(big.get('height')), 54 + 36 + 33 + 36 + 1)
        bar = [r for r in big.iter(SVG + 'rect') if r.get('class') == 'b k-plan'][0]
        self.assertEqual((bar.get('y'), bar.get('height')), ('60', '24'))        # row y 54 + 4 x 1.5, 16 x 1.5
        self.assertNotRegex(gantt.render(sample(), text_scale=1.37, row_scale=2.13, day=13.37), r'\d\.\d{3}')

    def test_row_scale(self):
        tall = ET.fromstring(gantt.render(small(), interactive=True, row_scale=2))
        self.assertEqual(tall.find(SVG + 'style').text, ET.fromstring(gantt.render(small(), interactive=True)).find(SVG + 'style').text)
        self.assertEqual((tall.get('data-x0'), tall.get('data-hh')), ('320', '36'))        # columns and header as before
        self.assertEqual([g.get('data-h') for g in self.rows_of(tall)], ['48', '44', '48'])
        bar = [r for r in tall.iter(SVG + 'rect') if r.get('class') == 'b k-plan'][0]
        self.assertEqual((bar.get('y'), bar.get('height')), ('44', '32'))        # the bar grows with its row
        text = [t for t in tall.iter(SVG + 'text') if t.text == 'Job'][0]
        self.assertEqual(text.get('y'), '64')                     # on the middle of the row: 36 + 24 + 4
        ms = [p for p in tall.iter(SVG + 'path') if (p.get('class') or '').startswith('m ')][0]
        self.assertIn(' 60L', ms.get('d'))                        # the milestone keeps its size, on the middle
        low = ET.fromstring(gantt.render(small(), interactive=True, row_scale=0.7))
        self.assertEqual([g.get('data-h') for g in self.rows_of(low)], ['24', '22', '24'])    # never lower than the text
        both = ET.fromstring(gantt.render(small(), interactive=True, text_scale=1.5, row_scale=1.2))
        self.assertEqual([g.get('data-h') for g in self.rows_of(both)], ['36', '33', '36'])
        d = small()
        d['lines'] = [{'on': '2026-11-20', 'label': 'Freeze'}]
        strip = ET.fromstring(gantt.render(d, interactive=True, row_scale=2))
        self.assertEqual(strip.get('data-hh'), '60')              # header 36 + strip 12 x 2

    def test_label_width_override(self):
        d = small()
        root = ET.fromstring(gantt.render(d, interactive=True, label_width=300))
        self.assertEqual((root.get('data-x0'), root.get('data-tc')), ('380', '300'))
        d['label_width'] = 200                                     # the window's width wins over the data's
        self.assertEqual(ET.fromstring(gantt.render(d, interactive=True, label_width=300)).get('data-tc'), '300')
        self.assertEqual(ET.fromstring(gantt.render(d, interactive=True)).get('data-tc'), '200')
        self.assertEqual(ET.fromstring(gantt.render(d, interactive=True, label_width=300, text_scale=1.5)).get('data-tc'), '450')
        self.assertEqual(ET.fromstring(gantt.render(d, interactive=True, label_width=50)).get('data-tc'), '120')
        self.assertEqual(gantt.render(small(), label_width=None), gantt.render(small()))

    def test_scales_are_clamped(self):
        self.assertEqual((gantt.clamp_text(0.1), gantt.clamp_text(7), gantt.clamp_text(1.234)), (0.8, 2, 1.23))
        self.assertEqual((gantt.clamp_rows(0.1), gantt.clamp_rows(7), gantt.clamp_rows(1.234)), (0.7, 3, 1.23))
        self.assertEqual((gantt.clamp_label(1), gantt.clamp_label(9999), gantt.clamp_label(250.4)), (120, 1200, 250))
        self.assertEqual(gantt.render(small(), text_scale=9), gantt.render(small(), text_scale=2))

    def test_the_window_draws_no_title_tooltips(self):
        d = small()
        d['rows'][2]['items'] = [{'from': '2026-11-02', 'to': '2026-11-04', 'label': 'A rather long label', 'note': 'n'},
                                 {'from': '2026-11-05', 'to': '2026-11-06', 'label': 'Next'}]
        self.assertIn('<title>', gantt.render(d))
        self.assertNotIn('<title>', gantt.render(d, interactive=True))     # app.html shows its own details


class ViewTest(unittest.TestCase):
    def rows(self, svg):
        """[(data-r or 'p:<person>', data-g)] of the row groups of an interactive SVG."""
        return [(g.get('data-r') or 'p:' + g.get('data-p'), g.get('data-g')) for g in ET.fromstring(svg).iter(SVG + 'g')
                if g.get('class') == 'row']

    def test_people(self):
        self.assertEqual(gantt.people('田中さん・佐藤（要相談）'), ['田中', '佐藤'])
        self.assertEqual(gantt.people('佐藤（A-13 候補）'), ['佐藤'])
        self.assertEqual(gantt.people('A, B/C、D Ｅ　F (x) G((y)z)さん'), ['A', 'B', 'C', 'D', 'Ｅ', 'F', 'G'])
        self.assertEqual(gantt.people('佐藤・佐藤さん'), ['佐藤'])
        self.assertEqual(gantt.people('（未定）'), [])
        self.assertEqual(gantt.people('A（B・C'), ['A（B', 'C'])           # an unclosed bracket is part of a name
        self.assertEqual(gantt.people(None), [])
        for s in ('田中さん・佐藤（要相談）', 'A, B/C、D Ｅ　F (x) G((y)z)さん', 'A（B・C', ')x(', ''):
            self.assertEqual(''.join(t for _, t in gantt.owner_parts(s)), s)      # the pieces give the string back
        d = small()
        d['rows'] += [{'id': 'c', 'title': 'C', 'owner': 'Y・X（lead）'}, {'id': 'e', 'title': 'E', 'owner': 'Yさん'}]
        self.assertEqual(gantt.owners(d), [('X', 2), ('Y', 2), ('未定', 1)])      # most rows first, 未定 last
        self.assertEqual(gantt.members(d), {'X': ['a', 'c'], '未定': ['b'], 'Y': ['c', 'e']})

    def test_query(self):
        d = sample()
        q = gantt.render(d, interactive=True, query='layout')
        self.assertEqual(self.rows(q), [('g2', '1'), ('board', None)])          # the group above the row stays
        marks = [r for r in ET.fromstring(q).iter(SVG + 'rect') if r.get('class') == 'qh']
        self.assertEqual(len(marks), 1)
        self.assertIn('.gantt .qh{', q)
        its = [g for g in ET.fromstring(q).iter(SVG + 'g') if (g.get('class') or '').startswith('it ')]
        self.assertEqual([(g.get('data-r'), g.get('data-i')) for g in its], [('board', '0'), ('board', '1'), ('board', '2')])
        self.assertEqual(float(ET.fromstring(q).get('height')), 36 + 12 + 22 + 24 + 1)   # drawn without gaps
        self.assertEqual(self.rows(gantt.render(d, interactive=True, query='MATERIAL')), [('g2', '1'), ('encl', None)])  # item note
        self.assertEqual(self.rows(gantt.render(d, interactive=True, query='free text')), [('g2', '1'), ('encl', None)])  # row note
        self.assertEqual([r for r, _ in self.rows(gantt.render(d, interactive=True, query='ｃ'))][:3], ['g1', 'dates', 'g2'])
        self.assertEqual(self.rows(gantt.render(d, interactive=True, query='Software')),          # a group's name: all its rows
                         [('g3', '1'), ('fw', None), ('app', None)])
        self.assertEqual(self.rows(gantt.render(d, interactive=True, query='nothing like this')), [])
        self.assertEqual(gantt.render(d, query='  '), gantt.render(d))
        self.assertNotIn('qh', gantt.render(d, query='Bench'))  # found by the row title: no item is marked

    def test_owner_filter(self):
        d = sample()
        self.assertEqual(self.rows(gantt.render(d, interactive=True, owner='B')),
                         [('g2', '1'), ('board', None), ('g4', '1'), ('test', None)])
        self.assertEqual(self.rows(gantt.render(d, interactive=True, owner='未定')), [('g4', '1'), ('avail', None)])
        self.assertEqual(self.rows(gantt.render(d, interactive=True, owner='B', query='bench')), [('g4', '1'), ('test', None)])
        self.assertEqual(self.rows(gantt.render(d, interactive=True, owner='Software', query='Software')), [])
        root = ET.fromstring(gantt.render(d, interactive=True, owner='C'))
        self.assertEqual(int(root.get('data-x0')), 320)          # the owner column stays

    def test_by_person_view_keeps_the_real_ids(self):
        d = sample()
        d['rows'][3]['owner'] = 'A・Bさん'                          # Enclosure design: two people
        before = copy.deepcopy(d)
        svg = gantt.render(d, interactive=True, by_owner=True)
        self.assertEqual(d, before)                               # a view: the data is not reordered
        self.assertEqual(self.rows(svg), [
            ('p:B', '1'), ('encl', None), ('board', None), ('test', None),
            ('p:C', '1'), ('fw', None), ('app', None),
            ('p:PM', '1'), ('dates', None),
            ('p:A', '1'), ('encl', None),
            ('p:未定', '1'), ('avail', None)])
        self.assertIn('>B（3 件）<', svg)
        self.assertNotIn('class="ttl" data-r=""', svg)          # a person header is not a row of the data
        by_id = {r['id']: r for r in d['rows']}
        for g in ET.fromstring(svg).iter(SVG + 'g'):
            if (g.get('class') or '').startswith('it '):
                it = by_id[g.get('data-r')]['items'][int(g.get('data-i'))]
                self.assertEqual((g.get('data-from'), g.get('data-on')), (it.get('from'), it.get('on')))
        self.assertEqual(self.rows(gantt.render(d, interactive=True, by_owner=True, owner='A')), [('p:A', '1'), ('encl', None)])
        self.assertEqual(self.rows(gantt.render(d, interactive=True, by_owner=True, query='bench')),
                         [('p:B', '1'), ('test', None)])
        ET.fromstring(gantt.render(d, by_owner=True, legend=True))          # static too


class RenameTest(unittest.TestCase):
    def test_rename_in_an_owner_string(self):
        r = gantt.rename_owner
        self.assertEqual(r('アルバイト（未定）', 'アルバイト', '田中'), '田中（未定）')
        self.assertEqual(r('田中・アルバイト', 'アルバイト', '佐藤'), '田中・佐藤')
        self.assertEqual(r('アルバイトさん、 佐藤', 'アルバイト', '田中'), '田中さん、 佐藤')        # さん and the spaces stay
        self.assertEqual(r('佐藤（アルバイトと分担）', 'アルバイト', '田中'), '佐藤（アルバイトと分担）')   # brackets do not count
        self.assertEqual(r('アルバイトA・アルバイト', 'アルバイト', 'X'), 'アルバイトA・X')               # whole names only
        self.assertEqual(r('アルバイト', 'アルバイト', 'A・B'), 'A・B')                                 # one person into two

    def test_rename_op_is_one_change(self):
        d = small()
        d['rows'] += [{'id': 'c', 'title': 'C', 'owner': 'アルバイト（未定）'},
                      {'id': 'e', 'title': 'E', 'owner': '田中・アルバイト'}, {'id': 'f', 'title': 'F', 'owner': '佐藤'}]
        t = gantt.apply(d, [{'op': 'rename_person', 'from': 'アルバイト', 'to': '鈴木'}])
        self.assertEqual(t, ['担当の名前「アルバイト」→「鈴木」: 「C」、「E」（2 行）'])
        self.assertEqual([r.get('owner') for r in d['rows']], ['X', None, None, '鈴木（未定）', '田中・鈴木', '佐藤'])
        self.assertEqual(len(d['log']), 1)
        self.assertEqual(gantt.apply(d, [{'op': 'rename_person', 'from': 'nobody', 'to': 'x'}]), [])
        with self.assertRaises(ValueError):
            gantt.apply(d, [{'op': 'rename_person', 'from': '鈴木', 'to': '  '}])

    def test_one_undo_puts_every_row_back(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, 'a.gantt.json')
            d = small()
            d['rows'] += [{'id': 'c', 'title': 'C', 'owner': 'X・Y'}]
            gantt.save(p, d)
            api = gantt.Api(p)
            r = api.apply([{'op': 'rename_person', 'from': 'X', 'to': 'Z'}])
            self.assertEqual(r['members'], {'Z': ['a', 'c'], '未定': ['b'], 'Y': ['c']})
            self.assertTrue(api.undo()['ok'])
            self.assertEqual([r.get('owner') for r in gantt.load(p)['rows']], ['X', None, None, 'X・Y'])


class FitTest(unittest.TestCase):
    def test_non_integer_day_width(self):
        self.assertEqual(gantt.clamp_day(13.374), 13.37)
        self.assertEqual(gantt.clamp_day(14.0), 14)
        self.assertIsInstance(gantt.clamp_day(14.0), int)
        self.assertEqual((gantt.clamp_day(2.5), gantt.clamp_day(99)), (6, 40))
        root = ET.fromstring(gantt.render(small(), day=13.37, interactive=True))
        self.assertEqual(root.get('data-dw'), '13.37')
        bar = [r for r in root.iter(SVG + 'rect') if r.get('class') == 'b k-plan'][0]
        self.assertEqual(bar.get('x'), gantt.f(320 + 7 * 13.37))
        self.assertEqual(bar.get('width'), gantt.f(5 * 13.37))
        self.assertEqual(gantt.day_of(argparse.Namespace(day=None), {'day': 12.5}), 12.5)

    def test_fit_fills_the_window(self):
        d = sample()
        d['rows'] = d['rows'] * 3                                 # header 36, then strip and rows 780 px at scale 1
        for w, h, text in ((1264, 900, 1), (1900, 1000, 1), (1100, 1400, 1), (1264, 1300, 1.5)):
            day, rows = gantt.fit_view(d, w, h, text_scale=text)
            self.assertTrue(gantt.ROWS_MIN < rows < gantt.ROWS_MAX)
            svg = ET.fromstring(gantt.render(d, day=day, row_scale=rows, text_scale=text))
            sw, sh = float(svg.get('width')), float(svg.get('height'))
            self.assertLessEqual(sw, w)
            self.assertGreater(sw, w - 70 * 0.01 - 1)            # 70 days, the day width floored to 0.01
            self.assertLessEqual(sh, h)
            self.assertGreater(sh, h - 780 * 0.01 - 2)          # the row scale floored to 0.01
        self.assertEqual(gantt.fit_view(sample(), 1200, 3000)[1], 3)        # few rows: as high as they go
        self.assertLess(gantt.fit_view(d, 1264, 700, text_scale=1.5)[1], 1.5)   # lower than the text wants: it scrolls
        lw = gantt.fit_view(d, 1264, 900, label_width=500)[0]
        self.assertEqual(lw, gantt.clamp_day(math.floor((1264 - 0.5 - 580) / 70 * 100) / 100))
        d['rows'] = d['rows'] * 10
        self.assertEqual(gantt.fit_view(d, 1200, 600)[1], 0.7)      # many rows: the lowest, then scroll
        self.assertEqual(gantt.fit_view(d, 300, 600)[0], 6)          # narrow: the smallest day, then scroll


class RecentTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.home, gantt.HOME = gantt.HOME, os.path.join(self.dir, 'home')     # never the real ~/.gantt
        self.files = []
        for i in range(10):
            p = os.path.join(self.dir, 'p%d.gantt.json' % i)
            gantt.save(p, dict(small(), title='Plan %d' % i))
            self.files.append(p)

    def tearDown(self):
        gantt.HOME = self.home
        shutil.rmtree(self.dir)

    def test_recent_list(self):
        self.assertEqual((gantt.recent_files(), gantt.last_file()), ([], None))
        for p in self.files:
            gantt.push_recent(p)
        gantt.push_recent(self.files[3].upper() if os.name == 'nt' else self.files[3])     # again: moves to the top
        r = gantt.recent_files()
        self.assertEqual(len(r), gantt.RECENT_KEEP)
        self.assertTrue(gantt.same_file(r[0], self.files[3]))
        self.assertEqual([os.path.basename(p).lower() for p in r[1:]], ['p9.gantt.json', 'p8.gantt.json', 'p7.gantt.json',
                                                                        'p6.gantt.json', 'p5.gantt.json', 'p4.gantt.json',
                                                                        'p2.gantt.json'])
        os.remove(self.files[9])                                  # gone files drop out
        self.assertNotIn(self.files[9], gantt.recent_files())
        self.assertTrue(gantt.same_file(gantt.last_file(), self.files[3]))
        last = gantt.read_last()
        self.assertTrue(gantt.same_file(last['file'], self.files[3]))
        gantt.write_last(scale=1.5)                                # other keys are kept
        self.assertEqual(len(gantt.read_last()['recent']), gantt.RECENT_KEEP)

    def test_open_another_file_in_the_window(self):
        api = gantt.Api(self.files[0])
        api._opened = gantt.push_recent
        api.apply([{'op': 'row', 'row': 'b', 'set': {'owner': 'Q'}}])
        r = api.open_file(self.files[1], None, {'text': 1.1})
        self.assertTrue(r['ok'])
        self.assertEqual((r['title'], r['file'], r['text']), ('Agent Gantt — Plan 1', 'p1.gantt.json', 1.1))
        self.assertFalse(api.undo()['ok'])                        # undo belongs to the other file
        self.assertEqual(gantt.load(self.files[0])['rows'][2]['owner'], 'Q')
        listed = api.recent()
        self.assertEqual([(x['title'], x['current']) for x in listed], [('Plan 1', True)])
        self.assertFalse(api.open_file(os.path.join(self.dir, 'missing.gantt.json'))['ok'])
        bad = os.path.join(self.dir, 'bad.gantt.json')
        with open(bad, 'w') as fh:
            fh.write('[1]')
        self.assertFalse(api.open_file(bad)['ok'])
        self.assertEqual(api.get()['file'], 'p1.gantt.json')       # still the last good one

    def test_window_title(self):
        self.assertEqual(gantt.window_title({'title': 'X'}, 'a.gantt.json'), 'Agent Gantt — X')
        self.assertEqual(gantt.window_title({}, '/d/plan.gantt.json'), 'Agent Gantt — plan')
        self.assertEqual(gantt.window_title(None, None), 'Agent Gantt')


class CheckTest(unittest.TestCase):
    def test_sample_is_clean(self):
        self.assertEqual(gantt.check(sample())[0], [])

    def test_problems(self):
        d = small()
        d['rows'][0]['items'][0]['kind'] = 'nope'
        d['rows'][0]['items'][1]['on'] = '2026-13-45'
        d['rows'][1]['items'] = [{'on': '2026-11-03'}]
        d['rows'][2]['id'] = 'a'
        d['rows'].append({'id': 'c', 'items': [{'from': '2026-11-10', 'to': '2026-11-09'}]})
        bad, _ = gantt.check(d)
        text = '\n'.join(bad)
        for s in ('unknown kind', 'not YYYY-MM-DD', 'group row has items', 'duplicate id', 'from is after to'):
            self.assertIn(s, text)
        self.assertTrue(gantt.check({'rows': []})[0])


class DayTest(unittest.TestCase):
    def test_day_of(self):
        ns = argparse.Namespace
        self.assertEqual(gantt.day_of(ns(day=None), {}), 14)
        self.assertEqual(gantt.day_of(ns(day=None), {'day': 18}), 18)
        self.assertEqual(gantt.day_of(ns(day=10), {'day': 18}), 10)
        self.assertEqual(gantt.day_of(ns(day=None), {'day': 99}), 40)
        self.assertEqual(gantt.day_of(ns(day=None), {'day': True}), 14)


class FileCommandsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'plan.gantt.json')
        shutil.copyfile(SAMPLE, self.path)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_cmd(self, fn, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(argparse.Namespace(**kw))
        return out.getvalue()

    def test_svg_default_name_and_content(self):
        self.run_cmd(gantt.cmd_svg, file=self.path, output=None, day=14, no_legend=False)
        out = os.path.join(self.dir, 'plan.svg')
        with open(out, encoding='utf-8') as f:
            text = f.read()
        self.assertTrue(text.startswith('<?xml'))
        ET.fromstring(text.encode('utf-8'))
        self.assertIn('>Work<', text)
        self.run_cmd(gantt.cmd_svg, file=self.path, output=out, day=14, no_legend=True)
        with open(out, encoding='utf-8') as f:
            self.assertNotIn('>Work<', f.read())

    def test_embed_replaces_only_between_markers(self):
        page = os.path.join(self.dir, 'page.html')
        head, tail = 'ä\r\n<p>before</p>\n', '\n<p>after</p>\r\n'
        with open(page, 'wb') as f:
            f.write((head + gantt.BEGIN + '<svg>old</svg>' + gantt.END + tail).encode('utf-8'))
        self.run_cmd(gantt.cmd_embed, file=self.path, page=page, day=14)
        with open(page, 'rb') as f:
            raw = f.read().decode('utf-8')
        self.assertTrue(raw.startswith(head + gantt.BEGIN + '\n<svg class="gantt zoom"'))
        self.assertTrue(raw.endswith('</svg>\n' + gantt.END + tail))
        self.assertNotIn('old', raw)
        self.assertIn('>Work<', raw)                              # legend on
        self.run_cmd(gantt.cmd_embed, file=self.path, page=page, day=14)        # again: the same result
        with open(page, 'rb') as f:
            self.assertEqual(f.read().decode('utf-8'), raw)

    def test_embed_without_markers_is_an_error_and_leaves_the_page(self):
        page = os.path.join(self.dir, 'page.html')
        with open(page, 'wb') as f:
            f.write(b'<p>x</p>' + gantt.BEGIN.encode())
        with self.assertRaises(SystemExit) as cm:
            self.run_cmd(gantt.cmd_embed, file=self.path, page=page, day=14)
        self.assertIn('markers', str(cm.exception))
        with open(page, 'rb') as f:
            self.assertEqual(f.read(), b'<p>x</p>' + gantt.BEGIN.encode())

    def test_changes_prints_new_user_entries_and_moves_seen(self):
        out = self.run_cmd(gantt.cmd_changes, file=self.path, peek=True, all=False)
        self.assertEqual(out.strip(), '2026-11-04 10:12:40  「Main board」の担当: （なし） → B')
        self.assertEqual(gantt.load(self.path)['seen'], '2026-11-04 10:05:12')                  # --peek: unchanged
        self.run_cmd(gantt.cmd_changes, file=self.path, peek=False, all=False)
        self.assertEqual(gantt.load(self.path)['seen'], '2026-11-04 10:12:40')
        self.assertEqual(self.run_cmd(gantt.cmd_changes, file=self.path, peek=False, all=False).strip(), '(no changes)')
        out = self.run_cmd(gantt.cmd_changes, file=self.path, peek=True, all=True)
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_changes_ignores_entries_by_others(self):
        d = gantt.load(self.path)
        d['log'].append({'time': '2026-11-04 11:00:00', 'by': 'agent', 'text': 'mine'})
        gantt.save(self.path, d)
        self.assertNotIn('mine', self.run_cmd(gantt.cmd_changes, file=self.path, peek=True, all=True))

    def test_check_command(self):
        self.assertIn('ok:', self.run_cmd(gantt.cmd_check, file=self.path))
        d = gantt.load(self.path)
        d['rows'][1]['items'][0]['kind'] = 'zzz'
        gantt.save(self.path, d)
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
            gantt.cmd_check(argparse.Namespace(file=self.path))
        self.assertEqual(cm.exception.code, 1)


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'plan.gantt.json')
        shutil.copyfile(SAMPLE, self.path)
        self.api = gantt.Api(self.path)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def raw(self):
        with open(self.path, 'rb') as f:
            return f.read()

    def move(self, to='2026-11-30'):
        return self.api.apply([{'op': 'item', 'row': 'encl', 'i': 1, 'expect': {'from': '2026-11-16', 'to': '2026-11-27'},
                                'set': {'to': to}}])

    def test_get_apply_poll_undo(self):
        g = self.api.get()
        self.assertTrue(g['svg'].startswith('<svg') and 'data-x0' in g['svg'])
        self.assertNotIn('log', g['data'])
        self.assertEqual((g['file'], g['day'], g['share']), ('plan.gantt.json', 14, False))
        self.assertIsNone(self.api.poll(g['mtime']))
        r = self.move()
        self.assertTrue(r['ok'])
        self.assertNotEqual(r['mtime'], g['mtime'])
        self.assertEqual(gantt.load(self.path)['rows'][3]['items'][1]['to'], '2026-11-30')
        self.assertIn('11/16〜11/27 → 11/16〜11/30', gantt.load(self.path)['log'][-1]['text'])
        self.assertIsNone(self.api.poll(r['mtime']))
        self.assertIsNotNone(self.api.poll(g['mtime']))
        u = self.api.undo()
        self.assertTrue(u['ok'])
        self.assertEqual(gantt.load(self.path)['rows'][3]['items'][1]['to'], '2026-11-27')
        self.assertFalse(self.api.undo()['ok'])

    def test_stale_expect_is_a_conflict_and_the_file_is_not_written(self):
        self.move()
        text = self.raw()
        r = self.move('2026-12-02')                              # expect no longer matches
        self.assertEqual((r['ok'], r['conflict']), (False, True))
        self.assertEqual(self.raw(), text)
        r = self.api.apply([{'op': 'row', 'row': 'nope', 'set': {}}])
        self.assertEqual((r['ok'], r['conflict']), (False, False))

    def test_an_outside_edit_is_not_lost_and_blocks_undo(self):
        self.move()
        d = gantt.load(self.path)                                # the agent edits the file
        d['rows'][0]['group'] = 'Renamed'
        gantt.save(self.path, d)
        r = self.api.apply([{'op': 'row', 'row': 'avail', 'set': {'owner': 'Z'}}])      # read - apply - write
        self.assertTrue(r['ok'])
        d = gantt.load(self.path)
        self.assertEqual((d['rows'][0]['group'], d['rows'][-1]['owner']), ('Renamed', 'Z'))
        d['rows'][1]['owner'] = 'Late'                           # an edit after the last op: nothing can be undone
        gantt.save(self.path, d)
        u = self.api.undo()
        self.assertFalse(u['ok'])
        self.assertIn('戻せません', u['error'])
        d = gantt.load(self.path)
        self.assertEqual((d['rows'][0]['group'], d['rows'][1]['owner'], d['rows'][-1]['owner']), ('Renamed', 'Late', 'Z'))

    def test_undo_survives_the_agent_reading_changes(self):
        self.move()
        d = gantt.load(self.path)
        d['seen'] = d['log'][-1]['time']                         # what `changes` does
        gantt.save(self.path, d)
        self.assertTrue(self.api.undo()['ok'])
        d = gantt.load(self.path)
        self.assertEqual(d['rows'][3]['items'][1]['to'], '2026-11-27')
        self.assertTrue(d['log'][-1]['text'].startswith('元に戻す: '))      # the agent had read it: tell it
        self.assertGreater(d['log'][-1]['time'], d['seen'])

    def test_a_broken_file_is_reported_by_poll(self):
        g = self.api.get()
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('{ not json')
        self.assertIn('error', self.api.poll(g['mtime']))

    def test_open_link_refuses_other_schemes(self):
        self.assertFalse(self.api.open_link('javascript:alert(1)'))
        self.assertFalse(self.api.open_link('ftp://example.com/'))

    def test_public_names_are_the_api(self):
        names = [n for n in dir(self.api) if not n.startswith('_')]
        self.assertEqual(sorted(names), sorted(gantt.Handler.METHODS + gantt.Handler.APP_ONLY))
        self.assertFalse(set(gantt.Handler.APP_ONLY) & set(gantt.Handler.METHODS))     # no file switching over HTTP

    def test_view_argument(self):
        g = self.api.get(None, {'query': 'LAYOUT', 'text': 1.5, 'rows': 2, 'label_width': 300})
        self.assertEqual((g['shown'], g['total'], g['text'], g['rows'], g['label_width']), (1, 7, 1.5, 2, 300))
        self.assertIn('data-tc="450"', g['svg'])
        self.assertIn('class="qh"', g['svg'])
        self.assertEqual([p for p, _ in g['owners']], ['B', 'C', 'PM', 'A', '未定'])
        g = self.api.get(None, {'owner': 'C', 'by_owner': True})
        self.assertEqual(g['shown'], 2)
        self.assertIn('data-p="C"', g['svg'])
        self.assertEqual(g['members']['C'], ['fw', 'app'])
        g = self.api.get(None, {'fit': True, 'w': 1200, 'h': 700, 'text': 1.2, 'rows': 0.9})
        self.assertEqual((g['day'], g['rows'], g['text']), gantt.fit_view(sample(), 1200, 700, False, 1.2) + (1.2,))
        self.assertEqual(g['fitted'], [1200, 700])
        self.assertIsNone(self.api.get()['fitted'])
        self.assertLessEqual(float(ET.fromstring(g['svg']).get('width')), 1200)
        self.assertEqual(self.api.get(13.5)['day'], 13.5)
        r = self.move()                                          # the view also comes back from apply
        self.assertEqual((r['text'], r['rows'], r['label_width']), (1.3, 1, None))      # the window's defaults

    def test_prefs(self):
        kept = []
        self.api._remember = lambda **kw: kept.append(kw)
        self.assertEqual(self.api.prefs(), {'day': 14, 'text': 1.3, 'rows': 1, 'fit': True, 'by_owner': False,
                                            'label_width': None})
        p = self.api.prefs({'text': 9, 'rows': 0.1, 'fit': False, 'day': 'x', 'by_owner': 1, 'label_width': 50, 'other': 2})
        self.assertEqual(p, {'day': 14, 'text': 2, 'rows': 0.7, 'fit': False, 'by_owner': False, 'label_width': 120})
        self.assertEqual(kept, [{'text': 2, 'rows': 0.7, 'fit': False, 'label_width': 120}])
        self.assertEqual(self.api.prefs({'label_width': None})['label_width'], None)     # back to the data's
        self.assertEqual((self.api.get()['text'], self.api.get()['rows']), (2, 0.7))
        self.assertEqual(gantt.Api(self.path, 10, {'scale': 1.1, 'fit': False}).prefs()['text'], 1.1)   # the first version's key

    def test_without_a_file(self):
        api = gantt.Api(None)
        self.assertEqual(api.get(), {'nofile': True})
        self.assertIsNone(api.poll('x'))
        self.assertFalse(api.apply([])['ok'])
        self.assertFalse(api.undo()['ok'])
        self.assertFalse(api.share()['ok'])
        self.assertFalse(api.pick()['ok'])                       # no window here
        r = api.open_file(self.path)
        self.assertTrue(r['ok'])
        self.assertEqual(r['title'], 'Agent Gantt — Sample product plan')
        self.assertTrue(api.get()['svg'].startswith('<svg'))


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.path = os.path.join(cls.dir, 'plan.gantt.json')
        shutil.copyfile(SAMPLE, cls.path)
        gantt.Handler.api = gantt.Api(cls.path)
        cls.srv = gantt.Server(('127.0.0.1', 0), gantt.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.dir)

    def req(self, path, body=None, headers=None, raw=None):
        """(status, body) of a request sent like the page does (our Origin); a None header value removes it."""
        h = {'Origin': 'http://127.0.0.1:%d' % self.port}
        if body is not None or raw is not None:
            h['Content-Type'] = 'application/json'
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request('http://127.0.0.1:%d%s' % (self.port, path), data, h)
        try:
            with self.opener.open(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_page_and_api(self):
        code, body = self.req('/', headers={'Origin': None})
        self.assertEqual(code, 200)
        self.assertIn(b'<svg', gantt.render(sample()).encode()[:10])
        self.assertIn('window.pywebview'.encode(), body)
        code, body = self.req('/api/get', [None])
        self.assertEqual(code, 200)
        g = json.loads(body)
        self.assertTrue(g['svg'].startswith('<svg'))
        code, body = self.req('/api/apply', [[{'op': 'row', 'row': 'dates', 'set': {'owner': 'Q'}}], None])
        self.assertTrue(json.loads(body)['ok'])
        code, body = self.req('/api/poll', [g['mtime'], None])
        self.assertEqual(json.loads(body)['data']['rows'][1]['owner'], 'Q')
        self.assertEqual(json.loads(self.req('/api/undo', [None])[1])['ok'], True)
        self.assertEqual(self.req('/api/nope', [])[0], 404)
        self.assertEqual(self.req('/api/get', {'a': 1})[0], 400)
        self.assertEqual(self.req('/api/get', raw=b'not json')[0], 400)
        self.assertEqual(self.req('/nothing', headers={'Origin': None})[0], 404)

    def test_other_sites_are_refused(self):
        post = ('/api/get', [None])
        self.assertEqual(self.req(*post, headers={'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.req(*post, headers={'Origin': 'null'})[0], 403)
        self.assertEqual(self.req(*post, headers={'Origin': None})[0], 403)                       # a write needs our Origin
        self.assertEqual(self.req(*post, headers={'Host': 'evil.example'})[0], 403)               # DNS rebinding
        self.assertEqual(self.req(*post, headers={'Origin': None, 'Referer': 'http://evil.example/x'})[0], 403)
        self.assertEqual(self.req(*post, headers={'Origin': None, 'Referer': 'http://127.0.0.1:%d/' % self.port})[0], 200)
        self.assertEqual(self.req(*post, headers={'Origin': None, 'Sec-Fetch-Site': 'same-origin'})[0], 200)
        self.assertEqual(self.req(*post, headers={'Origin': None, 'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.req('/', headers={'Origin': None, 'Sec-Fetch-Site': 'cross-site',
                                                'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': 'script'})[0], 403)
        self.assertEqual(self.req('/', headers={'Origin': None, 'Sec-Fetch-Site': 'cross-site',
                                                'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'})[0], 200)
        self.assertEqual(self.req('/', headers={'Origin': None, 'Host': 'evil.example'})[0], 403)

    def test_only_json_bodies(self):
        self.assertEqual(self.req('/api/get', raw=b'[null]', headers={'Content-Type': 'text/plain'})[0], 415)

    def test_headers(self):
        r = self.opener.open(urllib.request.Request('http://127.0.0.1:%d/' % self.port))
        self.assertEqual(r.headers['X-Frame-Options'], 'SAMEORIGIN')
        self.assertEqual(r.headers['X-Content-Type-Options'], 'nosniff')


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'plan.gantt.json')
        shutil.copyfile(SAMPLE, self.path)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_wrapper_page(self):
        data = sample()
        svg, w, h = gantt._draw(data, 14, False, True)
        text, pw, ph = gantt.page_html(data, svg, w, h, '2026-11-04 10:12')
        self.assertEqual((pw, ph), (int(w) + 24, int(h) + 30 + 24))
        self.assertIn('@page{size:%dpx %dpx;margin:0}' % (pw, ph), text)
        self.assertIn('<b>Sample product plan</b><span>更新 2026-11-04 10:12</span>', text)
        self.assertIn('background:#fff', text)
        self.assertIn(svg, text)
        self.assertNotIn('var(--ink)', text.split('<style>')[1].split('</style>')[0])         # light colours: the SVG's fallbacks
        self.assertIn('>Work<', text)                                                         # legend on
        self.assertNotIn('<script', text)

    def test_wrapper_page_escapes_the_title(self):
        data = small()
        data['title'] = '<i>x</i> & y'
        text, _, _ = gantt.page_html(data, '<svg/>', 100, 50, 'now')
        self.assertIn('&lt;i&gt;x&lt;/i&gt; &amp; y', text)

    def test_browser_lookup_honours_the_environment(self):
        old = os.environ.get('GANTT_BROWSER')
        try:
            os.environ['GANTT_BROWSER'] = self.path               # any existing file
            self.assertEqual(gantt.find_browser(), self.path)
        finally:
            if old is None:
                os.environ.pop('GANTT_BROWSER', None)
            else:
                os.environ['GANTT_BROWSER'] = old

    def test_a_failing_browser_is_a_clear_error_and_leaves_no_files(self):
        old = os.environ.get('GANTT_BROWSER')
        fake = os.path.join(self.dir, 'fake-browser.cmd' if os.name == 'nt' else 'fake-browser.sh')
        with open(fake, 'w') as f:
            f.write('@exit 3\n' if os.name == 'nt' else '#!/bin/sh\nexit 3\n')
        os.chmod(fake, 0o755)
        try:
            os.environ['GANTT_BROWSER'] = fake
            out = os.path.join(self.dir, 'x.pdf')
            with self.assertRaises(gantt.ExportError) as cm:
                gantt.export(sample(), out, 'pdf')
            self.assertIn('wrote no pdf', str(cm.exception))
            self.assertEqual(sorted(os.listdir(self.dir)), ['fake-browser.cmd' if os.name == 'nt' else 'fake-browser.sh',
                                                            'plan.gantt.json'])
        finally:
            if old is None:
                os.environ.pop('GANTT_BROWSER', None)
            else:
                os.environ['GANTT_BROWSER'] = old

    def test_share_svg_targets_without_a_browser(self):
        out = os.path.join(self.dir, 'out')
        os.mkdir(out)
        d = gantt.load(self.path)
        d['share'] = [os.path.join(out, 'chart.svg')]
        gantt.save(self.path, d)
        for _ in range(2):                                        # the same name every time
            written = gantt.write_share(self.path, d)
            self.assertEqual(written, [os.path.join(out, 'chart.svg')])
        ET.parse(written[0])
        self.assertEqual(os.listdir(out), ['chart.svg'])
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith('.gantt')], [])      # no temp file beside the data
        self.assertTrue(gantt.Api(self.path).share()['ok'])
        self.assertTrue(gantt.Api(self.path).get()['share'])

    def test_share_errors(self):
        d = gantt.load(self.path)
        d['share'] = [os.path.join(self.dir, 'missing-folder', 'x.pdf')]
        gantt.save(self.path, d)
        r = gantt.Api(self.path).share()
        self.assertFalse(r['ok'])
        self.assertIn('no folder', r['error'])
        with self.assertRaises(gantt.ExportError):
            gantt.write_share(self.path, d)
        d['share'] = [os.path.join(self.dir, 'x.docx')]
        with self.assertRaises(gantt.ExportError):
            gantt.write_share(self.path, d)
        d['share'] = 'x.pdf'
        with self.assertRaises(gantt.ExportError):
            gantt.write_share(self.path, d)

    @unittest.skipUnless(gantt.find_browser(), 'no Edge / Chrome')
    def test_png_and_pdf_with_a_real_browser(self):
        png, pdf = os.path.join(self.dir, 'a.png'), os.path.join(self.dir, 'a.pdf')
        gantt.export(sample(), png, 'png', scale=1)
        gantt.export(sample(), pdf, 'pdf')
        with open(png, 'rb') as f:
            head = f.read(24)
        self.assertEqual(head[:8], b'\x89PNG\r\n\x1a\n')
        svg, w, h = gantt._draw(sample(), 14, False, True)
        _, pw, ph = gantt.page_html(sample(), svg, w, h, 'x')
        self.assertEqual((int.from_bytes(head[16:20], 'big'), int.from_bytes(head[20:24], 'big')), (pw, ph))
        with open(pdf, 'rb') as f:
            raw = f.read()
        self.assertTrue(raw.startswith(b'%PDF'))
        self.assertEqual(raw.count(b'/Type /Page\n') + raw.count(b'/Type/Page/'), 1)          # one page
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith('.gantt')], [])

    @unittest.skipUnless(gantt.find_browser(), 'no Edge / Chrome')
    def test_share_command_writes_pdf_and_png(self):
        out = os.path.join(self.dir, 'drive')
        os.mkdir(out)
        d = gantt.load(self.path)
        d['share'] = [os.path.join(out, 'plan.pdf'), os.path.join(out, 'plan.png')]
        gantt.save(self.path, d)
        written = gantt.write_share(self.path, d)
        self.assertEqual(sorted(os.listdir(out)), ['plan.pdf', 'plan.png'])
        self.assertEqual(written, d['share'])
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith('.gantt')], [])


if __name__ == '__main__':
    unittest.main()
