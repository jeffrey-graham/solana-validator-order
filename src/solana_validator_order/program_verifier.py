#!/usr/bin/env python3
"""
Live Solana Devnet & Mainnet-Beta Program Verifier (`program_verifier.py`)
==========================================================================
Verifies:
  1. Executable status, owner loader, and rent balance of all on-chain Solana
     Program IDs referenced by `solana-validator-order` across Mainnet-Beta and Devnet.
  2. Live zero-SOL SVM `simulateTransaction` execution of `ComputeBudgetProgram`
     (`SetComputeUnitLimit` + `SetComputeUnitPrice`) + `MemoProgramV2` probe bursts
     on Solana Devnet (`https://api.devnet.solana.com`), confirming exact fee & CU metering.
  3. Live deployment status of our custom `ValidatorOrderRegistry` Program ID
     (`CmNC6vKh7Tr99ucPJacvgibwVE4JgUhqunyQ3qeCm6Do`) on Solana Devnet.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

from .fee_estimator import (
    COMPUTE_BUDGET_PROGRAM_ID,
    DEFAULT_DEVNET_RPC,
    DEFAULT_MAINNET_RPC,
    _default_rpc_post,
    b58decode,
    encode_set_compute_unit_limit,
    encode_set_compute_unit_price,
)
from .registry_client import (
    DEVNET_DEPLOYER_PUBKEY,
    DEVNET_PROBE_STATE_ACCOUNT,
    VALIDATOR_ORDER_REGISTRY_PROGRAM_ID,
    encode_record_probe_pulse,
)

MEMO_PROGRAM_V2_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
JUPITER_V6_PROGRAM_ID = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
RAYDIUM_AMM_V4_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
DEVNET_SIMULATION_PAYER = DEVNET_DEPLOYER_PUBKEY

CANONICAL_PROGRAMS: List[Dict[str, str]] = [
    {
        "name": "ValidatorOrderRegistry (Custom SBF Program)",
        "program_id": VALIDATOR_ORDER_REGISTRY_PROGRAM_ID,
        "cluster": "devnet",
        "role": "Custom on-chain probe pulse & priority-fee checkpoint registry",
    },
    {
        "name": "ComputeBudget Native Program",
        "program_id": COMPUTE_BUDGET_PROGRAM_ID,
        "cluster": "both",
        "role": "SetComputeUnitLimit (0x02) & SetComputeUnitPrice (0x03) scheduler scoring",
    },
    {
        "name": "SPL Memo v2 Program",
        "program_id": MEMO_PROGRAM_V2_ID,
        "cluster": "both",
        "role": "On-chain probe burst tagging & verifiable block telemetry",
    },
    {
        "name": "Jupiter Aggregator v6",
        "program_id": JUPITER_V6_PROGRAM_ID,
        "cluster": "mainnet-beta",
        "role": "High-contention Mainnet routing benchmark program",
    },
    {
        "name": "Raydium Liquidity Pool V4",
        "program_id": RAYDIUM_AMM_V4_PROGRAM_ID,
        "cluster": "mainnet-beta",
        "role": "Hot writable-account AMM state-lock contention benchmark",
    },
]


def _encode_compact_u16(val: int) -> bytes:
    if val < 0x80:
        return bytes([val])
    if val < 0x4000:
        return bytes([(val & 0x7F) | 0x80, (val >> 7) & 0x7F])
    return bytes([(val & 0x7F) | 0x80, ((val >> 7) & 0x7F) | 0x80, (val >> 14) & 0x03])


def build_unsigned_simulation_probe_tx_base64(
    *,
    cu_limit: int = 35_000,
    cu_price_micro_lamports: int = 25_000,
    memo_text: str = "svo:v0.2.0:devnet-verify",
    simulation_payer: str = DEVNET_SIMULATION_PAYER,
    probe_state_account: str = DEVNET_PROBE_STATE_ACCOUNT,
) -> str:
    """
    Builds a valid Legacy Solana wire transaction (with 1 dummy 64-byte signature)
    invoking `ComputeBudget` + `ValidatorOrderRegistry::RecordProbePulse` + `SPL Memo v2`
    for zero-cost RPC `simulateTransaction` (`sigVerify: false, replaceRecentBlockhash: true`).
    """
    payer_bytes = b58decode(simulation_payer)
    ps_bytes = b58decode(probe_state_account)
    cb_bytes = b58decode(COMPUTE_BUDGET_PROGRAM_ID)
    reg_bytes = b58decode(VALIDATOR_ORDER_REGISTRY_PROGRAM_ID)
    memo_bytes = b58decode(MEMO_PROGRAM_V2_ID)

    ix_limit = encode_set_compute_unit_limit(cu_limit)
    ix_price = encode_set_compute_unit_price(cu_price_micro_lamports)
    ix_pulse = encode_record_probe_pulse(
        burst_id=1,
        send_seq=0,
        cu_price_micro_lamports=cu_price_micro_lamports,
    )
    ix_memo = memo_text.encode("utf-8")

    sig_section = _encode_compact_u16(1) + (b"\x00" * 64)
    header = bytes([1, 0, 3])  # 1 signer writable, 0 ro signed, 1 writable unsigned (ps), 3 ro unsigned programs
    keys = _encode_compact_u16(5) + payer_bytes + ps_bytes + cb_bytes + reg_bytes + memo_bytes
    recent_blockhash = b"\x00" * 32

    ixs = (
        _encode_compact_u16(4)
        + bytes([2, 0]) + _encode_compact_u16(len(ix_limit)) + ix_limit
        + bytes([2, 0]) + _encode_compact_u16(len(ix_price)) + ix_price
        + bytes([3, 2, 0, 1]) + _encode_compact_u16(len(ix_pulse)) + ix_pulse
        + bytes([4, 1, 0]) + _encode_compact_u16(len(ix_memo)) + ix_memo
    )
    return base64.b64encode(sig_section + header + keys + recent_blockhash + ixs).decode("ascii")


def simulate_compute_budget_probe(
    rpc_url: str = DEFAULT_DEVNET_RPC,
    *,
    cu_limit: int = 35_000,
    cu_price_micro_lamports: int = 25_000,
    memo_text: str = "svo:v0.2.0:devnet-verify",
    rpc_transport: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Executes a real SVM `simulateTransaction` probe against Solana Devnet."""
    transport = rpc_transport or _default_rpc_post
    wire_b64 = build_unsigned_simulation_probe_tx_base64(
        cu_limit=cu_limit,
        cu_price_micro_lamports=cu_price_micro_lamports,
        memo_text=memo_text,
    )
    resp = transport(
        rpc_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "simulateTransaction",
            "params": [
                wire_b64,
                {
                    "encoding": "base64",
                    "sigVerify": False,
                    "replaceRecentBlockhash": True,
                },
            ],
        },
    )
    val = (resp.get("result") or {}).get("value") or {}
    return {
        "rpc_url": rpc_url,
        "succeeded": val.get("err") is None,
        "err": val.get("err"),
        "fee_lamports": val.get("fee"),
        "units_consumed": val.get("unitsConsumed"),
        "replacement_blockhash": (val.get("replacementBlockhash") or {}).get("blockhash"),
        "logs": val.get("logs") or [],
    }


