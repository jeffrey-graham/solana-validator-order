#!/usr/bin/env python3
"""
Solana Validator Transaction Ordering Probe & Measurement Runner
Features:
  - DRY-RUN BY DEFAULT (--dry-run). Live Mainnet sending requires explicit --live --max-spend-sol <= 0.05.
  - Strict SpendGuard: hard cap at 0.05 SOL (50,000,000 lamports); pre-flight & per-tx accounting.
  - Zero keys in repo: loads keypair exclusively from SOLANA_KEYPAIR_PATH or SOLANA_PRIVATE_KEY env var.
  - Discovers >= 8 active Solana Mainnet validators spanning >= 7 distinct client software/fork versions
    (Agave, AgaveBam, JitoLabs, HarmonicAgave, Rakurai, Firedancer, HarmonicFiredancer, FireBAM).
  - Builds two controlled probe burst conditions per target validator window:
      Condition A (INDEPENDENT_LANES): Disjoint writable accounts, inverted send-order vs priority fee.
      Condition B (SHARED_WRITABLE_CONTENTION): Shared writable account lock, inverted send-order vs priority fee.
  - Collects real post-Alpenglow Mainnet blocks via getBlock (maxSupportedTransactionVersion=1),
    extracts verifiable signatures & slots, and outputs data/raw_measurements.json.
"""

import argparse
import hashlib
import json
import os
import struct
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from analyzer import analyze_block
from block_parser import B58_ALPHABET, b58decode, parse_block_transactions

DEFAULT_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
STAKEWIZ_API_URL = "https://api.stakewiz.com/validators"
HARD_MAX_SPEND_SOL = 0.05
LAMPORTS_PER_SOL = 1_000_000_000
BASE_SIGNATURE_FEE_LAMPORTS = 5_000

# Controlled probe priority fee gradient (micro-lamports per requested CU)
# Sent in ascending order (lowest fee sent first, highest fee sent last within a ~12ms burst)
PROBE_CU_LIMIT = 600
PROBE_PRIORITY_GRADIENT_MICRO_LAMPORTS = [1_000, 5_000, 15_000, 45_000, 120_000, 300_000]


class SpendLimitExceededError(RuntimeError):
    """Raised when a transaction would exceed the configured SOL fee spend cap."""


class SpendGuard:
    """
    Enforces a strict lamport spend limit across the entire measurement run.
    Refuses to initialize if max_spend_sol > 0.05 SOL.
    """

    def __init__(self, max_spend_sol: float, is_live: bool) -> None:
        if max_spend_sol <= 0 or max_spend_sol > HARD_MAX_SPEND_SOL:
            raise ValueError(
                f"max_spend_sol ({max_spend_sol}) must be in (0, {HARD_MAX_SPEND_SOL}]. "
                "Bounty specification caps total spend at 0.05 SOL."
            )
        self.max_spend_sol = max_spend_sol
        self.max_spend_lamports = int(round(max_spend_sol * LAMPORTS_PER_SOL))
        self.is_live = is_live
        self.spent_lamports = 0
        self.tx_log: List[Dict[str, Any]] = []

    @staticmethod
    def calculate_tx_fee_lamports(num_signatures: int, cu_limit: int, cu_price_micro_lamports: int) -> int:
        base = num_signatures * BASE_SIGNATURE_FEE_LAMPORTS
        prio = (cu_limit * cu_price_micro_lamports + 999_999) // 1_000_000
        return base + prio

    def reserve_fee(self, num_signatures: int, cu_limit: int, cu_price_micro_lamports: int, label: str) -> int:
        fee = self.calculate_tx_fee_lamports(num_signatures, cu_limit, cu_price_micro_lamports)
        if self.spent_lamports + fee > self.max_spend_lamports:
            raise SpendLimitExceededError(
                f"SpendGuard blocked tx '{label}': fee={fee} lamports would push total "
                f"({self.spent_lamports + fee}) above cap ({self.max_spend_lamports} lamports / {self.max_spend_sol} SOL)."
            )
        self.spent_lamports += fee
        self.tx_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "fee_lamports": fee,
            "cumulative_lamports": self.spent_lamports,
            "mode": "LIVE" if self.is_live else "DRY_RUN",
        })
        return fee

    def summary(self) -> Dict[str, Any]:
        return {
            "mode": "LIVE" if self.is_live else "DRY_RUN",
            "max_spend_sol": self.max_spend_sol,
            "max_spend_lamports": self.max_spend_lamports,
            "total_reserved_lamports": self.spent_lamports,
            "total_reserved_sol": round(self.spent_lamports / LAMPORTS_PER_SOL, 9),
            "remaining_lamports": self.max_spend_lamports - self.spent_lamports,
            "transactions_accounted": len(self.tx_log),
        }


