#!/usr/bin/env python3
"""
Per-Writable-Account Priority Fee Percentile SDK & CLI (`fee_estimator.py`)
===========================================================================
Models Solana's Banking Stage prio-graph account-lock contention by querying
`getRecentPrioritizationFees` scoped to the exact writable state locks touched
by a transaction (e.g., Raydium AMM pool, Jupiter shared accounts, Whirlpool),
rather than overpaying naive global slot averages.

Zero external dependencies (Python 3.10+ standard library).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"
DEFAULT_MAINNET_RPC = os.environ.get("SOLANA_RPC_URL", "https://solana-rpc.publicnode.com")
DEFAULT_DEVNET_RPC = os.environ.get("SOLANA_DEVNET_RPC_URL", "https://api.devnet.solana.com")
BASE_SIGNATURE_FEE_LAMPORTS = 5_000
LAMPORTS_PER_SOL = 1_000_000_000

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_MAP = {c: i for i, c in enumerate(B58_ALPHABET)}


def b58encode(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    res: List[str] = []
    while num > 0:
        num, rem = divmod(num, 58)
        res.append(B58_ALPHABET[rem])
    res.reverse()
    pad = len(data) - len(data.lstrip(b"\x00"))
    return ("1" * pad) + "".join(res)


def b58decode(s: str) -> bytes:
    if not s:
        return b""
    num = 0
    for ch in s:
        if ch not in B58_MAP:
            raise ValueError(f"Invalid base58 character: {ch!r}")
        num = num * 58 + B58_MAP[ch]
    res = bytearray()
    while num > 0:
        num, rem = divmod(num, 256)
        res.append(rem)
    res.reverse()
    pad = len(s) - len(s.lstrip("1"))
    return (b"\x00" * pad) + bytes(res)


def validate_pubkey(pubkey: str) -> str:
    cleaned = pubkey.strip()
    decoded = b58decode(cleaned)
    if len(decoded) != 32:
        raise ValueError(f"Invalid Solana public key {cleaned!r}: decoded {len(decoded)} bytes (expected 32)")
    return cleaned


def encode_set_compute_unit_limit(units: int) -> bytes:
    """Encodes `ComputeBudgetInstruction::SetComputeUnitLimit(u32)` (discriminant `0x02`)."""
    if not (1 <= int(units) <= 1_400_000):
        raise ValueError(f"compute unit limit must be in [1, 1_400_000], got {units}")
    return b"\x02" + struct.pack("<I", int(units))


def encode_set_compute_unit_price(micro_lamports: int) -> bytes:
    """Encodes `ComputeBudgetInstruction::SetComputeUnitPrice(u64)` (discriminant `0x03`)."""
    if int(micro_lamports) < 0 or int(micro_lamports) >= (1 << 64):
        raise ValueError(f"micro_lamports must be a non-negative u64, got {micro_lamports}")
    return b"\x03" + struct.pack("<Q", int(micro_lamports))


def calculate_total_fee_lamports(
    cu_limit: int,
    cu_price_micro_lamports: int,
    num_signatures: int = 1,
) -> int:
    """Exact Solana fee formula: `5000 * signatures + ceil(cu_limit * cu_price_micro_lamports / 1_000_000)`."""
    if num_signatures < 1:
        raise ValueError("num_signatures must be >= 1")
    base = num_signatures * BASE_SIGNATURE_FEE_LAMPORTS
    prio = (int(cu_limit) * int(cu_price_micro_lamports) + 999_999) // 1_000_000
    return base + prio


def compute_percentile(sorted_values: Sequence[int], pct: float) -> int:
    """Linear-interpolated percentile over pre-sorted integer micro-lamport values."""
    if not sorted_values:
        return 0
    if pct <= 0:
        return int(sorted_values[0])
    if pct >= 100:
        return int(sorted_values[-1])
    pos = (pct / 100.0) * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return int(sorted_values[lo])
    frac = pos - lo
    return int(round(sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac))


@dataclass(frozen=True)
class FeeTierRecommendation:
    tier: str
    cu_limit: int
    cu_price_micro_lamports: int
    priority_fee_lamports: int
    total_fee_lamports: int
    total_fee_sol: float
    set_cu_limit_ix_hex: str
    set_cu_price_ix_hex: str


@dataclass(frozen=True)
class FeeEstimate:
    rpc_url: str
    writable_accounts: List[str]
    slots_sampled: int
    nonzero_slots: int
    nonzero_ratio: float
    min_slot: int
    max_slot: int
    percentiles_all_slots: Dict[str, int]
    percentiles_nonzero_slots: Dict[str, int]
    recent_20_slot_ema: int
    surge_multiplier: float
    congestion_regime: str
    recommendations: Dict[str, FeeTierRecommendation]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _default_rpc_post(rpc_url: str, payload: Dict[str, Any], timeout: int = 15) -> Dict[str, Any]:
    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        rpc_url,
        data=raw,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "solana-validator-order-sdk/0.2.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if isinstance(body, dict) and "error" in body:
        raise RuntimeError(f"Solana RPC error from {rpc_url}: {body['error']}")
    return body


class SolanaPriorityFeeClient:
    """
    Client for estimating per-writable-account Solana priority fees and building
    deterministic `ComputeBudgetProgram` instruction payloads.
    """

    def __init__(
        self,
        rpc_url: str = DEFAULT_MAINNET_RPC,
        *,
        rpc_transport: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> None:
        self.rpc_url = rpc_url
        self._transport = rpc_transport or _default_rpc_post

    def get_recent_prioritization_fees(self, writable_accounts: Sequence[str] = ()) -> List[Dict[str, Any]]:
        validated = [validate_pubkey(a) for a in writable_accounts[:128]]
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getRecentPrioritizationFees",
            "params": [validated] if validated else [[]],
        }
        resp = self._transport(self.rpc_url, payload)
        result = resp.get("result") or []
        if not isinstance(result, list):
            raise RuntimeError(f"Unexpected getRecentPrioritizationFees response: {result!r}")
        return sorted(result, key=lambda x: int(x.get("slot", 0)))

    def estimate_writable_lock_fees(
        self,
        writable_accounts: Sequence[str] = (),
        *,
        cu_limit: int = 200_000,
        num_signatures: int = 1,
        min_floor_micro_lamports: int = 1_000,
    ) -> FeeEstimate:
        """
        Computes per-writable-account priority-fee percentiles across recent slots,
        detects short-window contention surges, and returns tiered recommendations.
        """
        if not (1 <= cu_limit <= 1_400_000):
            raise ValueError(f"cu_limit must be in [1, 1_400_000], got {cu_limit}")

        validated = [validate_pubkey(a) for a in writable_accounts[:128]]
        rows = self.get_recent_prioritization_fees(validated)

        fees = [max(0, int(r.get("prioritizationFee", 0))) for r in rows]
        slots = [int(r.get("slot", 0)) for r in rows]
        sorted_all = sorted(fees)
        nonzero = sorted(f for f in fees if f > 0)

        pct_keys = [("p25", 25.0), ("p50", 50.0), ("p75", 75.0), ("p90", 90.0), ("p95", 95.0)]
        pct_all = {k: compute_percentile(sorted_all, p) for k, p in pct_keys}
        pct_nz = {k: compute_percentile(nonzero, p) for k, p in pct_keys}

        # Short-window EMA across most recent 20 slots to detect burst contention on hot state locks
        recent_tail = fees[-20:] if fees else []
        ema_val = 0.0
        alpha = 0.25
        for idx, val in enumerate(recent_tail):
            ema_val = float(val) if idx == 0 else (alpha * float(val) + (1.0 - alpha) * ema_val)
        ema_int = int(round(ema_val))

        baseline_med = max(1, pct_all["p50"] or pct_nz["p50"] or 1)
        surge = round(ema_val / baseline_med, 3) if recent_tail else 1.0
        nz_ratio = round(len(nonzero) / len(fees), 4) if fees else 0.0

        if nz_ratio >= 0.50 or pct_all["p75"] >= 50_000:
            regime = "hot_writable_lock_contention"
        elif nz_ratio >= 0.10 or pct_nz["p75"] >= 10_000:
            regime = "intermittent_burst_contention"
        else:
            regime = "uncontested_low_floor"

        # Use non-zero distribution blended with overall slots so sporadic hot-pool bursts aren't underpriced at 0
        econ_price = max(min_floor_micro_lamports // 2, pct_all["p25"])
        std_price = max(min_floor_micro_lamports, pct_all["p50"], pct_nz["p25"])
        fast_price = max(std_price, pct_all["p75"], pct_nz["p50"], ema_int)
        turbo_price = max(fast_price, pct_all["p95"], pct_nz["p90"])

        tiers: Dict[str, FeeTierRecommendation] = {}
        set_limit_hex = encode_set_compute_unit_limit(cu_limit).hex()
        for tier_name, price in (
            ("economy", econ_price),
            ("standard", std_price),
            ("fast", fast_price),
            ("turbo", turbo_price),
        ):
            total_lamports = calculate_total_fee_lamports(cu_limit, price, num_signatures=num_signatures)
            prio_lamports = total_lamports - (num_signatures * BASE_SIGNATURE_FEE_LAMPORTS)
            tiers[tier_name] = FeeTierRecommendation(
                tier=tier_name,
                cu_limit=cu_limit,
                cu_price_micro_lamports=int(price),
                priority_fee_lamports=int(prio_lamports),
                total_fee_lamports=int(total_lamports),
                total_fee_sol=round(total_lamports / LAMPORTS_PER_SOL, 9),
                set_cu_limit_ix_hex=set_limit_hex,
                set_cu_price_ix_hex=encode_set_compute_unit_price(price).hex(),
            )

        return FeeEstimate(
            rpc_url=self.rpc_url,
            writable_accounts=validated,
            slots_sampled=len(fees),
            nonzero_slots=len(nonzero),
            nonzero_ratio=nz_ratio,
            min_slot=min(slots) if slots else 0,
            max_slot=max(slots) if slots else 0,
            percentiles_all_slots=pct_all,
            percentiles_nonzero_slots=pct_nz,
            recent_20_slot_ema=ema_int,
            surge_multiplier=surge,
            congestion_regime=regime,
            recommendations=tiers,
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Estimate Solana per-writable-account priority-fee percentiles (micro-lamports/CU)."
    )
    parser.add_argument(
        "--accounts",
        "-a",
        nargs="*",
        default=["675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"],
        help="Writable Solana account pubkeys locked by your transaction.",
    )
    parser.add_argument("--cu-limit", type=int, default=200_000, help="Requested Compute Unit limit (default: 200000).")
    parser.add_argument("--rpc-url", type=str, default=DEFAULT_MAINNET_RPC, help="Solana JSON-RPC URL.")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON.")
    args = parser.parse_args(argv)

    client = SolanaPriorityFeeClient(rpc_url=args.rpc_url)
    est = client.estimate_writable_lock_fees(args.accounts, cu_limit=args.cu_limit)

    if args.json:
        print(json.dumps(est.to_dict(), indent=2))
        return 0

    print(f"Solana Per-Writable-Lock Priority Fee Estimate ({est.rpc_url})")
    print(f"  Writable Locks : {', '.join(est.writable_accounts) if est.writable_accounts else '(global)'}")
    print(f"  Slots Sampled  : {est.slots_sampled} (slots {est.min_slot}..{est.max_slot}, {est.nonzero_slots} non-zero)")
    print(f"  Regime         : {est.congestion_regime} (20-slot EMA={est.recent_20_slot_ema} uLamports/CU)")
    print("\n  Recommended Tiers:")
    for name, rec in est.recommendations.items():
        print(
            f"    [{name.upper():8s}] CUPrice={rec.cu_price_micro_lamports:>9,d} uLamports/CU | "
            f"TotalFee={rec.total_fee_lamports:>7,d} lamports ({rec.total_fee_sol:.9f} SOL)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
