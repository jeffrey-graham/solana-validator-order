/**
 * @jeffrey-graham/solana-validator-order (v0.2.0)
 * Zero-dependency ESM SDK for Solana per-writable-account priority-fee percentiles,
 * ComputeBudget instruction encoding, Banking Stage Spearman/monotonicity statistics,
 * and ValidatorOrderRegistry (`CmNC6vKh7Tr99ucPJacvgibwVE4JgUhqunyQ3qeCm6Do`) codecs.
 */

export const COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111";
export const VALIDATOR_ORDER_REGISTRY_PROGRAM_ID = "CmNC6vKh7Tr99ucPJacvgibwVE4JgUhqunyQ3qeCm6Do";
export const DEFAULT_MAINNET_RPC = "https://solana-rpc.publicnode.com";
export const DEFAULT_DEVNET_RPC = "https://api.devnet.solana.com";
export const BASE_SIGNATURE_FEE_LAMPORTS = 5_000n;
export const PROBE_STATE_LEN = 208;
export const CHECKPOINT_STATE_LEN = 112;

const B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
const B58_MAP = new Map([...B58_ALPHABET].map((ch, idx) => [ch, BigInt(idx)]));

export function b58encode(bytes) {
  const u8 = bytes instanceof Uint8Array ? bytes : Uint8Array.from(bytes);
  let num = 0n;
  for (const b of u8) {
    num = (num << 8n) | BigInt(b);
  }
  const chars = [];
  while (num > 0n) {
    const rem = Number(num % 58n);
    num /= 58n;
    chars.push(B58_ALPHABET[rem]);
  }
  chars.reverse();
  let pad = 0;
  while (pad < u8.length && u8[pad] === 0) pad++;
  return "1".repeat(pad) + chars.join("");
}

export function b58decode(str) {
  if (typeof str !== "string" || str.length === 0) return new Uint8Array(0);
  let num = 0n;
  for (const ch of str) {
    const val = B58_MAP.get(ch);
    if (val === undefined) {
      throw new Error(`Invalid base58 character: ${JSON.stringify(ch)}`);
    }
    num = num * 58n + val;
  }
  const bytes = [];
  while (num > 0n) {
    bytes.push(Number(num & 0xffn));
    num >>= 8n;
  }
  bytes.reverse();
  let pad = 0;
  while (pad < str.length && str[pad] === "1") pad++;
  const out = new Uint8Array(pad + bytes.length);
  out.set(bytes, pad);
  return out;
}

export function validatePubkey(pubkey) {
  const cleaned = String(pubkey).trim();
  const decoded = b58decode(cleaned);
  if (decoded.length !== 32) {
    throw new Error(`Invalid Solana public key ${cleaned}: decoded ${decoded.length} bytes (expected 32)`);
  }
  return cleaned;
}