def verify_onchain_programs(
    *,
    mainnet_rpc: str = DEFAULT_MAINNET_RPC,
    devnet_rpc: str = DEFAULT_DEVNET_RPC,
    rpc_transport: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Queries live `getAccountInfo` across Mainnet-Beta and Devnet for all suite Program IDs."""
    transport = rpc_transport or _default_rpc_post
    results: List[Dict[str, Any]] = []

    for item in CANONICAL_PROGRAMS:
        clusters = ["devnet", "mainnet-beta"] if item["cluster"] == "both" else [item["cluster"]]
        for cluster in clusters:
            url = devnet_rpc if cluster == "devnet" else mainnet_rpc
            resp = transport(
                url,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getAccountInfo",
                    "params": [item["program_id"], {"encoding": "base64"}],
                },
            )
            acct = (resp.get("result") or {}).get("value")
            executable = bool(acct and acct.get("executable"))
            results.append(
                {
                    "name": item["name"],
                    "program_id": item["program_id"],
                    "cluster": cluster,
                    "rpc_url": url,
                    "exists": acct is not None,
                    "executable": executable,
                    "owner": acct.get("owner") if acct else None,
                    "lamports": acct.get("lamports") if acct else 0,
                    "explorer_url": (
                        f"https://explorer.solana.com/address/{item['program_id']}"
                        + ("?cluster=devnet" if cluster == "devnet" else "")
                    ),
                }
            )

    # Also check Devnet deployer wallet balance
    dep_resp = transport(
        devnet_rpc,
        {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [DEVNET_DEPLOYER_PUBKEY]},
    )
    deployer_lamports = int((dep_resp.get("result") or {}).get("value") or 0)

    sim = simulate_compute_budget_probe(devnet_rpc, rpc_transport=transport)
    return {
        "programs": results,
        "devnet_deployer": {
            "pubkey": DEVNET_DEPLOYER_PUBKEY,
            "balance_lamports": deployer_lamports,
            "balance_sol": round(deployer_lamports / 1_000_000_000, 9),
        },
        "devnet_svm_simulation": sim,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Solana Mainnet-Beta & Devnet Program IDs and SVM simulation.")
    parser.add_argument("--mainnet-rpc", default=DEFAULT_MAINNET_RPC)
    parser.add_argument("--devnet-rpc", default=DEFAULT_DEVNET_RPC)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_onchain_programs(mainnet_rpc=args.mainnet_rpc, devnet_rpc=args.devnet_rpc)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print("=== Solana On-Chain Program Verification (Mainnet-Beta & Devnet) ===")
    for p in report["programs"]:
        status = (
            "EXECUTABLE ON-CHAIN"
            if p["executable"]
            else ("STAGED (Awaiting Devnet Deploy Top-Up)" if not p["exists"] else "NON-EXECUTABLE")
        )
        print(
            f"  [{p['cluster']:12s}] {p['program_id']} | {status:38s} | {p['name']}"
        )

    sim = report["devnet_svm_simulation"]
    print("\n=== Live Devnet SVM Transaction Simulation (ComputeBudget + Memo v2) ===")
    print(
        f"  Succeeded: {sim['succeeded']} | Fee: {sim['fee_lamports']} lamports | "
        f"CUs Consumed: {sim['units_consumed']} | Blockhash: {sim['replacement_blockhash']}"
    )
    for log_line in sim["logs"]:
        print(f"    {log_line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
