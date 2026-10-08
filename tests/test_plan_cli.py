from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from test_review import sample_report, draft
from muli_sorter.check_plan import main
from muli_sorter.review import build_review_model


class PlanCliTests(unittest.TestCase):
    def test_cli_outputs_validated_non_executable_plan_without_writing(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            model = build_review_model(sample_report())
            plan = draft(model)
            plan["segments"][0].update(decision="confirmed", project_id="p1")
            (root / "model.json").write_text(json.dumps(model))
            (root / "plan.json").write_text(json.dumps(plan))
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            output = StringIO()
            with redirect_stdout(output):
                result = main(["--model", str(root / "model.json"), "--decisions", str(root / "plan.json")])
            self.assertEqual(result, 0)
            self.assertFalse(json.loads(output.getvalue())["executable"])
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})


if __name__ == "__main__":
    unittest.main()
