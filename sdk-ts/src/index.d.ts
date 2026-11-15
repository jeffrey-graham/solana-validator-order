export declare const COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111";
export declare const VALIDATOR_ORDER_REGISTRY_PROGRAM_ID = "CmNC6vKh7Tr99ucPJacvgibwVE4JgUhqunyQ3qeCm6Do";
export declare const DEFAULT_MAINNET_RPC: string;
export declare const DEFAULT_DEVNET_RPC: string;
export declare const BASE_SIGNATURE_FEE_LAMPORTS: bigint;
export declare const PROBE_STATE_LEN = 208;
export declare const CHECKPOINT_STATE_LEN = 112;

export interface FeeTierRecommendation {
  tier: "economy" | "standard" | "fast" | "turbo";
  cuLimit: number;
  cuPriceMicroLamports: number;
  priorityFeeLamports: number;
  totalFeeLamports: number;
  totalFeeSol: number;
  setCuLimitIxHex: string;
  setCuPriceIxHex: string;
}

export interface FeeEstimate {
  rpcUrl: string;
  writableAccounts: string[];
  slotsSampled: number;
  nonzeroSlots: number;
  nonzeroRatio: number;
  minSlot: number;
  maxSlot: number;
  percentilesAllSlots: Record<"p25" | "p50" | "p75" | "p90" | "p95", number>;
  percentilesNonzeroSlots: Record<"p25" | "p50" | "p75" | "p90" | "p95", number>;
  recent20SlotEma: number;
  surgeMultiplier: number;
  congestionRegime: "uncontested_low_floor" | "intermittent_burst_contention" | "hot_writable_lock_contention";
  recommendations: Record<"economy" | "standard" | "fast" | "turbo", FeeTierRecommendation>;
}

export interface ProbeRingEntry {
  slotLow32: number;
  sendSeq: number;
  landedIdx: number;
  cuPriceMicroLamports: number;
}

export interface ProbeStateAccount {
  version: number;
  authority: string;
  activeBurstId: number;
  lastSlot: number;
  pulseCount: number;
  priorityReorderCount: number;
  lastSendSeq: number;
  ringWriteIdx: number;
  maxCuPriceMicroLamports: number;
  entries: ProbeRingEntry[];
}

export declare function b58encode(bytes: Uint8Array | number[]): string;
export declare function b58decode(str: string): Uint8Array;
export declare function validatePubkey(pubkey: string): string;
export declare function toHex(u8: Uint8Array): string;
export declare function encodeSetComputeUnitLimit(units: number): Uint8Array;
export declare function encodeSetComputeUnitPrice(microLamports: number | bigint): Uint8Array;
export declare function calculateTotalFeeLamports(
  cuLimit: number,
  cuPriceMicroLamports: number | bigint,
  numSignatures?: number
): number;
export declare function computePercentile(sortedValues: number[], pct: number): number;
export declare function encodeRecordProbePulse(params: {
  burstId: number | bigint;
  sendSeq: number;
  cuPriceMicroLamports: number | bigint;
}): Uint8Array;
export declare function encodePublishFeeCheckpoint(params: {
  targetWritableAccount: string;
  slot: number | bigint;
  p50MicroLamports: number | bigint;
  p75MicroLamports: number | bigint;
  p90MicroLamports: number | bigint;
  spearmanRhoBps: number;
  sampleSize: number;
}): Uint8Array;
export declare function decodeProbeState(bytes: Uint8Array | number[]): ProbeStateAccount;
export declare function spearmanRho(x: number[], y: number[]): number;
export declare function pairwiseMonotonicity(prioritiesInBlockOrder: number[]): {
  rate: number;
  concordant: number;
  discordant: number;
};

export declare class SolanaPriorityFeeClient {
  rpcUrl: string;
  constructor(rpcUrl?: string, options?: { fetchImpl?: typeof fetch });
  getRecentPrioritizationFees(
    writableAccounts?: string[]
  ): Promise<Array<{ slot: number; prioritizationFee: number }>>;
  estimateWritableLockFees(
    writableAccounts?: string[],
    options?: { cuLimit?: number; numSignatures?: number; minFloorMicroLamports?: number }
  ): Promise<FeeEstimate>;
  verifyProgramExecutable(programId: string): Promise<{
    programId: string;
    exists: boolean;
    executable: boolean;
    owner: string | null;
    lamports: number;
  }>;
}
