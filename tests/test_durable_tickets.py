import json
from pathlib import Path
import tempfile
from unittest import TestCase, mock

from muli_sorter.archive_io import ArchiveError
from muli_sorter.archive_tickets import MAX_BYTES, PersistentTickets


TOKEN = "a" * 64
TOKENS = [format(number, "064x") for number in range(1, 5)]


class PersistentTicketsTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = (Path(self.temp.name) / "tickets").resolve()
        self.identity = {"service": "archive", "generation": 3}

    def tearDown(self):
        self.temp.cleanup()

    def test_restart_restores_ticket_and_memory_mutation_stays_in_memory(self):
        ticket = {"model": {"report_id": "r1"}, "expires_at": 123.5}
        tickets = PersistentTickets(self.root, self.identity)
        tickets[TOKEN] = ticket
        tickets[TOKEN]["expires_at"] = 0

        restarted = PersistentTickets(self.root, self.identity)
        self.assertEqual(restarted[TOKEN]["expires_at"], 123.5)
        self.assertEqual(restarted.get("f" * 64, "missing"), "missing")

    def test_lru_eviction_keeps_durable_files_and_len_is_memory_only(self):
        tickets = PersistentTickets(self.root, self.identity, memory_items=2)
        for number, token in enumerate(TOKENS[:3]):
            tickets[token] = {"number": number, "expires_at": 100 + number}

        self.assertEqual(len(tickets), 2)
        self.assertFalse(TOKENS[0] in tickets)
        self.assertEqual(tickets[TOKENS[0]]["number"], 0)
        self.assertEqual(len(tickets), 2)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         sorted(token + ".json" for token in TOKENS[:3]))

    def test_lookup_addresses_one_file_without_directory_scan(self):
        tickets = PersistentTickets(self.root, self.identity, memory_items=1)
        tickets[TOKENS[0]] = {"value": 1}
        tickets[TOKENS[1]] = {"value": 2}

        with mock.patch("os.scandir", side_effect=AssertionError("directory scan")):
            self.assertEqual(tickets[TOKENS[0]]["value"], 1)

    def test_corrupt_record_is_rejected(self):
        tickets = PersistentTickets(self.root, self.identity)
        tickets[TOKEN] = {"value": 1}
        path = self.root / (TOKEN + ".json")
        record = json.loads(path.read_text())
        record["payload"]["value"] = 2
        path.write_text(json.dumps(record))

        with self.assertRaises(ArchiveError):
            PersistentTickets(self.root, self.identity, memory_items=0).get(TOKEN)

    def test_identity_and_token_are_bound_and_tokens_are_strict(self):
        PersistentTickets(self.root, self.identity)[TOKEN] = {"value": 1}
        with self.assertRaises(ArchiveError):
            PersistentTickets(self.root, {"service": "other"}, memory_items=0).get(TOKEN)
        for invalid in ("A" * 64, "a" * 63, "a" * 65, "../" + "a" * 61, 1):
            with self.subTest(invalid=invalid), self.assertRaises(ArchiveError):
                PersistentTickets(self.root, self.identity).get(invalid)

    def test_symlink_and_oversized_records_fail_closed(self):
        tickets = PersistentTickets(self.root, self.identity)
        tickets[TOKEN] = {"value": 1}
        path = self.root / (TOKEN + ".json")
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text(path.read_text())
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(ArchiveError):
            PersistentTickets(self.root, self.identity, memory_items=0).get(TOKEN)

        path.unlink()
        path.write_bytes(b"{" + b"x" * (MAX_BYTES + 1) + b"}")
        with self.assertRaises(ArchiveError):
            PersistentTickets(self.root, self.identity, memory_items=0).get(TOKEN)

    def test_identity_and_ticket_are_copied_for_durable_binding(self):
        identity = {"service": "archive", "nested": {"version": 1}}
        ticket = {"expires_at": 88, "nested": {"value": 1}}
        tickets = PersistentTickets(self.root, identity)
        identity["nested"]["version"] = 2
        tickets[TOKEN] = ticket
        ticket["nested"]["value"] = 2

        restarted = PersistentTickets(self.root, {"service": "archive", "nested": {"version": 1}}, memory_items=0)
        self.assertEqual(restarted[TOKEN]["nested"]["value"], 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
