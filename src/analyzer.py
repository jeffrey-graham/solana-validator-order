#!/usr/bin/env python3
"""
Statistical Transaction Ordering Analyzer for Solana Validator Blocks & Probe Bursts
Computes:
  - Spearman's rank correlation (rho) and Kendall's tau between block index and priority metrics
  - Pairwise priority monotonicity / inversion rate
  - Separation of Jito tip transactions vs native scheduler transactions
  - Contending writable-account queue ordering (isolating prio-graph serialization from multi-thread execution noise)
  - Quartile / sub-slot priority gradient profiles
"""

import math
from collections import defaultdict
from typing import Any, Dict, List, Tuple


def _rank_data(values: List[float]) -> List[float]:
    """Computes average ranks for tied values (1-based)."""
    n = len(values)
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg_rank
        i = j + 1
    return ranks


def spearman_rho(x: List[float], y: List[float]) -> float:
    """Computes Spearman's rank correlation coefficient without external dependencies."""
    n = len(x)
    if n < 3:
        return 0.0
    rx = _rank_data(x)
    ry = _rank_data(y)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den_x = sum((rx[i] - mx) ** 2 for i in range(n))
    den_y = sum((ry[i] - my) ** 2 for i in range(n))
    if den_x <= 0 or den_y <= 0:
        return 0.0
    return num / math.sqrt(den_x * den_y)


