import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from muli_sorter.order_catalog import ORDER_FIELDS, OrderError
from muli_sorter.order_preview import _validate_output_target, main, prepare_preview
from muli_sorter.review import build_review_model, digest
from test_review import sample_report


def _model():
    return build_review_model(sample_report())


def _reader_factory(*, duplicate=False, calls=None):
    calls = calls if calls is not None else []

    def reader(args):
        calls.append(list(args))
        if args[1] == "+record-get":
            ids = [args[i + 1] for i, value in enumerate(args[:-1]) if value == "--record-id"]
            return {"fields": ["产品ID"], "record_id_list": ids,
                    "data": [["TEST-01"] for _ in ids]}
        filter_data = json.loads(args[args.index("--filter-json") + 1])
        dates = [condition[2][len("ExactDate("):-1] for condition in filter_data["conditions"]]
        rows = []
        for chosen in dates:
            number = "00001" if chosen == "2026-07-09" else "00002"
            if duplicate and chosen == "2026-07-10":
                number = "00001"
            rid = "recORDER" + chosen.replace("-", "")
            rows.append([rid, [number, chosen + "T10:00:00+08:00", "合成客户", ["已拍摄"],
                               [{"id": "recPRODUCT1"}], None, None, None]])
        return {"fields": ORDER_FIELDS, "record_id_list": [rid for rid, _ in rows], "rev": 1, "has_more": False,
                "data": [row for _, row in rows]}

    return reader


def _namer(payload):
    path = "2026/7月/" + payload["shoot_date"].replace("-", "") + "_" + payload["order_id"] + "_合成"
    return path, Path(path).name


