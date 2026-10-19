"""All insertion paths obey the opened-page retention contract."""
from copy import deepcopy
import unittest

from muli_sorter.archive_jobs import MaterialReports


class MaterialReportBudgetTests(unittest.TestCase):
    @staticmethod
    def model(index):
        return {'report_id': f'sha256:{index:064x}',
                'units': [{'unit_id': str(index), 'files': [{'name': 'sample'}]}]}

    def test_many_background_generations_retain_only_latest_four(self):
        reports = MaterialReports()
        for index in range(1000):
            model = self.model(index)
            reports[model['report_id']] = model
            self.assertLessEqual(len(reports), 4)
        self.assertIsNone(reports.get(self.model(995)['report_id']))
        for index in range(996, 1000):
            self.assertEqual(reports.get(self.model(index)['report_id']), self.model(index))

    def test_reopened_identity_is_retained_without_duplicate_slot(self):
        reports = MaterialReports()
        for index in range(4):
            reports[self.model(index)['report_id']] = self.model(index)
        reports[self.model(0)['report_id']] = self.model(0)
        reports[self.model(4)['report_id']] = self.model(4)
        self.assertEqual(len(reports), 4)
        self.assertEqual(reports.get(self.model(0)['report_id']), self.model(0))
        self.assertIsNone(reports.get(self.model(1)['report_id']))

    def test_invalid_insert_does_not_evict_existing_snapshots(self):
        reports = MaterialReports()
        for index in range(4):
            reports[self.model(index)['report_id']] = self.model(index)
        before = list(reports)
        with self.assertRaises(ValueError):
            reports['wrong'] = self.model(5)
        bad = self.model(5)
        bad['units'].append(object())
        with self.assertRaises(TypeError):
            reports[bad['report_id']] = bad
        self.assertEqual(list(reports), before)

    def test_compressed_snapshots_are_detached_on_insert_and_read(self):
        reports = MaterialReports()
        original = self.model(1)
        expected = deepcopy(original)
        reports[original['report_id']] = original
        original['units'].clear()
        restored = reports.get(original['report_id'])
        self.assertEqual(restored, expected)
        restored['units'].clear()
        self.assertEqual(reports.get(original['report_id']), expected)

    def test_byte_budget_and_replacement_release_retained_payloads(self):
        reports = MaterialReports()
        reports.MAX_BYTES = 300
        for index in range(20):
            model = self.model(index)
            reports[model['report_id']] = model
            self.assertLessEqual(reports._bytes, reports.MAX_BYTES)
            self.assertEqual(reports._bytes, sum(map(len, reports._rows.values())))
        self.assertLess(len(reports),4)
        last=self.model(19);self.assertEqual(reports.get(last['report_id']),last)
        reports[last['report_id']]=last
        self.assertEqual(reports._bytes,sum(map(len,reports._rows.values())))
        reports.pop(last['report_id'])
        self.assertEqual(reports._bytes,sum(map(len,reports._rows.values())))

    def test_single_oversize_payload_is_not_kept_or_partially_restored(self):
        reports=MaterialReports();reports.MAX_BYTES=1
        model=self.model(1);reports[model['report_id']]=model
        self.assertEqual(len(reports),0);self.assertEqual(reports._bytes,0)
        self.assertIsNone(reports.get(model['report_id']))


if __name__ == '__main__':
    unittest.main()
