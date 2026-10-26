#!/usr/bin/env python3
"""
Post-Alpenglow Solana Mainnet Block & Transaction Parser
Supports:
  - Legacy transactions (ComputeBudget111111111111111111111111111111 instructions)
  - v0 transactions (Address Lookup Tables + ComputeBudget instructions)
  - v1 transactions (Post-Alpenglow Oct 21, 2026 native message.transactionConfig)

Extracts:
  - Block index, signature, version ('legacy', 0, 1)
  - Total fee (lamports), base fee, priority fee (lamports)
  - Requested Compute Unit limit (CU_req) & Consumed Compute Units (CU_used)
  - Effective Priority Score (micro-lamports per requested CU, matching Agave's calculate_priority)
  - Writable accounts locked (including ALT writable lookups)
  - Jito / BAM tip detection (to separate bundle tips from native scheduler ordering)
"""

import struct
from typing import Any, Dict, List, Optional, Set, Tuple

COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
VOTE_PROGRAM = "Vote111111111111111111111111111111111111111"

# Canonical Jito Tip Payment Accounts (to isolate Jito bundles from scheduler ordering)
JITO_TIP_ACCOUNTS: Set[str] = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}

BUILTIN_PROGRAMS: Set[str] = {
    COMPUTE_BUDGET_PROGRAM,
    "11111111111111111111111111111111",
    "Config1111111111111111111111111111111111111",
    "Stake11111111111111111111111111111111111111",
    VOTE_PROGRAM,
    "AddressLookupTab1e1111111111111111111111111",
    "Ed25519SigVerify111111111111111111111111111",
    "KeccakSecp256k11111111111111111111111111111",
    "Secp256r1SigVerify1111111111111111111111111",
}

# Base58 alphabet for fast instruction data decoding
B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_MAP = {c: i for i, c in enumerate(B58_ALPHABET)}


def b58decode(s: str) -> bytes:
    num = 0
    for char in s:
        if char not in B58_MAP:
            return b""
        num = num * 58 + B58_MAP[char]
    res = bytearray()
    while num > 0:
        num, rem = divmod(num, 256)
        res.append(rem)
    res.reverse()
    n_pad = 0
    for char in s:
        if char == "1":
            n_pad += 1
        else:
            break
    return b"\x00" * n_pad + bytes(res)


def parse_compute_budget_ixs(
    account_keys: List[str], instructions: List[Dict[str, Any]]
) -> Tuple[Optional[int], Optional[int]]:
    """
    Parses ComputeBudget111... instructions from legacy/v0 transactions.
    Returns (requested_cu_limit, cu_price_micro_lamports).
    """
    cu_limit: Optional[int] = None
    cu_price: Optional[int] = None

    for ix in instructions:
        prog_idx = ix.get("programIdIndex")
        if prog_idx is None or prog_idx >= len(account_keys):
            continue
        if account_keys[prog_idx] != COMPUTE_BUDGET_PROGRAM:
            continue
        raw = b58decode(ix.get("data", ""))
        if not raw:
            continue
        disc = raw[0]
        if disc == 2 and len(raw) >= 5:
            # SetComputeUnitLimit(u32)
            cu_limit = struct.unpack("<I", raw[1:5])[0]
        elif disc == 3 and len(raw) >= 9:
            # SetComputeUnitPrice(u64) in micro-lamports per CU
            cu_price = struct.unpack("<Q", raw[1:9])[0]
    return cu_limit, cu_price


def estimate_default_cu_limit(
    account_keys: List[str], instructions: List[Dict[str, Any]]
) -> int:
    """
    Matches Agave's default compute budget allocation when SetComputeUnitLimit is omitted:
    3,000 CU per builtin instruction + 200,000 CU per non-builtin BPF instruction, capped at 1,400,000.
    """
    total = 0
    for ix in instructions:
        prog_idx = ix.get("programIdIndex")
        prog_id = account_keys[prog_idx] if (prog_idx is not None and prog_idx < len(account_keys)) else ""
        if prog_id in BUILTIN_PROGRAMS:
            total += 3_000
        else:
            total += 200_000
    return max(3_000, min(total, 1_400_000))


def extract_writable_accounts(msg: Dict[str, Any], meta: Dict[str, Any]) -> List[str]:
    """
    Returns all static + dynamic (ALT) writable account pubkeys locked by the transaction.
    """
    keys = msg.get("accountKeys", [])
    header = msg.get("header", {})
    num_req_sigs = header.get("numRequiredSignatures", 1)
    num_ro_signed = header.get("numReadonlySignedAccounts", 0)
    num_ro_unsigned = header.get("numReadonlyUnsignedAccounts", 0)

    writable: List[str] = []
    # Signed writable: indices [0 .. num_req_sigs - num_ro_signed - 1]
    num_writable_signed = max(0, num_req_sigs - num_ro_signed)
    for i in range(min(num_writable_signed, len(keys))):
        writable.append(keys[i])

    # Unsigned writable: indices [num_req_sigs .. len(keys) - num_ro_unsigned - 1]
    end_unsigned_writable = max(num_req_sigs, len(keys) - num_ro_unsigned)
    for i in range(num_req_sigs, min(end_unsigned_writable, len(keys))):
        writable.append(keys[i])

    # Loaded writable addresses from v0 Address Lookup Tables
    loaded = meta.get("loadedAddresses", {})
    if loaded and isinstance(loaded.get("writable"), list):
        writable.extend(loaded["writable"])

    return writable


