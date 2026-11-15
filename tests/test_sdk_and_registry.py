#!/usr/bin/env python3
"""
Unit & Integration Tests for `solana_validator_order` SDK, Registry Codecs & Verifier
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Any, Dict

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from solana_validator_order import (  # noqa: E402
    CHECKPOINT_STATE_LEN,
    PROBE_STATE_LEN,
    VALIDATOR_ORDER_REGISTRY_PROGRAM_ID,
    SolanaPriorityFeeClient,
    b58decode,
    b58encode,
    calculate_total_fee_lamports,
    decode_fee_checkpoint,
    decode_probe_state,
    encode_publish_fee_checkpoint,
    encode_record_probe_pulse,
    encode_set_compute_unit_limit,
    encode_set_compute_unit_price,
    simulate_local_probe_processor,
    verify_onchain_programs,
)
from solana_validator_order.registry_client import CHECKPOINT_STATE_MAGIC, CHECKPOINT_STATE_STRUCT  # noqa: E402


class TestSolanaPriorityFeeSDK(unittest.TestCase):
    def test_compute_budget_wire_encoding_and_fee_math(self) -> None:
        limit_ix = encode_set_compute_unit_limit(180_000)
        self.assertEqual(limit_ix.hex(), "0220bf0200")

        price_ix = encode_set_compute_unit_price(250_000)
        self.assertEqual(price_ix.hex(), "0390d0030000000000")

        # 5000 + ceil(180000 * 250000 / 1_000_000) = 50_000 lamports
        self.assertEqual(calculate_total_fee_lamports(180_000, 250_000, num_signatures=1), 50_000)
        # Non-exact division rounds UP just like Agave runtime
        self.assertEqual(calculate_total_fee_lamports(600, 1_000, num_signatures=1), 5_001)

        with self.assertRaises(ValueError):
            encode_set_compute_unit_limit(0)
        with self.assertRaises(ValueError):
            encode_set_compute_unit_limit(1_500_000)

    def test_priority_fee_client_percentiles_and_surge(self) -> None:
        mock_rows = [
            {"slot": 469_000_000 + i, "prioritizationFee": (10_000 + i * 500) if i < 80 else (250_000 + (i - 80) * 10_000)}
            for i in range(100)
        ]

        def fake_transport(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            self.assertEqual(payload["method"], "getRecentPrioritizationFees")
            return {"jsonrpc": "2.0", "id": 1, "result": mock_rows}

        client = SolanaPriorityFeeClient("https://mock.solana.rpc", rpc_transport=fake_transport)
        est = client.estimate_writable_lock_fees(
            ["675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"],
            cu_limit=150_000,
        )

        self.assertEqual(est.slots_sampled, 100)
        self.assertEqual(est.congestion_regime, "hot_writable_lock_contention")
        self.assertGreater(est.surge_multiplier, 2.0)
        self.assertLessEqual(
            est.recommendations["economy"].cu_price_micro_lamports,
            est.recommendations["standard"].cu_price_micro_lamports,
        )
        self.assertLessEqual(
            est.recommendations["standard"].cu_price_micro_lamports,
            est.recommendations["fast"].cu_price_micro_lamports,
        )
        self.assertLessEqual(
            est.recommendations["fast"].cu_price_micro_lamports,
            est.recommendations["turbo"].cu_price_micro_lamports,
        )

    def test_validator_order_registry_state_machine_and_codecs(self) -> None:
        payer = "3pcmw8VQooZne1LehScj8FoWjtRF9nJqNfD8h8c5VmmY"
        buf = bytearray(PROBE_STATE_LEN)

        # Simulate inverted arrival order on shared writable lock:
        # Tx sent last (seq=5, 300k uLamports) lands first, followed by seq=2 and seq=0
        for seq, price in [(5, 300_000), (2, 15_000), (0, 1_000)]:
            ix = encode_record_probe_pulse(burst_id=77, send_seq=seq, cu_price_micro_lamports=price)
            buf, _ = simulate_local_probe_processor(buf, payer, slot=461_352_851, ix_data=ix)

        state = decode_probe_state(bytes(buf))
        self.assertEqual(state.authority, payer)
        self.assertEqual(state.active_burst_id, 77)
        self.assertEqual(state.pulse_count, 3)
        self.assertEqual(state.priority_reorder_count, 2)
        self.assertEqual(state.max_cu_price_micro_lamports, 300_000)
        self.assertEqual([e.send_seq for e in state.entries], [5, 2, 0])

        # Verify FeeCheckpoint codec including u16 sample_size > 32767
        target = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
        cp_ix = encode_publish_fee_checkpoint(
            target_writable_account=target,
            slot=469_767_785,
            p50_micro_lamports=12_000,
            p75_micro_lamports=48_000,
            p90_micro_lamports=195_000,
            spearman_rho_bps=-2139,
            sample_size=45_000,
        )
        self.assertEqual(len(cp_ix), 69)

        raw_cp = CHECKPOINT_STATE_STRUCT.pack(
            CHECKPOINT_STATE_MAGIC,
            1,
            b"\x00\x00\x00",
            b58decode(payer),
            b58decode(target),
            469_767_785,
            12_000,
            48_000,
            195_000,
            -2139,
            45_000,
            1,
        )
        self.assertEqual(len(raw_cp), CHECKPOINT_STATE_LEN)
        decoded_cp = decode_fee_checkpoint(raw_cp)
        self.assertEqual(decoded_cp.target_writable_account, target)
        self.assertEqual(decoded_cp.sample_size, 45_000)
        self.assertEqual(decoded_cp.spearman_rho_bps, -2139)

    def test_program_verifier_with_deterministic_transport(self) -> None:
        def fake_transport(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            method = payload["method"]
            if method == "getAccountInfo":
                prog_id = payload["params"][0]
                if prog_id == VALIDATOR_ORDER_REGISTRY_PROGRAM_ID:
                    return {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "value": {
                                "executable": True,
                                "owner": "BPFLoader2111111111111111111111111111111111",
                                "lamports": 42_265_600,
                            }
                        },
                    }
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"value": {"executable": True, "owner": "NativeLoader1111111111111111111111111111111", "lamports": 1}},
                }
            if method == "getBalance":
                return {"jsonrpc": "2.0", "id": 1, "result": {"value": 500_000_000}}
            if method == "simulateTransaction":
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "value": {
                            "err": None,
                            "fee": 5625,
                            "unitsConsumed": 23865,
                            "replacementBlockhash": {"blockhash": "11111111111111111111111111111111"},
                            "logs": ["Program ComputeBudget111111111111111111111111111111 success"],
                        }
                    },
                }
            raise AssertionError(f"Unexpected RPC method {method}")

        report = verify_onchain_programs(rpc_transport=fake_transport)
        self.assertTrue(all(p["executable"] for p in report["programs"]))
        self.assertTrue(report["devnet_svm_simulation"]["succeeded"])
        self.assertEqual(report["devnet_svm_simulation"]["fee_lamports"], 5625)

    def test_empirical_mainnet_dataset_integrity(self) -> None:
        data_file = os.path.join(REPO_ROOT, "data", "raw_measurements.json")
        with open(data_file, "r", encoding="utf-8") as f:
            dataset = json.load(f)
        blocks = dataset["mainnet_block_measurements"]
        self.assertEqual(len(blocks), 16)
        clients = {b["validator"]["client"] for b in blocks}
        self.assertGreaterEqual(len(clients), 7)
        for b in blocks:
            self.assertGreater(b["slot"], 460_000_000)
            self.assertTrue(len(b["verifiable_sample_signatures"]) >= 5)
            # Round-trip base58 check on validator identity
            ident = b["validator"]["identity"]
            self.assertEqual(b58encode(b58decode(ident)), ident)


if __name__ == "__main__":
    unittest.main()
