//! ValidatorOrderRegistry (`CmNC6vKh7Tr99ucPJacvgibwVE4JgUhqunyQ3qeCm6Do`)
//! =======================================================================
//! Minimal, zero-heap Solana SBF program for:
//!   1. `RecordProbePulse` (`0x01`): Records intra-slot execution order of controlled
//!      contention bursts on a shared writable account lock and tallies priority-fee
//!      reorderings (`send_seq_curr < send_seq_prev` within the same `burst_id`).
//!   2. `PublishFeeCheckpoint` (`0x02`): Stores verified per-writable-account
//!      priority-fee percentiles (`p50`, `p75`, `p90` micro-lamports/CU) and
//!      Spearman rank correlation (`rho_bps`) on-chain.

#[allow(deprecated)]
use solana_program::{
    account_info::{next_account_info, AccountInfo},
    clock::Clock,
    declare_id,
    entrypoint::ProgramResult,
    msg,
    program_error::ProgramError,
    pubkey::Pubkey,
    sysvar::Sysvar,
};

declare_id!("CmNC6vKh7Tr99ucPJacvgibwVE4JgUhqunyQ3qeCm6Do");

#[cfg(not(feature = "no-entrypoint"))]
solana_program::entrypoint!(process_instruction);

pub const PROBE_STATE_MAGIC: &[u8; 4] = b"SVOR";
pub const CHECKPOINT_STATE_MAGIC: &[u8; 4] = b"SVFC";
pub const RING_CAPACITY: usize = 8;
pub const PROBE_HEADER_LEN: usize = 80;
pub const RING_ENTRY_LEN: usize = 16;
pub const PROBE_STATE_LEN: usize = PROBE_HEADER_LEN + RING_CAPACITY * RING_ENTRY_LEN; // 208 bytes
pub const CHECKPOINT_STATE_LEN: usize = 112;

pub const IX_RECORD_PROBE_PULSE: u8 = 0x01;
pub const IX_PUBLISH_FEE_CHECKPOINT: u8 = 0x02;

pub fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    instruction_data: &[u8],
) -> ProgramResult {
    let (&disc, payload) = instruction_data
        .split_first()
        .ok_or(ProgramError::InvalidInstructionData)?;

    let account_info_iter = &mut accounts.iter();
    let authority_info = next_account_info(account_info_iter)?;
    let state_info = next_account_info(account_info_iter)?;

    if !authority_info.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    if !state_info.is_writable {
        return Err(ProgramError::InvalidAccountData);
    }
    if state_info.owner != program_id {
        return Err(ProgramError::IncorrectProgramId);
    }

    let mut data = state_info.try_borrow_mut_data()?;

    match disc {
        IX_RECORD_PROBE_PULSE => {
            let slot = Clock::get().map(|c| c.slot).unwrap_or(0);
            let (pulse_count, reorder_count) =
                apply_record_probe_pulse(&mut data, authority_info.key.as_ref(), slot, payload)?;
            msg!(
                "SVOR pulse={} reorders={} slot={}",
                pulse_count,
                reorder_count,
                slot
            );
            Ok(())
        }
        IX_PUBLISH_FEE_CHECKPOINT => {
            let slot = apply_publish_fee_checkpoint(&mut data, authority_info.key.as_ref(), payload)?;
            msg!("SVFC checkpoint updated at slot={}", slot);
            Ok(())
        }
        _ => Err(ProgramError::InvalidInstructionData),
    }
}

