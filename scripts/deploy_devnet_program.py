#!/usr/bin/env python3
"""
Pure-Python Solana Devnet SBF Program Deployer & Verifier (`deploy_devnet_program.py`)
======================================================================================
Resumable, rate-limit-aware (`HTTP 429` backoff + batched signature confirmation)
pure-Python deployer for `ValidatorOrderRegistry`
(`CmNC6vKh7Tr99ucPJacvgibwVE4JgUhqunyQ3qeCm6Do`) on Solana Devnet via
`BPFLoaderUpgradeab1e11111111111111111111111` (Upgradeable Loader v3, SBFv1–SBFv3).

Keys are stored and read strictly outside the repository (`~/.config/solana/`).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import struct
import sys
import time
import urllib.error
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from solana_validator_order.fee_estimator import (  # noqa: E402
    DEFAULT_DEVNET_RPC,
    _default_rpc_post,
    b58decode,
    b58encode,
    encode_set_compute_unit_limit,
    encode_set_compute_unit_price,
)
from solana_validator_order.program_verifier import _encode_compact_u16, verify_onchain_programs  # noqa: E402
from solana_validator_order.registry_client import (  # noqa: E402
    CHECKPOINT_STATE_LEN,
    DEVNET_DEPLOYER_PUBKEY,
    PROBE_STATE_LEN,
    VALIDATOR_ORDER_REGISTRY_PROGRAM_ID,
    decode_fee_checkpoint,
    decode_probe_state,
    encode_publish_fee_checkpoint,
    encode_record_probe_pulse,
)

SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
BPF_LOADER_UPGRADEABLE_ID = "BPFLoaderUpgradeab1e11111111111111111111111"
COMPUTE_BUDGET_ID = "ComputeBudget111111111111111111111111111111"
SYSVAR_RENT_ID = "SysvarRent111111111111111111111111111111111"
SYSVAR_CLOCK_ID = "SysvarC1ock11111111111111111111111111111111"

BUFFER_METADATA_SIZE = 37
PROGRAM_ACCOUNT_SIZE = 36
PROGRAMDATA_METADATA_SIZE = 45
CHUNK_SIZE = 920

_ED25519_P = (1 << 255) - 19
_ED25519_D = (-121665 * pow(121666, _ED25519_P - 2, _ED25519_P)) % _ED25519_P


def _rpc(rpc_url: str, payload: Dict[str, Any], max_retries: int = 7) -> Dict[str, Any]:
    """Rate-limit-safe RPC wrapper with exponential backoff on HTTP 429 / 5xx."""
    for attempt in range(max_retries):
        try:
            time.sleep(0.35)
            return _default_rpc_post(rpc_url, payload, timeout=20)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 502, 503, 504) and attempt + 1 < max_retries:
                wait_s = min(15.0, 2.0 * (1.8 ** attempt))
                time.sleep(wait_s)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 < max_retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"Exhausted retries calling {payload.get('method')} on {rpc_url}")


def _is_on_ed25519_curve(b32: bytes) -> bool:
    if len(b32) != 32:
        return False
    y = int.from_bytes(b32, "little") & ((1 << 255) - 1)
    sign = (b32[31] >> 7) & 1
    if y >= _ED25519_P:
        return False
    y2 = (y * y) % _ED25519_P
    u = (y2 - 1) % _ED25519_P
    v = (_ED25519_D * y2 + 1) % _ED25519_P
    v3 = pow(v, 3, _ED25519_P)
    v7 = (v3 * pow(v, 4, _ED25519_P)) % _ED25519_P
    x = (u * v3 * pow(u * v7, (_ED25519_P - 5) // 8, _ED25519_P)) % _ED25519_P
    vx2 = (v * x * x) % _ED25519_P
    if vx2 == u:
        pass
    elif vx2 == (-u) % _ED25519_P:
        x = (x * pow(2, (_ED25519_P - 1) // 4, _ED25519_P)) % _ED25519_P
    else:
        return False
    if x == 0 and sign == 1:
        return False
    return True


def find_program_address(seeds: Sequence[bytes], program_id_b58: str) -> Tuple[bytes, str, int]:
    prog = b58decode(program_id_b58)
    for bump in range(255, -1, -1):
        cand = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not _is_on_ed25519_curve(cand):
            return cand, b58encode(cand), bump
    raise RuntimeError("Unable to derive off-curve PDA")


def _load_keypair_outside_repo(path: str) -> Tuple[bytes, bytes, str]:
    abs_p = os.path.abspath(os.path.expanduser(path))
    if abs_p.startswith(REPO_ROOT + os.sep):
        raise PermissionError(f"Refusing to load keypair from inside repository: {abs_p}")
    with open(abs_p, "r", encoding="utf-8") as f:
        raw = bytes(json.load(f))
    if len(raw) != 64:
        raise ValueError(f"Expected 64-byte Solana keypair in {abs_p}, got {len(raw)}")
    return raw[:32], raw[32:], b58encode(raw[32:])


def _generate_or_load_keypair_outside_repo(path: str, overwrite: bool = False) -> Tuple[bytes, bytes, str]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    abs_p = os.path.abspath(os.path.expanduser(path))
    if abs_p.startswith(REPO_ROOT + os.sep):
        raise PermissionError(f"Refusing to store keypair inside repository: {abs_p}")
    if os.path.exists(abs_p) and not overwrite:
        return _load_keypair_outside_repo(abs_p)
    os.makedirs(os.path.dirname(abs_p), mode=0o700, exist_ok=True)
    seed = secrets.token_bytes(32)
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    with open(abs_p, "w", encoding="utf-8") as f:
        json.dump(list(seed + pub), f)
    os.chmod(abs_p, 0o600)
    return seed, pub, b58encode(pub)


def _sign_ed25519(seed32: bytes, message: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(seed32).sign(message)


def _encode_ix(prog_idx: int, acct_indices: Sequence[int], data: bytes) -> bytes:
    return (
        bytes([prog_idx])
        + _encode_compact_u16(len(acct_indices))
        + bytes(acct_indices)
        + _encode_compact_u16(len(data))
        + data
    )


def _get_latest_blockhash(rpc_url: str) -> str:
    resp = _rpc(
        rpc_url,
        {"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "confirmed"}]},
    )
    return resp["result"]["value"]["blockhash"]


def _get_rent_exempt(rpc_url: str, size: int) -> int:
    resp = _rpc(
        rpc_url,
        {"jsonrpc": "2.0", "id": 1, "method": "getMinimumBalanceForRentExemption", "params": [size]},
    )
    return int(resp["result"])


def _send_tx(rpc_url: str, tx_bytes: bytes, label: str) -> str:
    b64_tx = base64.b64encode(tx_bytes).decode("ascii")
    resp = _rpc(
        rpc_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                b64_tx,
                {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"},
            ],
        },
    )
    return str(resp["result"])


def _confirm_batch(rpc_url: str, sigs_with_labels: Sequence[Tuple[str, str]], timeout_s: int = 75) -> None:
    pending = {sig: lbl for sig, lbl in sigs_with_labels}
    deadline = time.time() + timeout_s
    while pending and time.time() < deadline:
        time.sleep(2.2)
        sig_list = list(pending.keys())
        st = _rpc(
            rpc_url,
            {"jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses", "params": [sig_list]},
        )
        vals = (st.get("result") or {}).get("value") or []
        for sig, val in zip(sig_list, vals):
            if val is not None:
                if val.get("err") is not None:
                    raise RuntimeError(f"Transaction {pending[sig]} ({sig}) failed on-chain: {val['err']}")
                if val.get("confirmationStatus") in ("confirmed", "finalized"):
                    pending.pop(sig, None)
    if pending:
        raise TimeoutError(f"Timed out waiting for confirmation of: {pending}")


def _send_and_confirm(rpc_url: str, tx_bytes: bytes, label: str, timeout_s: int = 75) -> str:
    sig = _send_tx(rpc_url, tx_bytes, label)
    _confirm_batch(rpc_url, [(sig, label)], timeout_s=timeout_s)
    return sig


def deploy_sbf_binary(
    so_path: str,
    *,
    rpc_url: str = DEFAULT_DEVNET_RPC,
    deployer_keypair_path: str = "~/.config/solana/devnet-deployer.json",
    program_keypair_path: str = "~/.config/solana/validator-order-program.json",
    buffer_keypair_path: str = "~/.config/solana/validator-order-buffer.json",
) -> Dict[str, Any]:
    with open(so_path, "rb") as f:
        elf_bytes = f.read()
    if elf_bytes[:4] != b"\x7fELF":
        raise ValueError(f"{so_path} is not a valid ELF binary")

    sha256_hex = hashlib.sha256(elf_bytes).hexdigest()
    dep_seed, dep_pub, dep_addr = _load_keypair_outside_repo(deployer_keypair_path)
    prog_seed, prog_pub, prog_addr = _load_keypair_outside_repo(program_keypair_path)
    pda_bytes, pda_addr, _ = find_program_address([prog_pub], BPF_LOADER_UPGRADEABLE_ID)

    existing = _rpc(
        rpc_url,
        {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [prog_addr, {"encoding": "base64"}]},
    )["result"]["value"]
    if existing and existing.get("executable"):
        print(f"[i] Program {prog_addr} is already executable on {rpc_url}!")
        return {
            "program_id": prog_addr,
            "programdata_pda": pda_addr,
            "elf_bytes": len(elf_bytes),
            "elf_sha256": sha256_hex,
            "already_deployed": True,
        }

    buf_space = BUFFER_METADATA_SIZE + len(elf_bytes)
    prog_space = PROGRAM_ACCOUNT_SIZE
    progdata_space = PROGRAMDATA_METADATA_SIZE + len(elf_bytes)

    buf_rent = _get_rent_exempt(rpc_url, buf_space)
    prog_rent = _get_rent_exempt(rpc_url, prog_space)
    progdata_rent = _get_rent_exempt(rpc_url, progdata_space)

    loader_bytes = b58decode(BPF_LOADER_UPGRADEABLE_ID)
    sys_bytes = b58decode(SYSTEM_PROGRAM_ID)
    cb_bytes = b58decode(COMPUTE_BUDGET_ID)
    rent_bytes = b58decode(SYSVAR_RENT_ID)
    clock_bytes = b58decode(SYSVAR_CLOCK_ID)
    prio_ix = encode_set_compute_unit_price(10_000)

    # Check if an existing resumable Buffer account already exists on-chain
    buf_seed, buf_pub, buf_addr = _generate_or_load_keypair_outside_repo(buffer_keypair_path, overwrite=False)
    buf_info = _rpc(
        rpc_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [buf_addr, {"encoding": "base64", "commitment": "confirmed"}],
        },
    )["result"]["value"]

    buf_sig = "resumed-existing-buffer"
    onchain_buf_payload = b""
    if buf_info and buf_info.get("owner") == BPF_LOADER_UPGRADEABLE_ID:
        raw_buf = base64.b64decode(buf_info["data"][0])
        if len(raw_buf) == buf_space and raw_buf[5:37] == dep_pub:
            onchain_buf_payload = raw_buf[BUFFER_METADATA_SIZE:]
            print(f"[+] Resuming existing on-chain Buffer {buf_addr} ({buf_space} bytes)")
        else:
            buf_info = None

    if not buf_info:
        bal_resp = _rpc(rpc_url, {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [dep_addr]})
        balance = int(bal_resp["result"]["value"])
        needed = progdata_rent + prog_rent + 5_000_000
        if balance < needed:
            raise RuntimeError(
                f"Insufficient Devnet SOL on deployer {dep_addr}: have {balance / 1e9:.6f} SOL, "
                f"need >= {needed / 1e9:.6f} SOL. Request 0.5 Devnet SOL at https://faucet.solana.com first."
            )
        buf_seed, buf_pub, buf_addr = _generate_or_load_keypair_outside_repo(buffer_keypair_path, overwrite=True)
        bh = b58decode(_get_latest_blockhash(rpc_url))
        create_buf_ix = struct.pack("<IQQ32s", 0, buf_rent, buf_space, loader_bytes)
        init_buf_ix = struct.pack("<I", 0)
        msg_buf = (
            bytes([2, 0, 3])
            + _encode_compact_u16(5)
            + dep_pub
            + buf_pub
            + sys_bytes
            + loader_bytes
            + cb_bytes
            + bh
            + _encode_compact_u16(3)
            + _encode_ix(4, [], prio_ix)
            + _encode_ix(2, [0, 1], create_buf_ix)
            + _encode_ix(3, [1, 0], init_buf_ix)
        )
        tx_buf = (
            _encode_compact_u16(2)
            + _sign_ed25519(dep_seed, msg_buf)
            + _sign_ed25519(buf_seed, msg_buf)
            + msg_buf
        )
        buf_sig = _send_and_confirm(rpc_url, tx_buf, "CreateAndInitBuffer")
        print(f"[+] Initialized Buffer {buf_addr} ({buf_space} bytes): {buf_sig}")

    # Collect missing chunks and upload in rate-limit-friendly batches of 4
    total_chunks = (len(elf_bytes) + CHUNK_SIZE - 1) // CHUNK_SIZE
    missing_chunks: List[Tuple[int, int, bytes]] = []
    for idx, offset in enumerate(range(0, len(elf_bytes), CHUNK_SIZE), start=1):
        chunk = elf_bytes[offset : offset + CHUNK_SIZE]
        if onchain_buf_payload and onchain_buf_payload[offset : offset + len(chunk)] == chunk:
            continue
        missing_chunks.append((idx, offset, chunk))

    print(f"[i] Buffer {buf_addr}: {total_chunks - len(missing_chunks)}/{total_chunks} chunks already present; writing {len(missing_chunks)} chunks...")

    batch_size = 4
    for b_start in range(0, len(missing_chunks), batch_size):
        batch = missing_chunks[b_start : b_start + batch_size]
        bh = b58decode(_get_latest_blockhash(rpc_url))
        sent_batch: List[Tuple[str, str]] = []
        for idx, offset, chunk in batch:
            write_ix = struct.pack("<IIQ", 1, offset, len(chunk)) + chunk
            msg_write = (
                bytes([1, 0, 2])
                + _encode_compact_u16(4)
                + dep_pub
                + buf_pub
                + loader_bytes
                + cb_bytes
                + bh
                + _encode_compact_u16(2)
                + _encode_ix(3, [], prio_ix)
                + _encode_ix(2, [1, 0], write_ix)
            )
            tx_write = _encode_compact_u16(1) + _sign_ed25519(dep_seed, msg_write) + msg_write
            lbl = f"Write[{idx}/{total_chunks}]@{offset}"
            wsig = _send_tx(rpc_url, tx_write, lbl)
            sent_batch.append((wsig, lbl))
        _confirm_batch(rpc_url, sent_batch)
        last_sig, last_lbl = sent_batch[-1]
        print(f"    Confirmed batch through {last_lbl}: {last_sig}")

    # 3. DeployWithMaxDataLen(2)
    bh = b58decode(_get_latest_blockhash(rpc_url))
    cu_limit_ix = encode_set_compute_unit_limit(400_000)
    create_prog_ix = struct.pack("<IQQ32s", 0, prog_rent, prog_space, loader_bytes)
    deploy_ix = struct.pack("<IQ", 2, len(elf_bytes))
    msg_dep = (
        bytes([2, 0, 5])
        + _encode_compact_u16(9)
        + dep_pub
        + prog_pub
        + pda_bytes
        + buf_pub
        + rent_bytes
        + clock_bytes
        + sys_bytes
        + loader_bytes
        + cb_bytes
        + bh
        + _encode_compact_u16(4)
        + _encode_ix(8, [], cu_limit_ix)
        + _encode_ix(8, [], prio_ix)
        + _encode_ix(6, [0, 1], create_prog_ix)
        + _encode_ix(7, [0, 2, 1, 3, 4, 5, 6, 0], deploy_ix)
    )
    tx_dep = (
        _encode_compact_u16(2)
        + _sign_ed25519(dep_seed, msg_dep)
        + _sign_ed25519(prog_seed, msg_dep)
        + msg_dep
    )
    dep_sig = _send_and_confirm(rpc_url, tx_dep, "DeployWithMaxDataLen")
    print(f"[+] Deployed executable SBFv3 program {prog_addr} (ProgramData {pda_addr}): {dep_sig}")

    return {
        "program_id": prog_addr,
        "programdata_pda": pda_addr,
        "buffer_account": buf_addr,
        "elf_bytes": len(elf_bytes),
        "elf_sha256": sha256_hex,
        "buffer_init_tx": buf_sig,
        "deploy_tx": dep_sig,
    }


def seed_and_verify_onchain_state(
    *,
    rpc_url: str = DEFAULT_DEVNET_RPC,
    deployer_keypair_path: str = "~/.config/solana/devnet-deployer.json",
    probe_state_keypair_path: str = "~/.config/solana/validator-order-probe-state.json",
    checkpoint_state_keypair_path: str = "~/.config/solana/validator-order-checkpoint-state.json",
) -> Dict[str, Any]:
    dep_seed, dep_pub, dep_addr = _load_keypair_outside_repo(deployer_keypair_path)
    ps_seed, ps_pub, ps_addr = _generate_or_load_keypair_outside_repo(probe_state_keypair_path, overwrite=True)
    cs_seed, cs_pub, cs_addr = _generate_or_load_keypair_outside_repo(checkpoint_state_keypair_path, overwrite=True)

    prog_bytes = b58decode(VALIDATOR_ORDER_REGISTRY_PROGRAM_ID)
    sys_bytes = b58decode(SYSTEM_PROGRAM_ID)
    cb_bytes = b58decode(COMPUTE_BUDGET_ID)
    prio_ix = encode_set_compute_unit_price(10_000)

    ps_rent = _get_rent_exempt(rpc_url, PROBE_STATE_LEN)
    cs_rent = _get_rent_exempt(rpc_url, CHECKPOINT_STATE_LEN)

    slot_resp = _rpc(rpc_url, {"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []})
    cur_slot = int(slot_resp["result"])

    # Tx A: Create ProbeState + 2 inverted-arrival pulses (send_seq=5 @ 300k, then send_seq=1 @ 5k -> 1 priority reorder!)
    bh = b58decode(_get_latest_blockhash(rpc_url))
    create_ps_ix = struct.pack("<IQQ32s", 0, ps_rent, PROBE_STATE_LEN, prog_bytes)
    pulse_high = encode_record_probe_pulse(burst_id=1, send_seq=5, cu_price_micro_lamports=300_000)
    pulse_low = encode_record_probe_pulse(burst_id=1, send_seq=1, cu_price_micro_lamports=5_000)

    msg_ps = (
        bytes([2, 0, 3])
        + _encode_compact_u16(5)
        + dep_pub
        + ps_pub
        + sys_bytes
        + prog_bytes
        + cb_bytes
        + bh
        + _encode_compact_u16(4)
        + _encode_ix(4, [], prio_ix)
        + _encode_ix(2, [0, 1], create_ps_ix)
        + _encode_ix(3, [0, 1], pulse_high)
        + _encode_ix(3, [0, 1], pulse_low)
    )
    tx_ps = (
        _encode_compact_u16(2)
        + _sign_ed25519(dep_seed, msg_ps)
        + _sign_ed25519(ps_seed, msg_ps)
        + msg_ps
    )
    ps_sig = _send_and_confirm(rpc_url, tx_ps, "RecordProbePulseBurst")
    print(f"[+] Executed RecordProbePulse burst on {ps_addr}: {ps_sig}")

    # Tx B: Create FeeCheckpointState + PublishFeeCheckpoint for Raydium v4 lock
    bh = b58decode(_get_latest_blockhash(rpc_url))
    create_cs_ix = struct.pack("<IQQ32s", 0, cs_rent, CHECKPOINT_STATE_LEN, prog_bytes)
    ckpt_ix = encode_publish_fee_checkpoint(
        target_writable_account="675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
        slot=cur_slot,
        p50_micro_lamports=12_500,
        p75_micro_lamports=45_000,
        p90_micro_lamports=199_982,
        spearman_rho_bps=-2139,
        sample_size=150,
    )
    msg_cs = (
        bytes([2, 0, 3])
        + _encode_compact_u16(5)
        + dep_pub
        + cs_pub
        + sys_bytes
        + prog_bytes
        + cb_bytes
        + bh
        + _encode_compact_u16(3)
        + _encode_ix(4, [], prio_ix)
        + _encode_ix(2, [0, 1], create_cs_ix)
        + _encode_ix(3, [0, 1], ckpt_ix)
    )
    tx_cs = (
        _encode_compact_u16(2)
        + _sign_ed25519(dep_seed, msg_cs)
        + _sign_ed25519(cs_seed, msg_cs)
        + msg_cs
    )
    cs_sig = _send_and_confirm(rpc_url, tx_cs, "PublishFeeCheckpoint")
    print(f"[+] Executed PublishFeeCheckpoint on {cs_addr}: {cs_sig}")

    ps_info = _rpc(
        rpc_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [ps_addr, {"encoding": "base64", "commitment": "confirmed"}],
        },
    )["result"]["value"]
    cs_info = _rpc(
        rpc_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [cs_addr, {"encoding": "base64", "commitment": "confirmed"}],
        },
    )["result"]["value"]

    decoded_probe = asdict(decode_probe_state(base64.b64decode(ps_info["data"][0])))
    decoded_ckpt = asdict(decode_fee_checkpoint(base64.b64decode(cs_info["data"][0])))

    return {
        "authority": dep_addr,
        "probe_state_account": ps_addr,
        "probe_tx": ps_sig,
        "decoded_probe_state": decoded_probe,
        "checkpoint_state_account": cs_addr,
        "checkpoint_tx": cs_sig,
        "decoded_checkpoint_state": decoded_ckpt,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify or deploy ValidatorOrderRegistry on Solana Devnet.")
    parser.add_argument("--verify", action="store_true", help="Verify Mainnet & Devnet Program IDs + SVM simulation.")
    parser.add_argument(
        "--deploy-so",
        "--so-path",
        dest="deploy_so",
        type=str,
        default="",
        help="Path to compiled validator_order_registry.so.",
    )
    parser.add_argument(
        "--seed-state",
        action="store_true",
        help="Initialize & verify live SVOR ProbeState and SVFC FeeCheckpointState accounts on Devnet.",
    )
    parser.add_argument(
        "--payer-keypair",
        type=str,
        default="~/.config/solana/devnet-deployer.json",
        help="Path to Devnet payer keypair (strictly outside repository).",
    )
    parser.add_argument(
        "--program-keypair",
        type=str,
        default="~/.config/solana/validator-order-program.json",
        help="Path to deterministic program keypair (strictly outside repository).",
    )
    parser.add_argument("--rpc-url", type=str, default=DEFAULT_DEVNET_RPC)
    args = parser.parse_args(argv)

    out: Dict[str, Any] = {}
    if args.deploy_so:
        out["deployment"] = deploy_sbf_binary(
            args.deploy_so,
            rpc_url=args.rpc_url,
            deployer_keypair_path=args.payer_keypair,
            program_keypair_path=args.program_keypair,
        )
    if args.seed_state:
        out["seeded_state"] = seed_and_verify_onchain_state(
            rpc_url=args.rpc_url,
            deployer_keypair_path=args.payer_keypair,
        )
    if out:
        print(json.dumps(out, indent=2))
        return 0

    report = verify_onchain_programs(devnet_rpc=args.rpc_url)
    print(json.dumps(report, indent=2))
    dep = report["devnet_deployer"]
    if dep["balance_lamports"] == 0:
        print(
            f"\n[i] To deploy {VALIDATOR_ORDER_REGISTRY_PROGRAM_ID} live on Devnet:\n"
            f"    1. Fund Devnet deployer {DEVNET_DEPLOYER_PUBKEY} with 0.5 SOL at https://faucet.solana.com\n"
            f"    2. Run: python3 scripts/deploy_devnet_program.py --deploy-so artifacts/validator_order_registry.so --seed-state",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
