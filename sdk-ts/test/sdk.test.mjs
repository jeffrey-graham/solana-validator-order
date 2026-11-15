import assert from "node:assert/strict";
import test from "node:test";
import {
  SolanaPriorityFeeClient,
  VALIDATOR_ORDER_REGISTRY_PROGRAM_ID,
  b58decode,
  b58encode,
  calculateTotalFeeLamports,
  decodeProbeState,
  encodePublishFeeCheckpoint,
  encodeRecordProbePulse,
  encodeSetComputeUnitLimit,
  encodeSetComputeUnitPrice,
  pairwiseMonotonicity,
  spearmanRho,
  toHex,
} from "../src/index.mjs";

test("ComputeBudget binary encoders match Solana runtime wire format", () => {
  const limitIx = encodeSetComputeUnitLimit(180_000);
  assert.equal(limitIx.length, 5);
  assert.equal(toHex(limitIx), "0220bf0200");

  const priceIx = encodeSetComputeUnitPrice(250_000n);
  assert.equal(priceIx.length, 9);
  assert.equal(toHex(priceIx), "0390d0030000000000");

  // 5000 + ceil(180_000 * 250_000 / 1_000_000) = 5000 + 45000 = 50000 lamports
  assert.equal(calculateTotalFeeLamports(180_000, 250_000, 1), 50_000);
});

test("ValidatorOrderRegistry instruction & state binary codecs match Rust SBF layout", () => {
  assert.equal(VALIDATOR_ORDER_REGISTRY_PROGRAM_ID, "CmNC6vKh7Tr99ucPJacvgibwVE4JgUhqunyQ3qeCm6Do");
  const pulse = encodeRecordProbePulse({ burstId: 42n, sendSeq: 5, cuPriceMicroLamports: 300_000n });
  assert.equal(pulse.length, 19);
  assert.equal(pulse[0], 0x01);

  const cp = encodePublishFeeCheckpoint({
    targetWritableAccount: "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    slot: 461_352_851n,
    p50MicroLamports: 12_000n,
    p75MicroLamports: 45_000n,
    p90MicroLamports: 150_000n,
    spearmanRhoBps: -2139,
    sampleSize: 150,
  });
  assert.equal(cp.length, 69);
  assert.equal(cp[0], 0x02);

  // Construct synthetic 208-byte SVOR ProbeState buffer
  const buf = new Uint8Array(208);
  const dv = new DataView(buf.buffer);
  buf.set([0x53, 0x56, 0x4f, 0x52, 0x01], 0); // "SVOR" v1
  const authBytes = b58decode("3pcmw8VQooZne1LehScj8FoWjtRF9nJqNfD8h8c5VmmY");
  buf.set(authBytes, 8);
  dv.setBigUint64(40, 42n, true);
  dv.setBigUint64(48, 461_352_851n, true);
  dv.setUint32(56, 3, true); // pulseCount
  dv.setUint32(60, 2, true); // priorityReorderCount
  dv.setUint16(64, 0, true);
  dv.setUint16(66, 3, true);
  dv.setBigUint64(72, 300_000n, true);
  dv.setUint32(80, 461_352_851, true);
  dv.setUint16(84, 5, true);
  dv.setUint16(86, 0, true);
  dv.setBigUint64(88, 300_000n, true);

  const decoded = decodeProbeState(buf);
  assert.equal(decoded.authority, "3pcmw8VQooZne1LehScj8FoWjtRF9nJqNfD8h8c5VmmY");
  assert.equal(decoded.pulseCount, 3);
  assert.equal(decoded.priorityReorderCount, 2);
  assert.equal(decoded.maxCuPriceMicroLamports, 300_000);
  assert.equal(decoded.entries.length, 1);
  assert.equal(decoded.entries[0].sendSeq, 5);
});

test("SolanaPriorityFeeClient computes per-writable-lock percentiles and surge regimes", async () => {
  const mockRows = Array.from({ length: 100 }, (_, i) => ({
    slot: 469_000_000 + i,
    prioritizationFee: i < 80 ? 10_000 + i * 500 : 250_000 + (i - 80) * 10_000,
  }));

  const client = new SolanaPriorityFeeClient("https://mock.rpc", {
    fetchImpl: async () => ({
      ok: true,
      json: async () => ({ jsonrpc: "2.0", id: 1, result: mockRows }),
    }),
  });

  const est = await client.estimateWritableLockFees(
    ["675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"],
    { cuLimit: 150_000 },
  );

  assert.equal(est.slotsSampled, 100);
  assert.equal(est.congestionRegime, "hot_writable_lock_contention");
  assert.ok(est.surgeMultiplier > 2.0);
  assert.ok(est.recommendations.economy.cuPriceMicroLamports <= est.recommendations.standard.cuPriceMicroLamports);
  assert.ok(est.recommendations.standard.cuPriceMicroLamports <= est.recommendations.fast.cuPriceMicroLamports);
  assert.ok(est.recommendations.fast.cuPriceMicroLamports <= est.recommendations.turbo.cuPriceMicroLamports);
});

test("Spearman rho & pairwise monotonicity match Python reference implementation", () => {
  const idxs = [0, 1, 2, 3, 4];
  const prios = [500, 400, 300, 200, 100];
  assert.ok(Math.abs(spearmanRho(idxs, prios) - -1.0) < 1e-9);
  const mono = pairwiseMonotonicity(prios);
  assert.equal(mono.rate, 1.0);
  assert.equal(mono.concordant, 10);
  assert.equal(mono.discordant, 0);

  const pub = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8";
  assert.equal(b58encode(b58decode(pub)), pub);
});