def parse_block_transactions(block: Dict[str, Any], slot: int) -> List[Dict[str, Any]]:
    """
    Parses all transactions in a Solana Mainnet block (post-Alpenglow compatible).
    """
    parsed_txs: List[Dict[str, Any]] = []
    raw_txs = block.get("transactions", [])

    for idx, entry in enumerate(raw_txs):
        tx = entry.get("transaction", {})
        meta = entry.get("meta") or {}
        msg = tx.get("message", {})
        version = entry.get("version", "legacy")
        sigs = tx.get("signatures", [])
        sig = sigs[0] if sigs else f"unknown_{idx}"

        account_keys = msg.get("accountKeys", [])
        loaded = meta.get("loadedAddresses", {})
        all_keys = list(account_keys)
        if loaded:
            all_keys.extend(loaded.get("writable", []))
            all_keys.extend(loaded.get("readonly", []))

        is_vote = VOTE_PROGRAM in account_keys
        is_jito_tip = any(k in JITO_TIP_ACCOUNTS for k in all_keys)

        header = msg.get("header", {})
        num_sigs = max(1, header.get("numRequiredSignatures", len(sigs) or 1))
        total_fee = int(meta.get("fee", 5000 * num_sigs))
        base_fee = 5000 * num_sigs
        priority_fee_lamports = max(0, total_fee - base_fee)

        instructions = msg.get("instructions", [])
        tx_config = msg.get("transactionConfig")

        # Handle v1 native transactionConfig vs legacy/v0 ComputeBudget instructions
        if str(version) == "1" and isinstance(tx_config, dict):
            cu_limit = tx_config.get("computeUnitLimit") or estimate_default_cu_limit(account_keys, instructions)
            cfg_prio = tx_config.get("priorityFee")
            if cfg_prio is not None:
                priority_fee_lamports = int(cfg_prio)
            cu_price_micro = int((priority_fee_lamports * 1_000_000) / max(1, cu_limit))
            budget_source = "v1_transactionConfig"
        else:
            ix_cu_limit, ix_cu_price = parse_compute_budget_ixs(account_keys, instructions)
            cu_limit = ix_cu_limit if ix_cu_limit is not None else estimate_default_cu_limit(account_keys, instructions)
            if ix_cu_price is not None:
                cu_price_micro = ix_cu_price
            else:
                cu_price_micro = int((priority_fee_lamports * 1_000_000) / max(1, cu_limit))
            budget_source = "compute_budget_ix" if (ix_cu_limit is not None or ix_cu_price is not None) else "default_budget"

        cu_consumed = int(meta.get("computeUnitsConsumed") or 0)

        # Agave BankingStage / CentralScheduler priority formula:
        # reward / (requested_cu + 1), expressed in micro-lamports per CU
        scheduler_priority_score = (total_fee * 1_000_000) / max(1, cu_limit)
        priority_fee_per_cu_req = (priority_fee_lamports * 1_000_000) / max(1, cu_limit)
        priority_fee_per_cu_used = (priority_fee_lamports * 1_000_000) / max(1, cu_consumed) if cu_consumed > 0 else 0.0

        writable_accounts = extract_writable_accounts(msg, meta)
        fee_payer = account_keys[0] if account_keys else ""
        # Contention key: primary non-payer writable account (or payer if only payer is writable)
        non_payer_writables = [a for a in writable_accounts if a != fee_payer]

        parsed_txs.append({
            "slot": slot,
            "block_index": idx,
            "signature": sig,
            "version": str(version),
            "budget_source": budget_source,
            "is_vote": is_vote,
            "is_jito_tip": is_jito_tip,
            "succeeded": meta.get("err") is None,
            "num_signatures": num_sigs,
            "total_fee_lamports": total_fee,
            "base_fee_lamports": base_fee,
            "priority_fee_lamports": priority_fee_lamports,
            "requested_cu": int(cu_limit),
            "consumed_cu": cu_consumed,
            "cu_price_micro_lamports": int(cu_price_micro),
            "scheduler_priority_score": round(scheduler_priority_score, 2),
            "priority_fee_per_cu_req": round(priority_fee_per_cu_req, 2),
            "priority_fee_per_cu_used": round(priority_fee_per_cu_used, 2),
            "fee_payer": fee_payer,
            "writable_count": len(writable_accounts),
            "writable_accounts_sample": non_payer_writables[:5],
        })

    return parsed_txs