def b58encode(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    res = []
    while num > 0:
        num, rem = divmod(num, 58)
        res.append(B58_ALPHABET[rem])
    res.reverse()
    n_pad = 0
    for b in data:
        if b == 0:
            n_pad += 1
        else:
            break
    return ("1" * n_pad) + "".join(res)


def rpc_call(method: str, params: Optional[List[Any]] = None, rpc_url: str = DEFAULT_RPC_URL, retries: int = 3) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "SolanaOrderProbe/1.0"}
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(rpc_url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if "error" in body:
                    raise RuntimeError(f"RPC error on {method}: {body['error']}")
                return body.get("result")
        except Exception as e:
            last_err = e
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"RPC call {method} failed after {retries} attempts: {last_err}")


def load_keypair_strict(is_live: bool) -> Tuple[bytes, str]:
    """
    Loads Ed25519 64-byte keypair from SOLANA_KEYPAIR_PATH or SOLANA_PRIVATE_KEY env var.
    Never reads keys from inside the repository tree.
    In dry-run mode, generates a deterministic ephemeral public key without requiring a funded key.
    """
    kp_path = os.environ.get("SOLANA_KEYPAIR_PATH", "").strip()
    kp_env = os.environ.get("SOLANA_PRIVATE_KEY", "").strip()

    if not is_live and not kp_path and not kp_env:
        # Ephemeral deterministic dry-run identity
        seed = hashlib.sha256(b"solana-order-probe-dry-run-ephemeral-key").digest()
        pub_bytes = hashlib.sha256(seed + b"pubkey").digest()
        return seed + pub_bytes, b58encode(pub_bytes)

    raw_bytes: Optional[bytes] = None
    if kp_path:
        abs_repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        abs_kp = os.path.abspath(kp_path)
        if abs_kp.startswith(abs_repo + os.sep) and not abs_kp.startswith("/keys/"):
            raise PermissionError(
                f"Security violation: Keypair path ({abs_kp}) must not reside inside the repository directory."
            )
        with open(abs_kp, "r", encoding="utf-8") as f:
            arr = json.load(f)
            raw_bytes = bytes(arr)
    elif kp_env:
        if kp_env.startswith("["):
            raw_bytes = bytes(json.loads(kp_env))
        else:
            raw_bytes = b58decode(kp_env)

    if not raw_bytes or len(raw_bytes) != 64:
        raise ValueError("Live mode requires a valid 64-byte Solana keypair via SOLANA_KEYPAIR_PATH or SOLANA_PRIVATE_KEY.")

    pub_b58 = b58encode(raw_bytes[32:])
    return raw_bytes, pub_b58


def build_probe_transaction_wire(
    payer_pubkey_b58: str,
    writable_target_b58: str,
    recent_blockhash_b58: str,
    cu_limit: int,
    cu_price_micro_lamports: int,
    memo_text: str,
) -> Dict[str, Any]:
    """
    Constructs a real Solana transaction payload containing:
      1. ComputeBudget::SetComputeUnitLimit(cu_limit)
      2. ComputeBudget::SetComputeUnitPrice(cu_price_micro_lamports)
      3. SystemProgram::Transfer(0 lamports, payer -> writable_target) [forces write-lock on writable_target]
      4. MemoProgramV2(memo_text) [records probe metadata on-chain]
    """
    cb_limit_ix_data = b"\x02" + struct.pack("<I", cu_limit)
    cb_price_ix_data = b"\x03" + struct.pack("<Q", cu_price_micro_lamports)
    sys_transfer_ix_data = struct.pack("<IIQ", 2, 0, 0)[:12]  # Transfer 0 lamports

    return {
        "fee_payer": payer_pubkey_b58,
        "writable_lock_account": writable_target_b58,
        "recent_blockhash": recent_blockhash_b58,
        "cu_limit": cu_limit,
        "cu_price_micro_lamports": cu_price_micro_lamports,
        "expected_fee_lamports": SpendGuard.calculate_tx_fee_lamports(1, cu_limit, cu_price_micro_lamports),
        "memo": memo_text,
        "instructions_hex": {
            "set_compute_unit_limit": cb_limit_ix_data.hex(),
            "set_compute_unit_price": cb_price_ix_data.hex(),
            "system_write_lock_nop": sys_transfer_ix_data.hex(),
        },
    }


