"""Tests of gantt.py: ops, render, dumps, embed, changes, the page API, the HTTP guards, share / png / pdf.
python test_gantt.py   (the png / pdf tests are skipped when no Edge / Chrome is found)"""
import argparse
import contextlib
import copy
import io
import json
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
        self.assertEqual(sorted(names), sorted(gantt.Handler.METHODS))


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
