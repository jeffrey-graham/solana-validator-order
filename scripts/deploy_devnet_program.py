#!/usr/bin/env python3
"""
Pure-Python Solana Devnet SBF Program Deployer & Verifier (`deploy_devnet_program.py`)
======================================================================================
Deploys a compiled Solana SBF `.so` binary (built via `cargo build-sbf` in CI or locally)
to our deterministic Program ID (`CmNC6vKh7Tr99ucPJacvgibwVE4JgUhqunyQ3qeCm6Do`) on
Solana Devnet (`https://api.devnet.solana.com`) without requiring a multi-GB local
Solana CLI installation on memory-constrained hosts.

Keys are read strictly from outside the repository (`~/.config/solana/`).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import struct
import sys
import time
from typing import Any, Dict, Optional, Sequence, Tuple

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from solana_validator_order.fee_estimator import (  # noqa: E402
    DEFAULT_DEVNET_RPC,
    _default_rpc_post,
    b58decode,
    b58encode,
    encode_set_compute_unit_price,
)
from solana_validator_order.program_verifier import _encode_compact_u16, verify_onchain_programs  # noqa: E402
from solana_validator_order.registry_client import (  # noqa: E402
    DEVNET_DEPLOYER_PUBKEY,
    VALIDATOR_ORDER_REGISTRY_PROGRAM_ID,
)

SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
BPF_LOADER_V2_ID = "BPFLoader2111111111111111111111111111111111"
COMPUTE_BUDGET_ID = "ComputeBudget111111111111111111111111111111"
CHUNK_SIZE = 880


def _load_keypair_outside_repo(path: str) -> Tuple[bytes, bytes, str]:
    abs_p = os.path.abspath(os.path.expanduser(path))
    if abs_p.startswith(REPO_ROOT + os.sep):
        raise PermissionError(f"Refusing to load keypair from inside repository: {abs_p}")
    with open(abs_p, "r", encoding="utf-8") as f:
        raw = bytes(json.load(f))
    if len(raw) != 64:
        raise ValueError(f"Expected 64-byte Solana keypair in {abs_p}, got {len(raw)}")
    return raw[:32], raw[32:], b58encode(raw[32:])


def _sign_ed25519(seed32: bytes, message: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.from_private_bytes(seed32)
    return priv.sign(message)


def _get_latest_blockhash(rpc_url: str) -> str:
    resp = _default_rpc_post(
        rpc_url,
        {"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "confirmed"}]},
    )
    return resp["result"]["value"]["blockhash"]


def _send_and_confirm(rpc_url: str, tx_bytes: bytes, label: str, timeout_s: int = 45) -> str:
    b64_tx = base64.b64encode(tx_bytes).decode("ascii")
    resp = _default_rpc_post(
        rpc_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [b64_tx, {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}],
        },
    )
    sig = resp["result"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = _default_rpc_post(
            rpc_url,
            {"jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses", "params": [[sig]]},
        )
        val = ((st.get("result") or {}).get("value") or [None])[0]
        if val is not None:
            if val.get("err") is not None:
                raise RuntimeError(f"Transaction {label} ({sig}) failed on-chain: {val['err']}")
            if val.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig
        time.sleep(1.2)
    raise TimeoutError(f"Timed out waiting for confirmation of {label} ({sig})")


def deploy_sbf_binary(
    so_path: str,
    *,
    rpc_url: str = DEFAULT_DEVNET_RPC,
    deployer_keypair_path: str = "~/.config/solana/devnet-deployer.json",
    program_keypair_path: str = "~/.config/solana/validator-order-program.json",
) -> Dict[str, Any]:
    with open(so_path, "rb") as f:
        elf_bytes = f.read()
    if elf_bytes[:4] != b"\x7fELF":
        raise ValueError(f"{so_path} is not a valid ELF binary")

    sha256_hex = hashlib.sha256(elf_bytes).hexdigest()
    dep_seed, dep_pub, dep_addr = _load_keypair_outside_repo(deployer_keypair_path)
    prog_seed, prog_pub, prog_addr = _load_keypair_outside_repo(program_keypair_path)

    rent_resp = _default_rpc_post(
        rpc_url,
        {"jsonrpc": "2.0", "id": 1, "method": "getMinimumBalanceForRentExemption", "params": [len(elf_bytes)]},
    )
    rent_lamports = int(rent_resp["result"])

    bal_resp = _default_rpc_post(
        rpc_url,
        {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [dep_addr]},
    )
    balance = int(bal_resp["result"]["value"])
    needed = rent_lamports + 5_000_000  # rent + ~0.005 SOL tx fees buffer
    if balance < needed:
        raise RuntimeError(
            f"Insufficient Devnet SOL on deployer {dep_addr}: have {balance / 1e9:.6f} SOL, "
            f"need >= {needed / 1e9:.6f} SOL. Request 0.5 Devnet SOL at https://faucet.solana.com first."
        )

    loader_bytes = b58decode(BPF_LOADER_V2_ID)
    sys_bytes = b58decode(SYSTEM_PROGRAM_ID)
    cb_bytes = b58decode(COMPUTE_BUDGET_ID)
    prio_ix = encode_set_compute_unit_price(10_000)

    # 1. SystemProgram::CreateAccount (2 signers: deployer + program_keypair)
    bh = b58decode(_get_latest_blockhash(rpc_url))
    create_ix = struct.pack("<IQQ32s", 0, rent_lamports, len(elf_bytes), loader_bytes)
    msg_create = (
        bytes([2, 0, 2])
        + _encode_compact_u16(4) + dep_pub + prog_pub + sys_bytes + cb_bytes
        + bh
        + _encode_compact_u16(2)
        + bytes([3, 0]) + _encode_compact_u16(len(prio_ix)) + prio_ix
        + bytes([2, 2, 0, 1]) + _encode_compact_u16(len(create_ix)) + create_ix
    )
    tx_create = (
        _encode_compact_u16(2)
        + _sign_ed25519(dep_seed, msg_create)
        + _sign_ed25519(prog_seed, msg_create)
        + msg_create
    )
    create_sig = _send_and_confirm(rpc_url, tx_create, "CreateAccount")
    print(f"[+] Created program account {prog_addr}: {create_sig}")

    # 2. BPFLoader2::Write chunks
    for offset in range(0, len(elf_bytes), CHUNK_SIZE):
        chunk = elf_bytes[offset : offset + CHUNK_SIZE]
        bh = b58decode(_get_latest_blockhash(rpc_url))
        # Bincode for LoaderInstruction::Write { offset: u32, bytes: Vec<u8> }: u32(0) + u32(offset) + u64(len) + chunk
        write_ix = struct.pack("<IIQ", 0, offset, len(chunk)) + chunk
        msg_write = (
            bytes([2, 0, 2])
            + _encode_compact_u16(4) + dep_pub + prog_pub + loader_bytes + cb_bytes
            + bh
            + _encode_compact_u16(2)
            + bytes([3, 0]) + _encode_compact_u16(len(prio_ix)) + prio_ix
            + bytes([2, 1, 1]) + _encode_compact_u16(len(write_ix)) + write_ix
        )
        tx_write = (
            _encode_compact_u16(2)
            + _sign_ed25519(dep_seed, msg_write)
            + _sign_ed25519(prog_seed, msg_write)
            + msg_write
        )
        _send_and_confirm(rpc_url, tx_write, f"Write@{offset}")

    # 3. BPFLoader2::Finalize
    bh = b58decode(_get_latest_blockhash(rpc_url))
    fin_ix = struct.pack("<I", 1)
    msg_fin = (
        bytes([2, 0, 3])
        + _encode_compact_u16(5)
        + dep_pub
        + prog_pub
        + loader_bytes
        + b58decode("SysvarRent111111111111111111111111111111111")
        + cb_bytes
        + bh
        + _encode_compact_u16(2)
        + bytes([4, 0]) + _encode_compact_u16(len(prio_ix)) + prio_ix
        + bytes([2, 2, 1, 3]) + _encode_compact_u16(len(fin_ix)) + fin_ix
    )
    tx_fin = (
        _encode_compact_u16(2)
        + _sign_ed25519(dep_seed, msg_fin)
        + _sign_ed25519(prog_seed, msg_fin)
        + msg_fin
    )
    fin_sig = _send_and_confirm(rpc_url, tx_fin, "Finalize")
    print(f"[+] Finalized executable program {prog_addr}: {fin_sig}")
    return {
        "program_id": prog_addr,
        "elf_bytes": len(elf_bytes),
        "elf_sha256": sha256_hex,
        "create_tx": create_sig,
        "finalize_tx": fin_sig,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify or deploy ValidatorOrderRegistry on Solana Devnet.")
    parser.add_argument("--verify", action="store_true", help="Verify Mainnet & Devnet Program IDs + SVM simulation.")
    parser.add_argument("--deploy-so", type=str, default="", help="Path to compiled validator_order_registry.so.")
    parser.add_argument("--rpc-url", type=str, default=DEFAULT_DEVNET_RPC)
    args = parser.parse_args(argv)

    if args.deploy_so:
        res = deploy_sbf_binary(args.deploy_so, rpc_url=args.rpc_url)
        print(json.dumps(res, indent=2))
        return 0

    report = verify_onchain_programs(devnet_rpc=args.rpc_url)
    print(json.dumps(report, indent=2))
    dep = report["devnet_deployer"]
    if dep["balance_lamports"] == 0:
        print(
            f"\n[i] To deploy {VALIDATOR_ORDER_REGISTRY_PROGRAM_ID} live on Devnet:\n"
            f"    1. Fund Devnet deployer {DEVNET_DEPLOYER_PUBKEY} with 0.5 SOL at https://faucet.solana.com\n"
            f"    2. Run: python3 scripts/deploy_devnet_program.py --deploy-so artifacts/validator_order_registry.so",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