/// Applies `RecordProbePulse` (`payload` = 18 bytes after discriminant `0x01`:
/// `burst_id: u64 LE, send_seq: u16 LE, cu_price_micro_lamports: u64 LE`).
pub fn apply_record_probe_pulse(
    buf: &mut [u8],
    signer_pubkey: &[u8; 32],
    slot: u64,
    payload: &[u8],
) -> Result<(u32, u32), ProgramError> {
    if buf.len() < PROBE_STATE_LEN {
        return Err(ProgramError::AccountDataTooSmall);
    }
    if payload.len() != 18 {
        return Err(ProgramError::InvalidInstructionData);
    }

    let burst_id = u64::from_le_bytes(payload[0..8].try_into().unwrap());
    let send_seq = u16::from_le_bytes(payload[8..10].try_into().unwrap());
    let cu_price = u64::from_le_bytes(payload[10..18].try_into().unwrap());

    if &buf[0..4] == &[0, 0, 0, 0] {
        buf[0..4].copy_from_slice(PROBE_STATE_MAGIC);
        buf[4] = 1;
        buf[5..8].fill(0);
        buf[8..40].copy_from_slice(signer_pubkey);
        buf[40..48].copy_from_slice(&burst_id.to_le_bytes());
        buf[48..56].copy_from_slice(&slot.to_le_bytes());
        buf[56..80].fill(0);
    } else if &buf[0..4] != PROBE_STATE_MAGIC {
        return Err(ProgramError::InvalidAccountData);
    }

    if &buf[8..40] != signer_pubkey {
        return Err(ProgramError::IllegalOwner);
    }

    let prev_burst = u64::from_le_bytes(buf[40..48].try_into().unwrap());
    let mut pulse_count = u32::from_le_bytes(buf[56..60].try_into().unwrap());
    let mut reorder_count = u32::from_le_bytes(buf[60..64].try_into().unwrap());
    let mut last_send_seq = u16::from_le_bytes(buf[64..66].try_into().unwrap());
    let ring_idx = u16::from_le_bytes(buf[66..68].try_into().unwrap());
    let mut max_price = u64::from_le_bytes(buf[72..80].try_into().unwrap());

    if prev_burst != burst_id {
        pulse_count = 0;
        last_send_seq = send_seq;
        max_price = cu_price;
    } else {
        if pulse_count > 0 && send_seq < last_send_seq {
            reorder_count = reorder_count.saturating_add(1);
        }
        last_send_seq = send_seq;
        if cu_price > max_price {
            max_price = cu_price;
        }
    }

    let landed_idx = (pulse_count & 0xFFFF) as u16;
    pulse_count = pulse_count.saturating_add(1);

    let entry_offset = PROBE_HEADER_LEN + ((ring_idx as usize) % RING_CAPACITY) * RING_ENTRY_LEN;
    buf[entry_offset..entry_offset + 4].copy_from_slice(&(slot as u32).to_le_bytes());
    buf[entry_offset + 4..entry_offset + 6].copy_from_slice(&send_seq.to_le_bytes());
    buf[entry_offset + 6..entry_offset + 8].copy_from_slice(&landed_idx.to_le_bytes());
    buf[entry_offset + 8..entry_offset + 16].copy_from_slice(&cu_price.to_le_bytes());

    let next_ring_idx = ((ring_idx as usize + 1) % RING_CAPACITY) as u16;

    buf[40..48].copy_from_slice(&burst_id.to_le_bytes());
    buf[48..56].copy_from_slice(&slot.to_le_bytes());
    buf[56..60].copy_from_slice(&pulse_count.to_le_bytes());
    buf[60..64].copy_from_slice(&reorder_count.to_le_bytes());
    buf[64..66].copy_from_slice(&last_send_seq.to_le_bytes());
    buf[66..68].copy_from_slice(&next_ring_idx.to_le_bytes());
    buf[68..72].fill(0);
    buf[72..80].copy_from_slice(&max_price.to_le_bytes());

    Ok((pulse_count, reorder_count))
}