export function toHex(u8) {
  return Array.from(u8, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function encodeSetComputeUnitLimit(units) {
  const n = Number(units);
  if (!Number.isInteger(n) || n < 1 || n > 1_400_000) {
    throw new RangeError(`compute unit limit must be an integer in [1, 1_400_000], got ${units}`);
  }
  const out = new Uint8Array(5);
  const dv = new DataView(out.buffer);
  out[0] = 0x02;
  dv.setUint32(1, n, true);
  return out;
}

export function encodeSetComputeUnitPrice(microLamports) {
  const v = BigInt(microLamports);
  if (v < 0n || v > 0xffff_ffff_ffff_ffffn) {
    throw new RangeError(`microLamports must be a non-negative u64, got ${microLamports}`);
  }
  const out = new Uint8Array(9);
  const dv = new DataView(out.buffer);
  out[0] = 0x03;
  dv.setBigUint64(1, v, true);
  return out;
}

export function calculateTotalFeeLamports(cuLimit, cuPriceMicroLamports, numSignatures = 1) {
  const sigs = BigInt(numSignatures);
  if (sigs < 1n) throw new RangeError("numSignatures must be >= 1");
  const base = sigs * BASE_SIGNATURE_FEE_LAMPORTS;
  const prio = (BigInt(cuLimit) * BigInt(cuPriceMicroLamports) + 999_999n) / 1_000_000n;
  return Number(base + prio);
}

export function computePercentile(sortedValues, pct) {
  if (!sortedValues.length) return 0;
  if (pct <= 0) return Math.trunc(sortedValues[0]);
  if (pct >= 100) return Math.trunc(sortedValues[sortedValues.length - 1]);
  const pos = (pct / 100) * (sortedValues.length - 1);
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  if (lo === hi) return Math.trunc(sortedValues[lo]);
  const frac = pos - lo;
  return Math.round(sortedValues[lo] * (1 - frac) + sortedValues[hi] * frac);
}

export function encodeRecordProbePulse({ burstId, sendSeq, cuPriceMicroLamports }) {
  const bId = BigInt(burstId);
  const seq = Number(sendSeq);
  const price = BigInt(cuPriceMicroLamports);
  if (bId < 0n || bId > 0xffff_ffff_ffff_ffffn) throw new RangeError("burstId out of u64 range");
  if (!Number.isInteger(seq) || seq < 0 || seq > 0xffff) throw new RangeError("sendSeq out of u16 range");
  if (price < 0n || price > 0xffff_ffff_ffff_ffffn) throw new RangeError("cuPriceMicroLamports out of u64 range");
  const out = new Uint8Array(19);
  const dv = new DataView(out.buffer);
  out[0] = 0x01;
  dv.setBigUint64(1, bId, true);
  dv.setUint16(9, seq, true);
  dv.setBigUint64(11, price, true);
  return out;
}

export function encodePublishFeeCheckpoint({
  targetWritableAccount,
  slot,
  p50MicroLamports,
  p75MicroLamports,
  p90MicroLamports,
  spearmanRhoBps,
  sampleSize,
}) {
  const targetBytes = b58decode(validatePubkey(targetWritableAccount));
  if (spearmanRhoBps < -10_000 || spearmanRhoBps > 10_000) {
    throw new RangeError("spearmanRhoBps must be in [-10000, 10000]");
  }
  const out = new Uint8Array(69);
  const dv = new DataView(out.buffer);
  out[0] = 0x02;
  out.set(targetBytes, 1);
  dv.setBigUint64(33, BigInt(slot), true);
  dv.setBigUint64(41, BigInt(p50MicroLamports), true);
  dv.setBigUint64(49, BigInt(p75MicroLamports), true);
  dv.setBigUint64(57, BigInt(p90MicroLamports), true);
  dv.setInt16(65, Number(spearmanRhoBps), true);
  dv.setUint16(67, Number(sampleSize), true);
  return out;
}

export function decodeProbeState(bytes) {
  const u8 = bytes instanceof Uint8Array ? bytes : Uint8Array.from(bytes);
  if (u8.length < PROBE_STATE_LEN) {
    throw new RangeError(`ProbeState account too short: ${u8.length} < ${PROBE_STATE_LEN}`);
  }
  const magic = String.fromCharCode(u8[0], u8[1], u8[2], u8[3]);
  if (magic !== "SVOR") throw new Error(`Invalid ProbeState magic: ${magic}`);
  const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
  const entries = [];
  for (let i = 0; i < 8; i++) {
    const off = 80 + i * 16;
    const slotLow32 = dv.getUint32(off, true);
    const sendSeq = dv.getUint16(off + 4, true);
    const landedIdx = dv.getUint16(off + 6, true);
    const cuPriceMicroLamports = Number(dv.getBigUint64(off + 8, true));
    if (cuPriceMicroLamports > 0 || slotLow32 > 0) {
      entries.push({ slotLow32, sendSeq, landedIdx, cuPriceMicroLamports });
    }
  }
  return {
    version: u8[4],
    authority: b58encode(u8.subarray(8, 40)),
    activeBurstId: Number(dv.getBigUint64(40, true)),
    lastSlot: Number(dv.getBigUint64(48, true)),
    pulseCount: dv.getUint32(56, true),
    priorityReorderCount: dv.getUint32(60, true),
    lastSendSeq: dv.getUint16(64, true),
    ringWriteIdx: dv.getUint16(66, true),
    maxCuPriceMicroLamports: Number(dv.getBigUint64(72, true)),
    entries,
  };
}

function rankData(values) {
  const n = values.length;
  const indexed = values.map((v, i) => [i, v]).sort((a, b) => a[1] - b[1]);
  const ranks = new Array(n).fill(0);
  let i = 0;
  while (i < n) {
    let j = i;
    while (j + 1 < n && indexed[j + 1][1] === indexed[i][1]) j++;
    const avgRank = (i + j + 2) / 2;
    for (let k = i; k <= j; k++) ranks[indexed[k][0]] = avgRank;
    i = j + 1;
  }
  return ranks;
}

export function spearmanRho(x, y) {
  const n = x.length;
  if (n < 3 || y.length !== n) return 0;
  const rx = rankData(x);
  const ry = rankData(y);
  const mx = rx.reduce((a, b) => a + b, 0) / n;
  const my = ry.reduce((a, b) => a + b, 0) / n;
  let num = 0;
  let dx = 0;
  let dy = 0;
  for (let i = 0; i < n; i++) {
    const ax = rx[i] - mx;
    const ay = ry[i] - my;
    num += ax * ay;
    dx += ax * ax;
    dy += ay * ay;
  }
  if (dx <= 0 || dy <= 0) return 0;
  return num / Math.sqrt(dx * dy);
}

export function pairwiseMonotonicity(prioritiesInBlockOrder) {
  const n = prioritiesInBlockOrder.length;
  if (n < 2) return { rate: 0, concordant: 0, discordant: 0 };
  const step = Math.max(1, Math.floor(n / 400));
  const seq = [];
  for (let i = 0; i < n; i += step) seq.push(prioritiesInBlockOrder[i]);
  let concordant = 0;
  let discordant = 0;
  for (let i = 0; i < seq.length; i++) {
    for (let j = i + 1; j < seq.length; j++) {
      if (seq[i] > seq[j]) concordant++;
      else if (seq[i] < seq[j]) discordant++;
    }
  }
  const total = concordant + discordant;
  return {
    rate: total === 0 ? 0.5 : concordant / total,
    concordant,
    discordant,
  };
}

export class SolanaPriorityFeeClient {
  constructor(rpcUrl = DEFAULT_MAINNET_RPC, { fetchImpl = globalThis.fetch } = {}) {
    this.rpcUrl = rpcUrl;
    this.fetchImpl = fetchImpl;
  }

  async _rpc(method, params = []) {
    const resp = await this.fetchImpl(this.rpcUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    });
    if (!resp.ok) {
      throw new Error(`RPC HTTP ${resp.status} calling ${method} on ${this.rpcUrl}`);
    }
    const body = await resp.json();
    if (body.error) {
      throw new Error(`RPC error calling ${method}: ${JSON.stringify(body.error)}`);
    }
    return body.result;
  }

  async getRecentPrioritizationFees(writableAccounts = []) {
    const validated = writableAccounts.slice(0, 128).map(validatePubkey);
    const rows = (await this._rpc("getRecentPrioritizationFees", [validated])) || [];
    return [...rows].sort((a, b) => Number(a.slot) - Number(b.slot));
  }

  async estimateWritableLockFees(
    writableAccounts = [],
    { cuLimit = 200_000, numSignatures = 1, minFloorMicroLamports = 1_000 } = {},
  ) {
    if (!Number.isInteger(cuLimit) || cuLimit < 1 || cuLimit > 1_400_000) {
      throw new RangeError(`cuLimit must be in [1, 1_400_000], got ${cuLimit}`);
    }
    const validated = writableAccounts.slice(0, 128).map(validatePubkey);
    const rows = await this.getRecentPrioritizationFees(validated);
    const fees = rows.map((r) => Math.max(0, Math.trunc(Number(r.prioritizationFee || 0))));
    const slots = rows.map((r) => Number(r.slot || 0));
    const sortedAll = [...fees].sort((a, b) => a - b);
    const nonzero = sortedAll.filter((f) => f > 0);

    const pctKeys = [
      ["p25", 25],
      ["p50", 50],
      ["p75", 75],
      ["p90", 90],
      ["p95", 95],
    ];
    const percentilesAllSlots = Object.fromEntries(pctKeys.map(([k, p]) => [k, computePercentile(sortedAll, p)]));
    const percentilesNonzeroSlots = Object.fromEntries(pctKeys.map(([k, p]) => [k, computePercentile(nonzero, p)]));

    const recentTail = fees.slice(-20);
    let ema = 0;
    const alpha = 0.25;
    recentTail.forEach((val, idx) => {
      ema = idx === 0 ? val : alpha * val + (1 - alpha) * ema;
    });
    const recent20SlotEma = Math.round(ema);
    const baselineMed = Math.max(1, percentilesAllSlots.p50 || percentilesNonzeroSlots.p50 || 1);
    const surgeMultiplier = recentTail.length ? Number((ema / baselineMed).toFixed(3)) : 1;
    const nonzeroRatio = fees.length ? Number((nonzero.length / fees.length).toFixed(4)) : 0;

    let congestionRegime = "uncontested_low_floor";
    if (nonzeroRatio >= 0.5 || percentilesAllSlots.p75 >= 50_000) {
      congestionRegime = "hot_writable_lock_contention";
    } else if (nonzeroRatio >= 0.1 || percentilesNonzeroSlots.p75 >= 10_000) {
      congestionRegime = "intermittent_burst_contention";
    }

    const econPrice = Math.max(Math.floor(minFloorMicroLamports / 2), percentilesAllSlots.p25);
    const stdPrice = Math.max(minFloorMicroLamports, percentilesAllSlots.p50, percentilesNonzeroSlots.p25);
    const fastPrice = Math.max(stdPrice, percentilesAllSlots.p75, percentilesNonzeroSlots.p50, recent20SlotEma);
    const turboPrice = Math.max(fastPrice, percentilesAllSlots.p95, percentilesNonzeroSlots.p90);

    const setCuLimitIxHex = toHex(encodeSetComputeUnitLimit(cuLimit));
    const recommendations = {};
    for (const [tier, price] of [
      ["economy", econPrice],
      ["standard", stdPrice],
      ["fast", fastPrice],
      ["turbo", turboPrice],
    ]) {
      const totalFeeLamports = calculateTotalFeeLamports(cuLimit, price, numSignatures);
      const priorityFeeLamports = totalFeeLamports - numSignatures * 5_000;
      recommendations[tier] = {
        tier,
        cuLimit,
        cuPriceMicroLamports: price,
        priorityFeeLamports,
        totalFeeLamports,
        totalFeeSol: Number((totalFeeLamports / 1e9).toFixed(9)),
        setCuLimitIxHex,
        setCuPriceIxHex: toHex(encodeSetComputeUnitPrice(price)),
      };
    }

    return {
      rpcUrl: this.rpcUrl,
      writableAccounts: validated,
      slotsSampled: fees.length,
      nonzeroSlots: nonzero.length,
      nonzeroRatio,
      minSlot: slots.length ? Math.min(...slots) : 0,
      maxSlot: slots.length ? Math.max(...slots) : 0,
      percentilesAllSlots,
      percentilesNonzeroSlots,
      recent20SlotEma,
      surgeMultiplier,
      congestionRegime,
      recommendations,
    };
  }

  async verifyProgramExecutable(programId) {
    const validated = validatePubkey(programId);
    const res = await this._rpc("getAccountInfo", [validated, { encoding: "base64" }]);
    const val = res?.value || null;
    return {
      programId: validated,
      exists: val !== null,
      executable: Boolean(val?.executable),
      owner: val?.owner || null,
      lamports: val?.lamports || 0,
    };
  }
}
