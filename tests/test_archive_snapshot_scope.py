from copy import deepcopy
import unittest
from unittest.mock import patch

from test_archive_jobs import Fixture
from muli_sorter.archive_snapshot_scope import validate_selected_snapshot
from muli_sorter.queue_model_cache import QueueModelCache
from muli_sorter.order_feed_io import atomic_json
from muli_sorter.review import identity_digest


class SnapshotScopeTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.opened = deepcopy(self.model)
        self.service.material_reports[self.opened['report_id']] = self.opened
        self.selected = {uid for s in self.decisions['segments'] if s['decision'] == 'confirmed'
                         for uid in s['unit_ids']}
        self.uid = next(iter(self.selected))

    def current(self):
        self.model['report_id'] = identity_digest(self.model)

    def unit(self):
        return next(u for u in self.model['units'] if u['unit_id'] == self.uid)

    def unrelated_change(self):
        unit = next(u for u in self.model['units'] if u['unit_id'] not in self.selected)
        unit['warnings'].append('未选择素材的新提示')
        self.current()

    def test_unrelated_change_preserves_exact_plan_and_ticket_scope(self):
        original = deepcopy(self.decisions)
        self.unrelated_change()
        result = self.service.preflight(self.decisions)
        self.assertEqual(result['status'], 'ready', result)
        ticket = self.service.previews[result['preview_id']]
        self.assertEqual(ticket['decisions'], original)
        self.assertEqual(ticket['origin_report_id'], self.opened['report_id'])
        self.assertEqual(set(ticket['scope_binding']['selected_unit_ids']), self.selected)
        self.assertEqual(self.decisions, original)
        with patch.object(self.service, 'model', side_effect=AssertionError('submit reopened model')):
            job = self.service.submit(result['preview_id'], self.decisions, True)
        self.assertEqual(job['status'], 'queued')
        self.assertEqual(job['report_id'], self.opened['report_id'])

    def test_new_unit_is_not_added_to_old_plan(self):
        new = deepcopy(next(u for u in self.model['units'] if u['unit_id'] not in self.selected))
        new['unit_id'] = 'unit-new-unselected'
        for f in new['files']:
            f['source_path'] = 'unselected/' + f['source_path']
        self.model['units'].append(new)
        self.current()
        result = self.service.preflight(self.decisions)
        self.assertEqual(result['status'], 'ready', result)
        self.assertNotIn(new['unit_id'], {u['unit_id'] for u in self.service.previews[result['preview_id']]['model']['units']})

    def test_other_candidate_addition_does_not_block_chosen_project(self):
        project = deepcopy(self.model['projects'][0])
        project.update(project_id='other-project', path='2026/7月/20260709_09998_其他项目')
        self.model['projects'].append(project)
        self.unit()['candidate_project_ids'].append(project['project_id'])
        self.current()
        self.assertEqual(self.service.preflight(self.decisions)['status'], 'ready')

    def test_changed_selected_record_has_segment_and_unit_location(self):
        self.unit()['files'][0]['size_bytes'] += 1
        self.current()
        with patch.object(self.service, '_prepare', side_effect=AssertionError('checked media before scope')):
            result = self.service.preflight(self.decisions)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['issues'][0]['scope'], 'unit')
        self.assertEqual(result['issues'][0]['units'][0]['unit_id'], self.uid)
        self.assertTrue(result['issues'][0]['segments'])

    def test_missing_selected_unit_blocks(self):
        self.model['units'] = [u for u in self.model['units'] if u['unit_id'] != self.uid]
        self.current()
        result = self.service.preflight(self.decisions)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('已不在当前清单', result['errors'][0])

    def test_changed_project_is_located(self):
        pid = next(s['project_id'] for s in self.decisions['segments'] if self.uid in s['unit_ids'])
        next(p for p in self.model['projects'] if p['project_id'] == pid)['name'] += '改名'
        self.current()
        result = self.service.preflight(self.decisions)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['issues'][0]['project_id'], pid)
        self.assertTrue(result['issues'][0]['segments'])

    def test_removed_chosen_candidate_blocks(self):
        for u in self.model['units']:
            if u['unit_id'] in self.selected:
                u['candidate_project_ids'] = []
        self.current()
        result = self.service.preflight(self.decisions)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('当前候选', result['errors'][0])

    def test_unknown_opened_snapshot_still_blocks(self):
        self.service.material_reports.pop(self.opened['report_id'])
        self.unrelated_change()
        result = self.service.preflight(self.decisions)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['issues'][0]['current_report_id'], self.model['report_id'])

    def test_opened_cache_rejects_wrong_key(self):
        with self.assertRaisesRegex(ValueError, '缓存编号'):
            self.service.material_reports['sha256:'+'0'*64] = self.opened

    def test_live_source_checks_still_block_after_scope_passes(self):
        self.unrelated_change()
        path = self.root/'staging'/self.unit()['files'][0]['source_path']
        path.write_bytes(path.read_bytes() + b'CHANGED')
        result = self.service.preflight(self.decisions)
        self.assertEqual(result['status'], 'blocked', result)
        self.assertNotIn('计划来自其他快照', result['errors'][0])

    def test_dependency_and_relationship_changes_block(self):
        from muli_sorter.material_triage import companion_links
        # Use an explicit dependency edge to cover a non-selected parent.
        pending = next(u['unit_id'] for u in self.model['units'] if u['unit_id'] not in self.selected)
        with patch('muli_sorter.archive_snapshot_scope.companion_links', return_value=({self.uid: pending}, {})):
            self.model['units'][-1]['warnings'].append('dependency changed')
            self.current()
            with self.assertRaisesRegex(ValueError, '必要依赖'):
                validate_selected_snapshot(self.opened, self.model, self.decisions)
        with patch('muli_sorter.archive_snapshot_scope.companion_links', side_effect=[({self.uid: pending}, {}), ({}, {})]):
            with self.assertRaisesRegex(ValueError, '附属关系'):
                validate_selected_snapshot(self.opened, self.model, self.decisions)

    def test_manual_project_anchor_is_part_of_scope(self):
        self.manual()
        pending = next(u for u in self.model['units'] if u['unit_id'] not in self.selected)
        self.decisions['manual_projects'][0]['unit_ids'].append(pending['unit_id'])
        pending['capture_date'] = '2026-07-09'
        self.current()
        self.opened = deepcopy(self.model)
        pending['warnings'].append('anchor changed')
        self.current()
        with self.assertRaisesRegex(ValueError, '必要依赖'):
            validate_selected_snapshot(self.opened, self.model, self.decisions | {'report_id': self.opened['report_id']})

    def test_order_refetch_time_only_is_allowed_but_order_change_blocks(self):
        for p in self.opened['projects']:
            p['order_evidence'] = {'record_id': 'same-order', 'fetched_at': 'old', 'source': 'readonly'}
        self.opened['report_id'] = identity_digest(self.opened)
        decisions = self.decisions | {'report_id': self.opened['report_id']}
        self.model = deepcopy(self.opened)
        for p in self.model['projects']:
            p['order_evidence']['fetched_at'] = 'new'
        self.current()
        validate_selected_snapshot(self.opened, self.model, decisions)
        for p in self.model['projects']:
            p['order_evidence']['record_id'] = 'different-order'
        self.current()
        with self.assertRaisesRegex(ValueError, '项目资料'):
            validate_selected_snapshot(self.opened, self.model, decisions)

    def test_new_mp4_revokes_previously_approved_proxy(self):
        from triage_fixture import prepare_triage
        from muli_sorter.archive_jobs import ArchiveJobs
        root = self.root/'proxy-fixture'
        model, decisions = prepare_triage(root)
        proxy = next(u for u in model['units'] if u['kind'] == 'proxy_only')
        for segment in decisions['segments']:
            if proxy['unit_id'] in segment['unit_ids']:
                segment.update(decision='confirmed', project_id=proxy['candidate_project_ids'][0])
        decisions['archive_options'] = {'mode': 'copy', 'existing': 'skip_identical',
                                       'proxy_only_unit_ids': [proxy['unit_id']]}
        service = ArchiveJobs(root/'staging', root/'projects', root/'archive-state',
                              lambda: model, lambda: {}, enabled=True)
        try:
            service.material_reports[model['report_id']] = deepcopy(model)
            primary = deepcopy(proxy)
            primary.update(unit_id='unit-new-primary', kind='video')
            primary['files'][0]['name'] = primary['files'][0]['name'][:-4] + '.MP4'
            primary['files'][0]['source_path'] += '.MP4'
            model['units'].append(primary)
            model['report_id'] = identity_digest(model)
            with patch.object(service, '_prepare', side_effect=AssertionError('scope must block first')):
                result = service.preflight(decisions)
            self.assertEqual(result['status'], 'blocked', result)
            self.assertIn('已有 MP4', result['errors'][0])
            self.assertEqual(result['issues'][0]['units'][0]['unit_id'], proxy['unit_id'])
        finally:
            service.close()


class SnapshotResolverTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.queue = self.root/'queue'
        self.path = self.queue/'combined'/self.model['report_id']/'确认模型.json'
        self.path.parent.mkdir(parents=True)
        atomic_json(self.path, self.model)
        atomic_json(self.queue/'队列状态.json', {'base_model_path': self.path.relative_to(self.queue).as_posix()})
        self.provider = QueueModelCache(self.queue)

    def test_reads_only_requested_official_snapshot_after_restart(self):
        self.service.provider = self.provider
        old = deepcopy(self.model)
        pending = next(u for u in self.model['units'] if u['capture_date'] == '2026-07-11')
        pending['warnings'].append('unselected change')
        self.model['report_id'] = identity_digest(self.model)
        current_path = self.queue/'combined'/self.model['report_id']/'确认模型.json'
        current_path.parent.mkdir(parents=True)
        atomic_json(current_path, self.model)
        atomic_json(self.queue/'队列状态.json', {'base_model_path': current_path.relative_to(self.queue).as_posix()})
        result = self.service.preflight(self.decisions)
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual(self.provider.report_for_id(old['report_id']), old)
        self.assertIsNone(self.provider.report_for_id('sha256:'+'0'*64))

    def test_invalid_id_and_tampered_snapshot_rejected(self):
        with self.assertRaisesRegex(ValueError, '编号无效'):
            self.provider.report_for_id('../current')
        self.model['units'][0]['warnings'].append('tampered')
        atomic_json(self.path, self.model)
        with self.assertRaises(ValueError):
            self.provider.report_for_id(self.model['report_id'])

    def test_symlink_snapshot_and_ancestor_rejected(self):
        self.path.unlink()
        self.path.symlink_to(self.root/'model.json')
        with self.assertRaises((OSError, ValueError)):
            self.provider.report_for_id(self.model['report_id'])
        self.path.unlink()
        directory = self.path.parent
        directory.rmdir()
        directory.symlink_to(self.root)
        (self.root/'确认模型.json').write_text((self.root/'model.json').read_text())
        with self.assertRaises((OSError, ValueError)):
            self.provider.report_for_id(self.model['report_id'])

    def test_read_race_and_wrong_directory_id_rejected(self):
        with patch('muli_sorter.queue_model_cache.token', side_effect=[('before',), ('after',)]):
            with self.assertRaisesRegex(ValueError, '读取期间'):
                self.provider.report_for_id(self.model['report_id'])
        wrong = self.queue/'combined'/('sha256:'+'1'*64)/'确认模型.json'
        wrong.parent.mkdir()
        atomic_json(wrong, self.model)
        with self.assertRaisesRegex(ValueError, '编号不一致'):
            self.provider.report_for_id('sha256:'+'1'*64)
