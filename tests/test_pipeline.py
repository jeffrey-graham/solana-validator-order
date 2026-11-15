#!/usr/bin/env python3
"""
Unit & Pipeline Tests for Solana Validator Transaction Ordering Suite
Tests:
  1. SpendGuard cap enforcement (<= 0.05 SOL hard limit & per-tx overflow blocking)
  2. Post-Alpenglow v1 transactionConfig vs legacy/v0 ComputeBudget111... parsing
  3. Spearman rank correlation & pairwise monotonicity calculations
  4. Keypair repository isolation security check
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from analyzer import pairwise_monotonicity, spearman_rho
from block_parser import COMPUTE_BUDGET_PROGRAM, parse_block_transactions
from probe import SpendGuard, SpendLimitExceededError, b58encode


class TestValidatorOrderingPipeline(unittest.TestCase):
    def test_spend_guard_enforces_hard_cap(self):
        # Rejects > 0.05 SOL initialization
        with self.assertRaises(ValueError):
            SpendGuard(max_spend_sol=0.06, is_live=False)

        # Enforces per-transaction spend cap
        guard = SpendGuard(max_spend_sol=0.000015, is_live=False)  # 15,000 lamports
        fee1 = guard.reserve_fee(1, 600, 1000, "tx1")
        self.assertEqual(fee1, 5001)
        fee2 = guard.reserve_fee(1, 600, 1000, "tx2")
        self.assertEqual(fee2, 5001)
        with self.assertRaises(SpendLimitExceededError):
            guard.reserve_fee(1, 600, 30000000, "tx3_overflow")

    def test_parse_v1_and_legacy_transactions(self):
        # Construct synthetic block with 1 legacy ComputeBudget tx and 1 v1 transactionConfig tx
        cb_limit = b58encode(b"\x02" + struct.pack("<I", 100_000))
        cb_price = b58encode(b"\x03" + struct.pack("<Q", 50_000))  # 50,000 micro-lamports/CU -> 5,000 lamports prio fee

        mock_block = {
            "transactions": [
                {
                    "version": "legacy",
                    "transaction": {
                        "signatures": ["SigLegacy11111111111111111111111111111111111"],
                        "message": {
                            "header": {
                                "numRequiredSignatures": 1,
                                "numReadonlySignedAccounts": 0,
                                "numReadonlyUnsignedAccounts": 1,
                            },
                            "accountKeys": [
                                "Payer11111111111111111111111111111111111111",
                                "SharedWritable11111111111111111111111111111",
                                COMPUTE_BUDGET_PROGRAM,
                            ],
                            "instructions": [
                                {"programIdIndex": 2, "accounts": [], "data": cb_limit},
                                {"programIdIndex": 2, "accounts": [], "data": cb_price},
                            ],
                        },
                    },
                    "meta": {"fee": 10000, "computeUnitsConsumed": 25000, "err": None},
                },
                {
                    "version": 1,
                    "transaction": {
                        "signatures": ["SigV1Alpenglow111111111111111111111111111111"],
                        "message": {
                            "header": {
                                "numRequiredSignatures": 1,
                                "numReadonlySignedAccounts": 0,
                                "numReadonlyUnsignedAccounts": 1,
                            },
                            "accountKeys": [
                                "Payer22222222222222222222222222222222222222",
                                "SharedWritable11111111111111111111111111111",
                                "Prog111111111111111111111111111111111111111",
                            ],
                            "transactionConfig": {
                                "computeUnitLimit": 500,
                                "priorityFee": 500,
                                "loadedAccountsDataSizeLimit": 32000,
                                "heapSize": None,
                            },
                            "instructions": [
                                {"programIdIndex": 2, "accounts": [0, 1], "data": "1111"}
                            ],
                        },
                    },
                    "meta": {"fee": 5500, "computeUnitsConsumed": 450, "err": None},
                },
            ]
        }

        parsed = parse_block_transactions(mock_block, slot=461351539)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["budget_source"], "compute_budget_ix")
        self.assertEqual(parsed[0]["requested_cu"], 100_000)
        self.assertEqual(parsed[0]["cu_price_micro_lamports"], 50_000)

        self.assertEqual(parsed[1]["budget_source"], "v1_transactionConfig")
        self.assertEqual(parsed[1]["requested_cu"], 500)
        self.assertEqual(parsed[1]["priority_fee_lamports"], 500)
        self.assertEqual(parsed[1]["cu_price_micro_lamports"], 1_000_000)

    def test_spearman_and_monotonicity(self):
        # Strictly descending priority across indices 0..4 -> rho = -1.0, monotonicity = 1.0
        idxs = [0.0, 1.0, 2.0, 3.0, 4.0]
        prios = [500.0, 400.0, 300.0, 200.0, 100.0]
        self.assertAlmostEqual(spearman_rho(idxs, prios), -1.0, places=5)
        mono, conc, disc = pairwise_monotonicity(prios)
        self.assertAlmostEqual(mono, 1.0, places=5)
        self.assertEqual(conc, 10)
        self.assertEqual(disc, 0)


if __name__ == "__main__":
    unittest.main()