class OrderPreviewTests(unittest.TestCase):
    def setUp(self):
        self.resource = {"base_token": "SYNTHETIC", "table_name": "orders", "product_table_id": "products"}

    def test_unknown_date_is_counted_and_never_guessed(self):
        model = _model()
        model["units"][0]["capture_date"] = None
        model["units"][0]["candidate_project_ids"] = []
        model["report_id"] = "sha256:" + digest({k: v for k, v in model.items() if k != "report_id"})
        calls = []
        enriched, intents, summary = prepare_preview(model, [], self.resource, _reader_factory(calls=calls), _namer)
        self.assertEqual(summary["unknown_capture_date_units"], 1)
        self.assertEqual(summary["covered_dates"], ["2026-07-09", "2026-07-10"])
        self.assertEqual(summary["order_query_batch_count"], 1)
        self.assertTrue(all(u["capture_date"] is not None or not u["candidate_project_ids"] for u in enriched["units"]))

    def test_historical_dates_over_sixty_are_batched_and_merged(self):
        model = _model()
        units = []
        for i in range(61):
            unit = deepcopy(model["units"][0])
            unit["unit_id"] = "unit-historical-" + str(i)
            unit["files"] = [{**unit["files"][0], "source_path": "BATCH/SOURCE/" + str(i) + ".ARW",
                               "name": str(i) + ".ARW", "blake3": f"{i + 1:064x}"}]
            unit["capture_date"] = f"2020-01-{(i % 31) + 1:02d}" if i < 31 else (f"2021-02-{(i - 31) + 1:02d}" if i < 59 else f"2021-03-{i - 58:02d}")
            units.append(unit)
        model["units"] = units
        model["report_id"] = "sha256:" + digest({k: v for k, v in model.items() if k != "report_id"})
        calls = []
        _, _, summary = prepare_preview(model, [], self.resource, _reader_factory(calls=calls), _namer)
        self.assertEqual(summary["explicit_capture_date_count"], 61)
        self.assertEqual(summary["order_query_batch_count"], 2)
        self.assertGreaterEqual(summary["order_count"], 2)
        self.assertEqual(len([c for c in calls if c[1] == "+record-list"]), 2)

    def test_missing_folder_is_candidate_only_and_existing_order_skips_product_read(self):
        model = _model()
        existing = deepcopy(model["projects"][:1])
        existing[0].update(order_id="00001", dates=["2026-07-09"],
                           path="2026/7月/20260709_00001_existing", name="existing")
        model["projects"][0] = deepcopy(existing[0])
        model["report_id"] = "sha256:" + digest({k: v for k, v in model.items() if k != "report_id"})
        calls = []
        enriched, intents, summary = prepare_preview(model, existing, self.resource, _reader_factory(calls=calls), _namer)
        self.assertEqual(summary["product_record_ids_queried"], 1)
        self.assertEqual(len([c for c in calls if c[1] == "+record-get"]), 1)
        self.assertEqual(next(row for row in intents["intentions"] if row["order_id"] == "00001")["action"], "use_existing")
        candidate = next(p for p in enriched["projects"] if p.get("exists") is False)
        self.assertTrue(candidate["name"].startswith("【待建目录】"))
        self.assertTrue(all(s["decision"] == "pending" for s in enriched["initial_segments"]))

    def test_duplicate_order_across_date_groups_is_reported_and_blocked(self):
        _, intents, summary = prepare_preview(_model(), [], self.resource,
                                              _reader_factory(duplicate=True), _namer)
        self.assertEqual(summary["duplicate_order_ids"], ["00001"])
        self.assertTrue(all(row["action"] == "needs_review" for row in intents["intentions"]))

    def test_live_directory_not_in_model_is_rejected_before_enrichment(self):
        live = [{"project_id": "live-new", "order_id": "00001", "dates": ["2026-07-09"],
                 "path": "2026/7月/20260709_00001_live", "name": "live"}]
        with self.assertRaises(OrderError):
            prepare_preview(_model(), live, self.resource, _reader_factory(), _namer)

    def test_order_failure_returns_no_partial_preview(self):
        def broken(_):
            raise OrderError("synthetic failure")
        with self.assertRaises(OrderError):
            prepare_preview(_model(), [], self.resource, broken, _namer)

    def test_output_boundary_and_cli_write_only_four_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            projects = root / "projects"
            projects.mkdir()
            model_path, resource_path = root / "model.json", root / "resource.json"
            model_path.write_text(json.dumps(_model(), ensure_ascii=False), encoding="utf-8")
            resource_path.write_text(json.dumps(self.resource), encoding="utf-8")
            module = root / "folder_service.py"
            module.write_text("from pathlib import Path\ndef project_paths(payload, root):\n p=root/('2026/7月/'+payload['shoot_date'].replace('-','')+'_'+payload['order_id']+'_合成'); return p, p.name\n", encoding="utf-8")
            output = root / "preview"
            with patch("muli_sorter.order_preview.cli_read", _reader_factory()):
                self.assertEqual(main(["--model", str(model_path), "--projects", str(projects),
                                       "--resource", str(resource_path), "--folder-service-module", str(module),
                                       "--output", str(output)]), 0)
            self.assertEqual(sorted(p.name for p in output.iterdir()), sorted((
                "确认模型.json", "拍摄段确认.html", "目录处理意图.json", "读取摘要.json")))
            intentions = json.loads((output/'目录处理意图.json').read_text())['intentions']
            self.assertTrue(all(row['action']=='create_required' and not row['path'].startswith('/') for row in intentions))
            old = output / "读取摘要.json"
            before = old.read_bytes()
            self.assertEqual(main(["--model", str(model_path), "--projects", str(projects),
                                   "--resource", str(resource_path), "--folder-service-module", str(module),
                                   "--output", str(output)]), 1)
            self.assertEqual(old.read_bytes(), before)
            with self.assertRaises(ValueError):
                _validate_output_target(projects / "child", projects)

    def test_cli_read_failure_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            projects = root / "projects"
            projects.mkdir()
            model = root / "model.json"
            resource = root / "resource.json"
            model.write_text(json.dumps(_model(), ensure_ascii=False), encoding="utf-8")
            resource.write_text(json.dumps(self.resource), encoding="utf-8")
            module = root / "folder_service.py"
            module.write_text("def project_paths(payload, root): return ('2026/7月/20260709_00001_x', '20260709_00001_x')\n", encoding="utf-8")
            output = root / "failed"
            with patch("muli_sorter.order_preview.cli_read", side_effect=OrderError("no")):
                self.assertEqual(main(["--model", str(model), "--projects", str(projects), "--resource", str(resource),
                                       "--folder-service-module", str(module), "--output", str(output)]), 1)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
