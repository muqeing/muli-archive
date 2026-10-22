"""One file universe; presentation never creates archive or completion evidence."""
from copy import deepcopy
from pathlib import Path
import subprocess
import unittest

from muli_sorter.review_projection import archive_accounting, compact_archive


def unit(uid, *names):
    return {'unit_id': uid, 'files': [{'name': n, 'source_path': uid + '/' + n} for n in names]}


class AccountingTests(unittest.TestCase):
    def fixture(self):
        model = {'report_id': 'report', 'units': [
            unit('a', 'clip.MP4', 'clip.LRF'), unit('b', 'old.ARW'),
            unit('c', 'fresh.JPG', 'fresh.XMP'), unit('d', 'INDEX.BDM'),
            unit('e', 'SONY/SONYCARD.IND'), unit('f', 'discard.THM'), unit('g', 'unknown.BIN')]}
        archive = {'report_id': 'report', 'archived_units': [
            {'unit_id': 'a', 'in_current_model': True, 'file_count': 2},
            {'unit_id': 'previous-report-only', 'in_current_model': False, 'file_count': 99}],
            'retained_unit_ids': ['b'], 'retained_current_files': 1, 'history_total': 1000}
        materials = {'units': {uid: {'category': role} for uid, role in
                              zip('abcdef', ['shoot', 'shoot', 'companion', 'exception', 'support', 'discarded'])}}
        return model, archive, materials

    def test_exact_partition_companion_counts_and_history_scope_without_mutation(self):
        args = self.fixture(); before = deepcopy(args)
        a = archive_accounting(*args)
        self.assertEqual(a['total_units'], 7); self.assertEqual(a['total_files'], 9)
        self.assertEqual(a['buckets'], dict(archived={'units':1,'files':2}, retained={'units':1,'files':1},
            pending={'units':1,'files':2}, review={'units':2,'files':2}, system={'units':1,'files':1}, discarded={'units':1,'files':1}))
        self.assertEqual(a['archived_file_formats'], dict(primary=1, companion=1, system=0, other=0))
        self.assertEqual(sum(a['file_formats'].values()), 9)
        summary = compact_archive(args[1], accounting=a)
        self.assertEqual(summary['current_files'], 2); self.assertEqual(summary['history_total'], 1000)
        self.assertEqual(args, before)

    def test_unknown_or_absent_roles_never_become_system_or_archived(self):
        model, archive, materials = self.fixture()
        materials['units']['g'] = {'category': 'future-role'}
        a = archive_accounting(model, archive, materials)
        self.assertEqual(a['buckets']['review']['files'], 2)
        self.assertEqual(a['buckets']['archived']['files'], 2)

    def test_rejects_overlap_bad_receipt_counts_cross_report_and_duplicate_file(self):
        for mutation in ['overlap', 'receipt', 'retained', 'report', 'duplicate', 'foreign']:
            m, a, t = self.fixture()
            if mutation == 'overlap': a['retained_unit_ids'].append('a')
            if mutation == 'receipt': a['archived_units'][0]['file_count'] = 3
            if mutation == 'retained': a['retained_current_files'] = 0
            if mutation == 'report': a['report_id'] = 'other'
            if mutation == 'duplicate': m['units'][-1]['files'][0]['source_path'] = m['units'][0]['files'][0]['source_path']
            if mutation == 'foreign': a['retained_unit_ids'].append('not-in-report')
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): archive_accounting(m, a, t)

    def test_browser_rejects_incomplete_inconsistent_or_wrong_scope_ledger(self):
        import json
        import muli_sorter
        m, a, t = self.fixture(); ledger = archive_accounting(m, a, t)
        source = (Path(muli_sorter.__file__).parent / 'review_ui.js').read_text()
        function = source[source.index('  function accountingSafe('):source.index('  function archiveDataSafe(')]
        script = "const assert=require('assert/strict');const model={report_id:'report'};const units=new Array(7);\n" + function
        script += 'const ledger=' + json.dumps(ledger) + ";const data={archived_unit_ids:['a'],current_files:2,retained_unit_ids:['b'],retained_current_files:1};\n"
        script += """
        assert(accountingSafe(ledger,data));
        assert.equal(accountingSafe({...ledger,report_id:'other'},data),null);
        assert.equal(accountingSafe({...ledger,total_files:10},data),null);
        assert.equal(accountingSafe(ledger,{...data,current_files:3}),null);
        for(const value of [-1,1.2,Number.MAX_SAFE_INTEGER+1]) {
          const bad=structuredClone(ledger);bad.buckets.review.files=value;assert.equal(accountingSafe(bad,data),null);
        }
        const bad=structuredClone(ledger);delete bad.buckets.review;assert.equal(accountingSafe(bad,data),null);
        const wrong=structuredClone(ledger);wrong.archived_file_formats.primary=2;assert.equal(accountingSafe(wrong,data),null);
        assert.equal(accountingSafe(undefined,data),null);
        """
        subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