/// Applies `PublishFeeCheckpoint` (`payload` = 68 bytes after discriminant `0x02`).
pub fn apply_publish_fee_checkpoint(
    buf: &mut [u8],
    signer_pubkey: &[u8; 32],
    payload: &[u8],
) -> Result<u64, ProgramError> {
    if buf.len() < CHECKPOINT_STATE_LEN {
        return Err(ProgramError::AccountDataTooSmall);
    }
    if payload.len() != 68 {
        return Err(ProgramError::InvalidInstructionData);
    }

    let target_writable: &[u8] = &payload[0..32];
    let slot = u64::from_le_bytes(payload[32..40].try_into().unwrap());
    let p50 = u64::from_le_bytes(payload[40..48].try_into().unwrap());
    let p75 = u64::from_le_bytes(payload[48..56].try_into().unwrap());
    let p90 = u64::from_le_bytes(payload[56..64].try_into().unwrap());
    let rho_bps = i16::from_le_bytes(payload[64..66].try_into().unwrap());
    let sample_size = u16::from_le_bytes(payload[66..68].try_into().unwrap());

    if !(-10_000..=10_000).contains(&rho_bps) || p50 > p75 || p75 > p90 {
        return Err(ProgramError::InvalidArgument);
    }

    let mut update_count: u32 = 0;
    if &buf[0..4] == &[0, 0, 0, 0] {
        buf[0..4].copy_from_slice(CHECKPOINT_STATE_MAGIC);
        buf[4] = 1;
        buf[5..8].fill(0);
        buf[8..40].copy_from_slice(signer_pubkey);
    } else if &buf[0..4] == CHECKPOINT_STATE_MAGIC {
        if &buf[8..40] != signer_pubkey {
            return Err(ProgramError::IllegalOwner);
        }
        let prev_slot = u64::from_le_bytes(buf[72..80].try_into().unwrap());
        if slot < prev_slot {
            return Err(ProgramError::InvalidArgument);
        }
        update_count = u32::from_le_bytes(buf[108..112].try_into().unwrap());
    } else {
        return Err(ProgramError::InvalidAccountData);
    }

    update_count = update_count.saturating_add(1);
    buf[40..72].copy_from_slice(target_writable);
    buf[72..80].copy_from_slice(&slot.to_le_bytes());
    buf[80..88].copy_from_slice(&p50.to_le_bytes());
    buf[88..96].copy_from_slice(&p75.to_le_bytes());
    buf[96..104].copy_from_slice(&p90.to_le_bytes());
    buf[104..106].copy_from_slice(&rho_bps.to_le_bytes());
    buf[106..108].copy_from_slice(&sample_size.to_le_bytes());
    buf[108..112].copy_from_slice(&update_count.to_le_bytes());

    Ok(slot)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn build_pulse_payload(burst_id: u64, send_seq: u16, cu_price: u64) -> [u8; 18] {
        let mut out = [0u8; 18];
        out[0..8].copy_from_slice(&burst_id.to_le_bytes());
        out[8..10].copy_from_slice(&send_seq.to_le_bytes());
        out[10..18].copy_from_slice(&cu_price.to_le_bytes());
        out
    }

    #[test]
    fn test_priority_reordering_tallied_on_shared_lock() {
        let mut buf = [0u8; PROBE_STATE_LEN];
        let auth = [7u8; 32];

        // Sent last (send_seq=5, 300_000 uLamports/CU), lands FIRST in block!
        let (cnt1, reord1) =
            apply_record_probe_pulse(&mut buf, &auth, 461_352_851, &build_pulse_payload(42, 5, 300_000))
                .unwrap();
        assert_eq!((cnt1, reord1), (1, 0));

        // Sent earlier (send_seq=2, 15_000 uLamports/CU), lands SECOND -> priority reorder detected!
        let (cnt2, reord2) =
            apply_record_probe_pulse(&mut buf, &auth, 461_352_851, &build_pulse_payload(42, 2, 15_000))
                .unwrap();
        assert_eq!((cnt2, reord2), (2, 1));

        // Sent earliest (send_seq=0, 1_000 uLamports/CU), lands THIRD -> second priority reorder!
        let (cnt3, reord3) =
            apply_record_probe_pulse(&mut buf, &auth, 461_352_851, &build_pulse_payload(42, 0, 1_000))
                .unwrap();
        assert_eq!((cnt3, reord3), (3, 2));

        let max_price = u64::from_le_bytes(buf[72..80].try_into().unwrap());
        assert_eq!(max_price, 300_000);
    }

    #[test]
    fn test_rejects_unauthorized_and_stale_checkpoint() {
        let mut buf = [0u8; CHECKPOINT_STATE_LEN];
        let auth = [1u8; 32];
        let attacker = [2u8; 32];
        let target = [9u8; 32];

        let mut p = [0u8; 68];
        p[0..32].copy_from_slice(&target);
        p[32..40].copy_from_slice(&500_000u64.to_le_bytes());
        p[40..48].copy_from_slice(&10_000u64.to_le_bytes());
        p[48..56].copy_from_slice(&25_000u64.to_le_bytes());
        p[56..64].copy_from_slice(&90_000u64.to_le_bytes());
        p[64..66].copy_from_slice(&(-2139i16).to_le_bytes());
        p[66..68].copy_from_slice(&150u16.to_le_bytes());

        assert_eq!(apply_publish_fee_checkpoint(&mut buf, &auth, &p).unwrap(), 500_000);

        // Unauthorized signer rejected
        assert_eq!(
            apply_publish_fee_checkpoint(&mut buf, &attacker, &p).unwrap_err(),
            ProgramError::IllegalOwner
        );

        // Stale slot regression rejected
        p[32..40].copy_from_slice(&499_999u64.to_le_bytes());
        assert_eq!(
            apply_publish_fee_checkpoint(&mut buf, &auth, &p).unwrap_err(),
            ProgramError::InvalidArgument
        );
    }
}
