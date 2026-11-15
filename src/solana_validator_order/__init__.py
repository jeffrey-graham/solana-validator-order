"""
`solana_validator_order` — Open-Source Solana Banking Stage & Priority-Fee SDK
===============================================================================
Provides:
  - Per-writable-account priority-fee percentile estimation (`SolanaPriorityFeeClient`)
  - Deterministic `ComputeBudgetProgram` instruction encoders (`0x02` & `0x03`)
  - Binary instruction & state codecs for `ValidatorOrderRegistry` (`CmNC6vKh7Tr99ucPJacvgibwVE4JgUhqunyQ3qeCm6Do`)
  - Live Solana Mainnet-Beta & Devnet Program ID verification + SVM `simulateTransaction` probe
  - Post-Alpenglow (`legacy` / `v0` / `v1`) block parsing and Spearman/monotonicity ordering analytics
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Any

_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

__version__ = "0.2.0"

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # Legacy empirical pipeline modules
    "analyze_block": ("analyzer", "analyze_block"),
    "analyze_contending_queues": ("analyzer", "analyze_contending_queues"),
    "pairwise_monotonicity": ("analyzer", "pairwise_monotonicity"),
    "spearman_rho": ("analyzer", "spearman_rho"),
    "COMPUTE_BUDGET_PROGRAM": ("block_parser", "COMPUTE_BUDGET_PROGRAM"),
    "JITO_TIP_ACCOUNTS": ("block_parser", "JITO_TIP_ACCOUNTS"),
    "extract_writable_accounts": ("block_parser", "extract_writable_accounts"),
    "parse_block_transactions": ("block_parser", "parse_block_transactions"),
    "parse_compute_budget_ixs": ("block_parser", "parse_compute_budget_ixs"),
    # Fee estimator SDK
    "COMPUTE_BUDGET_PROGRAM_ID": (".fee_estimator", "COMPUTE_BUDGET_PROGRAM_ID"),
    "DEFAULT_DEVNET_RPC": (".fee_estimator", "DEFAULT_DEVNET_RPC"),
    "DEFAULT_MAINNET_RPC": (".fee_estimator", "DEFAULT_MAINNET_RPC"),
    "FeeEstimate": (".fee_estimator", "FeeEstimate"),
    "FeeTierRecommendation": (".fee_estimator", "FeeTierRecommendation"),
    "SolanaPriorityFeeClient": (".fee_estimator", "SolanaPriorityFeeClient"),
    "b58decode": (".fee_estimator", "b58decode"),
    "b58encode": (".fee_estimator", "b58encode"),
    "calculate_total_fee_lamports": (".fee_estimator", "calculate_total_fee_lamports"),
    "compute_percentile": (".fee_estimator", "compute_percentile"),
    "encode_set_compute_unit_limit": (".fee_estimator", "encode_set_compute_unit_limit"),
    "encode_set_compute_unit_price": (".fee_estimator", "encode_set_compute_unit_price"),
    "validate_pubkey": (".fee_estimator", "validate_pubkey"),
    # Program verifier
    "CANONICAL_PROGRAMS": (".program_verifier", "CANONICAL_PROGRAMS"),
    "JUPITER_V6_PROGRAM_ID": (".program_verifier", "JUPITER_V6_PROGRAM_ID"),
    "MEMO_PROGRAM_V2_ID": (".program_verifier", "MEMO_PROGRAM_V2_ID"),
    "RAYDIUM_AMM_V4_PROGRAM_ID": (".program_verifier", "RAYDIUM_AMM_V4_PROGRAM_ID"),
    "build_unsigned_simulation_probe_tx_base64": (".program_verifier", "build_unsigned_simulation_probe_tx_base64"),
    "simulate_compute_budget_probe": (".program_verifier", "simulate_compute_budget_probe"),
    "verify_onchain_programs": (".program_verifier", "verify_onchain_programs"),
    # Registry client
    "CHECKPOINT_STATE_LEN": (".registry_client", "CHECKPOINT_STATE_LEN"),
    "DEVNET_DEPLOYER_PUBKEY": (".registry_client", "DEVNET_DEPLOYER_PUBKEY"),
    "IX_PUBLISH_FEE_CHECKPOINT": (".registry_client", "IX_PUBLISH_FEE_CHECKPOINT"),
    "IX_RECORD_PROBE_PULSE": (".registry_client", "IX_RECORD_PROBE_PULSE"),
    "PROBE_STATE_LEN": (".registry_client", "PROBE_STATE_LEN"),
    "FeeCheckpointAccount": (".registry_client", "FeeCheckpointAccount"),
    "ProbeRingEntry": (".registry_client", "ProbeRingEntry"),
    "ProbeStateAccount": (".registry_client", "ProbeStateAccount"),
    "VALIDATOR_ORDER_REGISTRY_PROGRAM_ID": (".registry_client", "VALIDATOR_ORDER_REGISTRY_PROGRAM_ID"),
    "decode_fee_checkpoint": (".registry_client", "decode_fee_checkpoint"),
    "decode_probe_state": (".registry_client", "decode_probe_state"),
    "encode_publish_fee_checkpoint": (".registry_client", "encode_publish_fee_checkpoint"),
    "encode_record_probe_pulse": (".registry_client", "encode_record_probe_pulse"),
    "simulate_local_probe_processor": (".registry_client", "simulate_local_probe_processor"),
}

__all__ = ["__version__", *_LAZY_EXPORTS.keys()]


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr_name = target
    mod = importlib.import_module(mod_name, __name__) if mod_name.startswith(".") else importlib.import_module(mod_name)
    val = getattr(mod, attr_name)
    globals()[name] = val
    return val