def pairwise_monotonicity(priorities_in_block_order: List[float]) -> Tuple[float, int, int]:
    """
    For a sequence of priorities in block order (index 0, 1, ...),
    computes the fraction of strictly differing pairs (i < j) where priority[i] >= priority[j].
    1.00 = 100% strictly descending priority order; 0.50 = random noise; 0.00 = strictly ascending.
    """
    n = len(priorities_in_block_order)
    if n < 2:
        return 0.0, 0, 0
    # Sample if n > 400 to keep O(N^2) fast
    step = max(1, n // 400)
    seq = priorities_in_block_order[::step]
    concordant = 0
    discordant = 0
    m = len(seq)
    for i in range(m):
        pi = seq[i]
        for j in range(i + 1, m):
            pj = seq[j]
            if pi > pj:
                concordant += 1
            elif pi < pj:
                discordant += 1
    total_pairs = concordant + discordant
    if total_pairs == 0:
        return 0.5, 0, 0
    return concordant / total_pairs, concordant, discordant


def analyze_contending_queues(non_jito_txs: List[Dict[str, Any]], min_queue_len: int = 5) -> Dict[str, Any]:
    """
    Groups transactions by shared writable account (or fee payer) to measure ordering
    inside serialized account-lock queues where parallel worker thread jitter is eliminated.
    """
    queues: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for tx in non_jito_txs:
        writables = tx.get("writable_accounts_sample", [])
        if writables:
            for w in writables:
                queues[w].append(tx)
        elif tx.get("fee_payer"):
            queues[tx["fee_payer"]].append(tx)

    qualifying = []
    for acct, q_txs in queues.items():
        if len(q_txs) < min_queue_len:
            continue
        prios = [t["scheduler_priority_score"] for t in q_txs]
        if len(set(prios)) < 2:
            # All transactions in this queue paid the exact same priority fee per CU -> FIFO regime
            qualifying.append({
                "account": acct,
                "tx_count": len(q_txs),
                "regime": "uniform_fee_fifo",
                "spearman_rho": None,
                "monotonic_rate": None,
            })
        else:
            idxs = [float(t["block_index"]) for t in q_txs]
            rho = spearman_rho(idxs, prios)
            mono, conc, disc = pairwise_monotonicity(prios)
            qualifying.append({
                "account": acct,
                "tx_count": len(q_txs),
                "regime": "variable_fee_contention",
                "spearman_rho": round(rho, 4),
                "monotonic_rate": round(mono, 4),
                "concordant_pairs": conc,
                "discordant_pairs": disc,
            })

    var_queues = [q for q in qualifying if q["regime"] == "variable_fee_contention"]
    weighted_mono = 0.0
    weighted_rho = 0.0
    if var_queues:
        total_w = sum(q["tx_count"] for q in var_queues)
        weighted_mono = sum(q["monotonic_rate"] * q["tx_count"] for q in var_queues) / total_w
        weighted_rho = sum(q["spearman_rho"] * q["tx_count"] for q in var_queues) / total_w

    return {
        "total_contending_queues": len(qualifying),
        "variable_fee_queues": len(var_queues),
        "uniform_fee_fifo_queues": len(qualifying) - len(var_queues),
        "weighted_contention_rho": round(weighted_rho, 4),
        "weighted_contention_monotonic_rate": round(weighted_mono, 4),
        "top_queues": sorted(var_queues, key=lambda x: x["tx_count"], reverse=True)[:5],
    }


def analyze_block(parsed_txs: List[Dict[str, Any]], validator_meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Runs comprehensive statistical analysis on a single validator's block.
    Note: Negative Spearman rho between block_index (0..N-1) and priority_score
    means higher priority lands EARLIER (closer to index 0).
    """
    total_txs = len(parsed_txs)
    non_vote = [t for t in parsed_txs if not t["is_vote"]]
    jito_txs = [t for t in non_vote if t["is_jito_tip"]]
    scheduler_txs = [t for t in non_vote if not t["is_jito_tip"]]

    v_counts: Dict[str, int] = defaultdict(int)
    for t in non_vote:
        v_counts[t["version"]] += 1

    if len(scheduler_txs) < 5:
        return {
            "slot": parsed_txs[0]["slot"] if parsed_txs else 0,
            "validator": validator_meta,
            "total_txs": total_txs,
            "error": "Too few non-vote scheduler transactions",
        }

    idxs = [float(t["block_index"]) for t in scheduler_txs]
    prio_req = [float(t["scheduler_priority_score"]) for t in scheduler_txs]
    prio_only_req = [float(t["priority_fee_per_cu_req"]) for t in scheduler_txs]
    prio_used = [float(t["priority_fee_per_cu_used"]) for t in scheduler_txs]
    raw_fees = [float(t["total_fee_lamports"]) for t in scheduler_txs]

    rho_sched = spearman_rho(idxs, prio_req)
    rho_prio_req = spearman_rho(idxs, prio_only_req)
    rho_prio_used = spearman_rho(idxs, prio_used)
    rho_raw_fee = spearman_rho(idxs, raw_fees)

    mono_rate, conc, disc = pairwise_monotonicity(prio_req)

    # Quartile median priority analysis (Q1=top 25% of block, Q4=bottom 25% of block)
    q_size = max(1, len(scheduler_txs) // 4)
    quartiles = []
    for q in range(4):
        sub = prio_req[q * q_size : (q + 1) * q_size if q < 3 else len(scheduler_txs)]
        s_sorted = sorted(sub)
        med = s_sorted[len(s_sorted) // 2] if s_sorted else 0.0
        mean_val = sum(sub) / len(sub) if sub else 0.0
        quartiles.append({
            "quartile": f"Q{q+1}",
            "median_priority_micro_lamports_per_cu": round(med, 2),
            "mean_priority_micro_lamports_per_cu": round(mean_val, 2),
        })

    contention = analyze_contending_queues(scheduler_txs)

    # Determine empirical rule classification
    if rho_sched <= -0.55 or contention["weighted_contention_monotonic_rate"] >= 0.72:
        inferred_rule = "Strict Priority-per-Requested-CU (Reward / CU_limit) with Multi-Thread Packing"
    elif rho_sched <= -0.25 or contention["weighted_contention_monotonic_rate"] >= 0.60:
        inferred_rule = "Hybrid Priority-per-CU + Sub-Slot Batch / Thread Interleaving"
    elif rho_sched >= 0.15:
        inferred_rule = "Inverted / Tail-Loaded Batching (High-Fee Bundles/Txns Packed Late in Slot)"
    else:
        inferred_rule = "Arrival-Time FIFO / High Multi-Worker Noise Dominated"

    return {
        "slot": parsed_txs[0]["slot"],
        "validator": validator_meta,
        "total_txs": total_txs,
        "non_vote_txs": len(non_vote),
        "vote_txs": total_txs - len(non_vote),
        "jito_tip_txs": len(jito_txs),
        "native_scheduler_txs": len(scheduler_txs),
        "tx_version_breakdown": dict(v_counts),
        "global_metrics": {
            "spearman_rho_scheduler_priority": round(rho_sched, 4),
            "spearman_rho_priority_fee_per_cu_req": round(rho_prio_req, 4),
            "spearman_rho_priority_fee_per_cu_used": round(rho_prio_used, 4),
            "spearman_rho_raw_total_fee": round(rho_raw_fee, 4),
            "pairwise_descending_monotonic_rate": round(mono_rate, 4),
            "concordant_pairs": conc,
            "discordant_pairs": disc,
        },
        "quartile_profile": quartiles,
        "contention_queue_metrics": contention,
        "inferred_rule": inferred_rule,
        "verifiable_sample_signatures": [
            {
                "block_index": t["block_index"],
                "signature": t["signature"],
                "version": t["version"],
                "total_fee_lamports": t["total_fee_lamports"],
                "requested_cu": t["requested_cu"],
                "scheduler_priority_score": t["scheduler_priority_score"],
                "is_jito_tip": t["is_jito_tip"],
            }
            for t in (scheduler_txs[:5] + scheduler_txs[len(scheduler_txs)//2 : len(scheduler_txs)//2 + 3] + scheduler_txs[-4:])
        ],
    }
