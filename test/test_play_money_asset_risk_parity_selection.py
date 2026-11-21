"""Candidate selection and command-line validation tests."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import unittest

import play_money_asset_risk_parity as play


class CandidateSelectionTest(unittest.TestCase):
    def test_selection_sorts_by_rank_and_ignores_candidates_after_five(self):
        candidates = [
            {"rank": rank, "symbol": f"SYM{rank}"}
            for rank in (6, 5, 4, 3, 2, 1)
        ]

        selected = play.select_candidates({"candidates": candidates})

        self.assertEqual([row["rank"] for row in selected], [1, 2, 3, 4, 5])
        self.assertEqual(
            [row["symbol"] for row in selected],
            ["SYM1", "SYM2", "SYM3", "SYM4", "SYM5"],
        )

    def test_selection_rejects_missing_or_duplicate_ranks_and_empty_symbols(self):
        invalid_candidates = (
            [{"rank": rank, "symbol": f"SYM{rank}"} for rank in (1, 2, 3, 4, 6)],
            [{"rank": rank, "symbol": f"SYM{rank}"} for rank in (1, 2, 3, 4, 4)],
            [
                {"rank": rank, "symbol": "" if rank == 5 else f"SYM{rank}"}
                for rank in range(1, 6)
            ],
            [
                {"rank": rank, "symbol": 123 if rank == 5 else f"SYM{rank}"}
                for rank in range(1, 6)
            ],
        )

        for candidates in invalid_candidates:
            with self.subTest(candidates=candidates), self.assertRaises(ValueError):
                play.select_candidates({"candidates": candidates})

    def test_cli_rejects_invalid_options_before_opening_input(self):
        invalid_arguments = (
            ["missing.json", "--lookback", "1"],
            ["missing.json", "--max-age-days", "-1"],
            ["missing.json", "--format", "csv"],
        )

        for arguments in invalid_arguments:
            stdout, stderr = StringIO(), StringIO()
            with (
                self.subTest(arguments=arguments),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                play.main(arguments)

            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("error:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
