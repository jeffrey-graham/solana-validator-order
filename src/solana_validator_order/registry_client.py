#!/usr/bin/env python3
"""
ValidatorOrderRegistry On-Chain Program Client & Binary Codec (`registry_client.py`)
====================================================================================
Byte-exact codec and PDA/account decoder for our custom Solana SBF program
(`programs/validator-order-registry/src/lib.rs`, Program ID:
`CmNC6vKh7Tr99ucPJacvgibwVE4JgUhqunyQ3qeCm6Do`).

On-Chain Instructions:
  - `0x01` `RecordProbePulse(burst_id: u64, send_seq: u16, cu_price_micro_lamports: u64)`
    Records intra-slot probe arrival order on a shared writable state lock and counts
    priority-fee reorderings (`send_seq_curr < send_seq_prev` within the same burst).
  - `0x02` `PublishFeeCheckpoint(...)`
    Publishes verified per-writable-account priority-fee percentiles (`p50/p75/p90`)
    and Spearman rank correlation (`rho_bps`) on-chain.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .fee_estimator import b58decode, b58encode, validate_pubkey

VALIDATOR_ORDER_REGISTRY_PROGRAM_ID = "CmNC6vKh7Tr99ucPJacvgibwVE4JgUhqunyQ3qeCm6Do"
DEVNET_DEPLOYER_PUBKEY = "3pcmw8VQooZne1LehScj8FoWjtRF9nJqNfD8h8c5VmmY"

PROBE_STATE_MAGIC = b"SVOR"
CHECKPOINT_STATE_MAGIC = b"SVFC"
RING_CAPACITY = 8

IX_RECORD_PROBE_PULSE = 0x01
IX_PUBLISH_FEE_CHECKPOINT = 0x02

# ProbeState layout (176 bytes total):
#   0..4   : magic = b"SVOR"
#   4      : version = 1 (u8)
#   5..8   : reserved ([u8; 3])
#   8..40  : authority ([u8; 32])
#   40..48 : active_burst_id (u64 LE)
#   48..56 : last_slot (u64 LE)
#   56..60 : pulse_count (u32 LE)
#   60..64 : priority_reorder_count (u32 LE)
#   64..66 : last_send_seq (u16 LE)
#   66..68 : ring_write_idx (u16 LE)
#   68..72 : pad (u32 LE)
#   72..80 : max_cu_price_micro_lamports (u64 LE)
#   80..208: 8 x 16-byte RingEntry(slot_low32: u32, send_seq: u16, landed_idx: u16, cu_price: u64)
PROBE_HEADER_STRUCT = struct.Struct("<4sB3s32sQQIIHHIQ")
RING_ENTRY_STRUCT = struct.Struct("<IHHQ")
PROBE_STATE_LEN = PROBE_HEADER_STRUCT.size + RING_CAPACITY * RING_ENTRY_STRUCT.size  # 80 + 128 = 208 bytes

# FeeCheckpointState layout (112 bytes total):
#   0..4   : magic = b"SVFC"
#   4      : version = 1 (u8)
#   5..8   : reserved ([u8; 3])
#   8..40  : authority ([u8; 32])
#   40..72 : target_writable_account ([u8; 32])
#   72..80 : slot (u64 LE)
#   80..88 : p50_micro_lamports (u64 LE)
#   88..96 : p75_micro_lamports (u64 LE)
#   96..104: p90_micro_lamports (u64 LE)
#  104..106: spearman_rho_bps (i16 LE, e.g. -2139 = -0.2139)
#  106..108: sample_size (u16 LE)
#  108..112: update_count (u32 LE)
CHECKPOINT_STATE_STRUCT = struct.Struct("<4sB3s32s32sQQQQhHI")
CHECKPOINT_STATE_LEN = CHECKPOINT_STATE_STRUCT.size  # 112 bytes


@dataclass(frozen=True)
class ProbeRingEntry:
    slot_low32: int
    send_seq: int
    landed_idx: int
    cu_price_micro_lamports: int


@dataclass(frozen=True)
class ProbeStateAccount:
    version: int
    authority: str
    active_burst_id: int
    last_slot: int
    pulse_count: int
    priority_reorder_count: int
    last_send_seq: int
    ring_write_idx: int
    max_cu_price_micro_lamports: int
    entries: List[ProbeRingEntry]


@dataclass(frozen=True)
class FeeCheckpointAccount:
    version: int
    authority: str
    target_writable_account: str
    slot: int
    p50_micro_lamports: int
    p75_micro_lamports: int
    p90_micro_lamports: int
    spearman_rho_bps: int
    sample_size: int
    update_count: int


def encode_record_probe_pulse(burst_id: int, send_seq: int, cu_price_micro_lamports: int) -> bytes:
    """Encodes `RecordProbePulse` (`0x01`, 19 bytes total)."""
    if not (0 <= burst_id < (1 << 64)):
        raise ValueError("burst_id must fit in u64")
    if not (0 <= send_seq < (1 << 16)):
        raise ValueError("send_seq must fit in u16")
    if not (0 <= cu_price_micro_lamports < (1 << 64)):
        raise ValueError("cu_price_micro_lamports must fit in u64")
    return struct.pack("<BQHQ", IX_RECORD_PROBE_PULSE, burst_id, send_seq, cu_price_micro_lamports)


def encode_publish_fee_checkpoint(
    target_writable_account: str,
    slot: int,
    p50_micro_lamports: int,
    p75_micro_lamports: int,
    p90_micro_lamports: int,
    spearman_rho_bps: int,
    sample_size: int,
) -> bytes:
    """Encodes `PublishFeeCheckpoint` (`0x02`, 69 bytes total)."""
    target_bytes = b58decode(validate_pubkey(target_writable_account))
    if not (-10_000 <= spearman_rho_bps <= 10_000):
        raise ValueError("spearman_rho_bps must be in [-10000, 10000]")
    if not (0 <= sample_size < (1 << 16)):
        raise ValueError("sample_size must fit in u16")
    return struct.pack(
        "<B32sQQQQhH",
        IX_PUBLISH_FEE_CHECKPOINT,
        target_bytes,
        slot,
        p50_micro_lamports,
        p75_micro_lamports,
        p90_micro_lamports,
        spearman_rho_bps,
        sample_size,
    )


def decode_probe_state(raw: bytes) -> ProbeStateAccount:
    """Decodes an on-chain `ProbeState` account (`208` bytes)."""
    if len(raw) < PROBE_STATE_LEN:
        raise ValueError(f"ProbeState account too short: {len(raw)} < {PROBE_STATE_LEN}")
    (
        magic,
        version,
        _res,
        auth_bytes,
        burst_id,
        last_slot,
        pulse_count,
        reorder_count,
        last_send_seq,
        ring_idx,
        _pad,
        max_price,
    ) = PROBE_HEADER_STRUCT.unpack_from(raw, 0)
    if magic != PROBE_STATE_MAGIC:
        raise ValueError(f"Invalid ProbeState magic: {magic!r}")
    entries: List[ProbeRingEntry] = []
    offset = PROBE_HEADER_STRUCT.size
    for _ in range(RING_CAPACITY):
        slot_low32, s_seq, l_idx, price = RING_ENTRY_STRUCT.unpack_from(raw, offset)
        offset += RING_ENTRY_STRUCT.size
        if price > 0 or slot_low32 > 0:
            entries.append(
                ProbeRingEntry(
                    slot_low32=slot_low32,
                    send_seq=s_seq,
                    landed_idx=l_idx,
                    cu_price_micro_lamports=price,
                )
            )
    return ProbeStateAccount(
        version=version,
        authority=b58encode(auth_bytes),
        active_burst_id=burst_id,
        last_slot=last_slot,
        pulse_count=pulse_count,
        priority_reorder_count=reorder_count,
        last_send_seq=last_send_seq,
        ring_write_idx=ring_idx,
        max_cu_price_micro_lamports=max_price,
        entries=entries,
    )


def decode_fee_checkpoint(raw: bytes) -> FeeCheckpointAccount:
    """Decodes an on-chain `FeeCheckpointState` account (`112` bytes)."""
    if len(raw) < CHECKPOINT_STATE_LEN:
        raise ValueError(f"FeeCheckpointState account too short: {len(raw)} < {CHECKPOINT_STATE_LEN}")
    (
        magic,
        version,
        _res,
        auth_bytes,
        target_bytes,
        slot,
        p50,
        p75,
        p90,
        rho_bps,
        sample_size,
        update_count,
    ) = CHECKPOINT_STATE_STRUCT.unpack_from(raw, 0)
    if magic != CHECKPOINT_STATE_MAGIC:
        raise ValueError(f"Invalid FeeCheckpointState magic: {magic!r}")
    return FeeCheckpointAccount(
        version=version,
        authority=b58encode(auth_bytes),
        target_writable_account=b58encode(target_bytes),
        slot=slot,
        p50_micro_lamports=p50,
        p75_micro_lamports=p75,
        p90_micro_lamports=p90,
        spearman_rho_bps=rho_bps,
        sample_size=sample_size,
        update_count=update_count,
    )


def simulate_local_probe_processor(
    state_buf: bytearray,
    payer_pubkey: str,
    slot: int,
    ix_data: bytes,
) -> Tuple[bytearray, Dict[str, int]]:
    """
    Reference state-transition mirror of `programs/validator-order-registry/src/lib.rs`
    (`process_record_probe_pulse`), used for deterministic client-side verification.
    """
    if len(state_buf) < PROBE_STATE_LEN:
        raise ValueError(f"state_buf must be >= {PROBE_STATE_LEN} bytes")
    if not ix_data or ix_data[0] != IX_RECORD_PROBE_PULSE or len(ix_data) != 19:
        raise ValueError("Invalid RecordProbePulse instruction bytes")
    _, burst_id, send_seq, cu_price = struct.unpack("<BQHQ", ix_data)
    payer_bytes = b58decode(validate_pubkey(payer_pubkey))

    magic = bytes(state_buf[0:4])
    if magic == b"\x00\x00\x00\x00":
        PROBE_HEADER_STRUCT.pack_into(
            state_buf,
            0,
            PROBE_STATE_MAGIC,
            1,
            b"\x00\x00\x00",
            payer_bytes,
            burst_id,
            slot,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    elif magic != PROBE_STATE_MAGIC:
        raise ValueError("Invalid account magic")

    decoded = PROBE_HEADER_STRUCT.unpack_from(state_buf, 0)
    auth_bytes = decoded[3]
    if auth_bytes != payer_bytes:
        raise PermissionError("Signer does not match ProbeState authority")

    prev_burst = decoded[4]
    pulse_count = decoded[6]
    reorder_count = decoded[7]
    last_send_seq = decoded[8]
    ring_idx = decoded[9]
    max_price = decoded[11]

    if prev_burst != burst_id:
        # Reset burst sequence tracking for a fresh burst window
        pulse_count = 0
        last_send_seq = send_seq
        max_price = cu_price
    else:
        if pulse_count > 0 and send_seq < last_send_seq:
            reorder_count += 1
        last_send_seq = send_seq
        if cu_price > max_price:
            max_price = cu_price

    landed_idx = pulse_count & 0xFFFF
    pulse_count += 1

    entry_offset = PROBE_HEADER_STRUCT.size + (ring_idx % RING_CAPACITY) * RING_ENTRY_STRUCT.size
    RING_ENTRY_STRUCT.pack_into(
        state_buf,
        entry_offset,
        slot & 0xFFFFFFFF,
        send_seq,
        landed_idx,
        cu_price,
    )
    next_ring_idx = (ring_idx + 1) % RING_CAPACITY

    PROBE_HEADER_STRUCT.pack_into(
        state_buf,
        0,
        PROBE_STATE_MAGIC,
        1,
        b"\x00\x00\x00",
        auth_bytes,
        burst_id,
        slot,
        pulse_count,
        reorder_count,
        last_send_seq,
        next_ring_idx,
        0,
        max_price,
    )
    return state_buf, {"pulse_count": pulse_count, "priority_reorder_count": reorder_count}