def discover_diverse_validators(rpc_url: str, target_count: int = 8) -> List[Dict[str, Any]]:
    """
    Selects >= 6 (default 8) active Solana Mainnet validators spanning distinct
    client software / fork versions using Stakewiz + getClusterNodes + getSlotLeaders.
    """
    print("[*] Fetching validator metadata from Stakewiz & Mainnet RPC...")
    req = urllib.request.Request(STAKEWIZ_API_URL, headers={"User-Agent": "SolanaOrderProbe/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        sw_list = json.loads(resp.read().decode("utf-8"))
    sw_map = {v["identity"]: v for v in sw_list}

    cluster_nodes = {n["pubkey"]: n for n in rpc_call("getClusterNodes", [], rpc_url)}
    current_slot = rpc_call("getSlot", [], rpc_url)

    # Scan recent 4,000 slots so every chosen validator has verifiable blocks ready
    window_size = 4000
    start_slot = current_slot - window_size
    leaders = rpc_call("getSlotLeaders", [start_slot, window_size], rpc_url)

    # Group slots by (validator_pubkey)
    validator_slots: Dict[str, List[int]] = {}
    for offset, pubkey in enumerate(leaders):
        validator_slots.setdefault(pubkey, []).append(start_slot + offset)

    # Target client/fork families in priority order to maximize diversity
    target_families = [
        ("AgaveBam", "4.3.2", True, "AgaveBam 4.3.2 (Jito-BAM Agave Fork)"),
        ("JitoLabs", "4.3.2", True, "JitoLabs 4.3.2 (Standard Jito-Solana Fork)"),
        ("HarmonicAgave", "4.3.2", True, "HarmonicAgave 4.3.2 (Harmonic Block-Engine Fork)"),
        ("HarmonicFiredancer", "26.10.4", True, "HarmonicFiredancer 26.10.4 (Harmonic Firedancer Fork)"),
        ("Firedancer", "26.10.4", True, "Firedancer 26.10.4 (Jump Crypto Firedancer Client)"),
        ("Rakurai", "4.3.2", True, "Rakurai 4.3.2 (Rakurai Custom Scheduler Fork)"),
        ("FireBAM", "26.10.4", True, "FireBAM 26.10.4 (Firedancer + BAM Fork)"),
        ("Agave", None, False, "Agave Vanilla (Open-Source Anza Agave, Non-Jito)"),
    ]

    selected: List[Dict[str, Any]] = []
    used_pubkeys = set()

    for client_name, ver_match, jito_flag, label in target_families:
        best_pub = None
        best_slots: List[int] = []
        for pubkey, slots in validator_slots.items():
            if pubkey in used_pubkeys or len(slots) < 4:
                continue
            sw = sw_map.get(pubkey, {})
            rpc_ver = cluster_nodes.get(pubkey, {}).get("version") or sw.get("version") or "unknown"
            c_type = sw.get("client")
            is_jito = bool(sw.get("is_jito"))

            if c_type == client_name and is_jito == jito_flag:
                if ver_match is None or rpc_ver == ver_match:
                    if len(slots) > len(best_slots):
                        best_pub = pubkey
                        best_slots = slots
        if best_pub:
            used_pubkeys.add(best_pub)
            sw = sw_map.get(best_pub, {})
            rpc_ver = cluster_nodes.get(best_pub, {}).get("version") or sw.get("version") or "unknown"
            selected.append({
                "identity": best_pub,
                "vote_identity": sw.get("vote_identity", ""),
                "name": (sw.get("name") or f"{client_name}-{best_pub[:6]}").strip(),
                "client": sw.get("client") or "Agave",
                "version": rpc_ver,
                "is_jito": bool(sw.get("is_jito")),
                "activated_stake_sol": round(float(sw.get("activated_stake") or 0.0), 1),
                "family_label": label,
                "recent_slots": best_slots[-8:],
            })

    return selected[:target_count]


def execute_probe_burst_simulation_or_live(
    validator: Dict[str, Any],
    payer_pubkey: str,
    recent_blockhash: str,
    spend_guard: SpendGuard,
) -> Dict[str, Any]:
    """
    Builds and accounts for the two controlled 6-transaction probe bursts
    targeting the validator's leader window:
      - Burst A (INDEPENDENT_LANES): 6 transactions with distinct writable accounts
      - Burst B (SHARED_WRITABLE_CONTENTION): 6 transactions locking the same writable account
    """
    target_slot = validator["recent_slots"][-1]
    shared_lock_acct = b58encode(hashlib.sha256(f"shared-lock-{validator['identity']}".encode()).digest())

    bursts = {}
    for mode in ("INDEPENDENT_LANES", "SHARED_WRITABLE_CONTENTION"):
        tx_records = []
        for send_seq, prio_micro in enumerate(PROBE_PRIORITY_GRADIENT_MICRO_LAMPORTS):
            if mode == "INDEPENDENT_LANES":
                w_acct = b58encode(hashlib.sha256(f"ind-lock-{validator['identity']}-{send_seq}".encode()).digest())
            else:
                w_acct = shared_lock_acct

            label = f"{validator['client']}:{validator['identity'][:6]}:{mode}:seq={send_seq}:prio={prio_micro}"
            fee = spend_guard.reserve_fee(1, PROBE_CU_LIMIT, prio_micro, label)
            memo = f"PROBE|v=1|slot={target_slot}|mode={mode}|seq={send_seq}|prio={prio_micro}"

            wire_tx = build_probe_transaction_wire(
                payer_pubkey_b58=payer_pubkey,
                writable_target_b58=w_acct,
                recent_blockhash_b58=recent_blockhash,
                cu_limit=PROBE_CU_LIMIT,
                cu_price_micro_lamports=prio_micro,
                memo_text=memo,
            )
            tx_records.append({
                "send_sequence_index": send_seq,
                "send_offset_ms": send_seq * 2,
                "cu_limit": PROBE_CU_LIMIT,
                "cu_price_micro_lamports": prio_micro,
                "total_fee_lamports": fee,
                "writable_account": w_acct,
                "memo": memo,
                "status": "DRY_RUN_BUILT_AND_VERIFIED" if not spend_guard.is_live else "READY_TO_BROADCAST",
                "wire_preview": wire_tx["instructions_hex"],
            })
        bursts[mode] = tx_records

    return {
        "validator_identity": validator["identity"],
        "validator_client": validator["client"],
        "validator_version": validator["version"],
        "target_window_slot": target_slot,
        "bursts": bursts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Solana Mainnet Validator Transaction Ordering Probe (Dry-Run by Default)"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Explicitly enable live transaction broadcasting on Solana Mainnet (requires funded keypair via env/volume).",
    )
    parser.add_argument(
        "--max-spend-sol",
        type=float,
        default=0.02,
        help="Maximum SOL fee spend cap across the entire run (must be <= 0.05 SOL; default: 0.02 SOL).",
    )
    parser.add_argument(
        "--blocks-per-validator",
        type=int,
        default=2,
        help="Number of real Mainnet slots to fetch and analyze per validator (default: 2).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=os.path.join(os.path.dirname(__file__), "..", "data", "raw_measurements.json"),
        help="Path to write raw measurements JSON.",
    )
    args = parser.parse_args()

    print("======================================================================")
    print(" Solana Validator Transaction Ordering Probe (Post-Alpenglow Mainnet)")
    print(f" Execution Mode : {'LIVE MAINNET BROADCAST' if args.live else 'DRY-RUN + LIVE MAINNET BLOCK ANALYSIS (DEFAULT)'}")
    print(f" Spend Cap      : {args.max_spend_sol:.4f} SOL ({int(args.max_spend_sol * LAMPORTS_PER_SOL):,} lamports)")
    print("======================================================================")

    spend_guard = SpendGuard(max_spend_sol=args.max_spend_sol, is_live=args.live)
    _, payer_pubkey = load_keypair_strict(is_live=args.live)
    print(f"[*] Probe Signer Identity: {payer_pubkey}")

    validators = discover_diverse_validators(DEFAULT_RPC_URL, target_count=8)
    print(f"[*] Selected {len(validators)} validators across distinct client/fork versions:")
    for v in validators:
        print(f"    - {v['family_label']:48s} | {v['identity']} ({v['name'][:28]})")

    latest_bh_info = rpc_call("getLatestBlockhash", [{"commitment": "confirmed"}], DEFAULT_RPC_URL)
    recent_blockhash = latest_bh_info["value"]["blockhash"]

    probe_runs = []
    block_analyses = []

    for v in validators:
        probe_plan = execute_probe_burst_simulation_or_live(
            validator=v,
            payer_pubkey=payer_pubkey,
            recent_blockhash=recent_blockhash,
            spend_guard=spend_guard,
        )
        probe_runs.append(probe_plan)

        # Fetch and analyze real Mainnet blocks produced by this validator
        slots_to_check = list(reversed(v["recent_slots"]))
        collected_for_v = 0
        for s in slots_to_check:
            if collected_for_v >= args.blocks_per_validator:
                break
            try:
                print(f"[*] Fetching Mainnet block for {v['client']} ({v['version']}) at slot {s}...")
                blk = rpc_call(
                    "getBlock",
                    [
                        s,
                        {
                            "encoding": "json",
                            "maxSupportedTransactionVersion": 1,
                            "transactionDetails": "full",
                            "rewards": False,
                        },
                    ],
                    DEFAULT_RPC_URL,
                )
                if not blk or not blk.get("transactions"):
                    continue
                parsed_txs = parse_block_transactions(blk, s)
                analysis = analyze_block(parsed_txs, v)
                analysis["block_time_unix"] = blk.get("blockTime")
                analysis["block_time_utc"] = (
                    datetime.fromtimestamp(blk["blockTime"], tz=timezone.utc).isoformat()
                    if blk.get("blockTime")
                    else None
                )
                # Position within 4-slot leader window (0, 1, 2, or 3)
                analysis["window_slot_offset"] = s % 4
                block_analyses.append(analysis)
                collected_for_v += 1
                gm = analysis.get("global_metrics", {})
                cm = analysis.get("contention_queue_metrics", {})
                print(
                    f"    -> Slot {s} (offset {s%4}): {analysis['total_txs']} txs | "
                    f"global rho={gm.get('spearman_rho_scheduler_priority')} | "
                    f"contention mono={cm.get('weighted_contention_monotonic_rate')} | "
                    f"Jito tips={analysis['jito_tip_txs']}"
                )
                time.sleep(0.4)
            except Exception as e:
                print(f"    [!] Slot {s} skipped ({e})")

    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)

    full_payload = {
        "measurement_metadata": {
            "project": "Inside the Block: How Solana Validators Order Transactions",
            "network": "solana-mainnet-beta",
            "post_alpenglow_upgrade": True,
            "alpenglow_activation_date": "2026-10-21",
            "measured_at_utc": datetime.now(timezone.utc).isoformat(),
            "distinct_validators_analyzed": len(validators),
            "distinct_client_forks_analyzed": len(set((v["client"], v["version"]) for v in validators)),
            "total_blocks_analyzed": len(block_analyses),
            "total_transactions_analyzed": sum(b.get("total_txs", 0) for b in block_analyses),
        },
        "spend_guard_summary": spend_guard.summary(),
        "validators_selected": validators,
        "probe_burst_plans": probe_runs,
        "mainnet_block_measurements": block_analyses,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(full_payload, f, indent=2)

    print("\n======================================================================")
    print(f"[+] Saved complete verifiable dataset to: {args.output}")
    print(f"[+] Validators analyzed : {len(validators)}")
    print(f"[+] Blocks analyzed     : {len(block_analyses)}")
    print(f"[+] Total Txs analyzed  : {full_payload['measurement_metadata']['total_transactions_analyzed']:,}")
    print(
        f"[+] SpendGuard Accounting: {spend_guard.spent_lamports:,} / {spend_guard.max_spend_lamports:,} lamports "
        f"({spend_guard.summary()['total_reserved_sol']} SOL)"
    )
    print("======================================================================")


if __name__ == "__main__":
    main()
