import { describe, expect, it } from 'vitest'
import { countdownAt, formatClock, snapshotFrom } from './countdown'

const SEC = 1000

describe('snapshotFrom', () => {
  it('is null when the server sent no deadline (plans disabled)', () => {
    expect(snapshotFrom({ seconds_remaining: null, grace_seconds: null }, 0)).toBeNull()
    expect(snapshotFrom({}, 0)).toBeNull()
  })

  it('captures the server-measured remaining time at the moment it arrived', () => {
    expect(snapshotFrom({ seconds_remaining: 960, grace_seconds: 60 }, 5000)).toEqual({
      secondsRemaining: 960,
      graceSeconds: 60,
      takenAt: 5000,
    })
  })

  it('treats a missing grace as zero', () => {
    expect(snapshotFrom({ seconds_remaining: 100 }, 0)?.graceSeconds).toBe(0)
  })
})

describe('countdownAt', () => {
  // A 15 minute interview with 60 s of grace: the server reports 960 s to the hard deadline.
  const snapshot = { secondsRemaining: 960, graceSeconds: 60, takenAt: 10_000 }

  it('counts down to the nominal end, not the hard deadline', () => {
    const state = countdownAt(snapshot, 10_000)
    expect(state.msToEnd).toBe(900 * SEC)
    expect(state.msToDeadline).toBe(960 * SEC)
    expect(state.phase).toBe('running')
  })

  it('uses elapsed time since the snapshot, not the wall clock', () => {
    expect(countdownAt(snapshot, 10_000 + 120 * SEC).msToEnd).toBe(780 * SEC)
  })

  it('warns at five minutes and goes urgent at one minute', () => {
    expect(countdownAt(snapshot, 10_000 + 599 * SEC).phase).toBe('running') // 301 s left
    expect(countdownAt(snapshot, 10_000 + 600 * SEC).phase).toBe('warning') // 300 s left
    expect(countdownAt(snapshot, 10_000 + 839 * SEC).phase).toBe('warning') // 61 s left
    expect(countdownAt(snapshot, 10_000 + 840 * SEC).phase).toBe('urgent') // 60 s left
  })

  it('enters the grace period when the nominal end passes', () => {
    const state = countdownAt(snapshot, 10_000 + 900 * SEC)
    expect(state.phase).toBe('grace')
    expect(state.msToEnd).toBe(0)
    expect(state.msToDeadline).toBe(60 * SEC)
  })

  it('is expired once the hard deadline passes, and never goes negative', () => {
    const state = countdownAt(snapshot, 10_000 + 961 * SEC)
    expect(state.phase).toBe('expired')
    expect(state.msToEnd).toBe(0)
    expect(state.msToDeadline).toBe(0)
  })

  it('goes straight from running to expired when there is no grace', () => {
    const noGrace = { secondsRemaining: 100, graceSeconds: 0, takenAt: 0 }
    expect(countdownAt(noGrace, 99 * SEC).phase).toBe('urgent')
    expect(countdownAt(noGrace, 100 * SEC).phase).toBe('expired')
  })

  it('starts already urgent when the interview is already nearly over', () => {
    expect(countdownAt({ secondsRemaining: 90, graceSeconds: 60, takenAt: 0 }, 0).phase).toBe('urgent')
  })

  it('tolerates a clock that moves backwards', () => {
    expect(countdownAt(snapshot, 5_000).msToDeadline).toBe(960 * SEC)
  })
})

describe('formatClock', () => {
  it('shows minutes and zero-padded seconds', () => {
    expect(formatClock(900 * SEC)).toBe('15:00')
    expect(formatClock(65 * SEC)).toBe('1:05')
    expect(formatClock(9 * SEC)).toBe('0:09')
  })

  it('rounds partial seconds up so it only shows 0:00 at the very end', () => {
    expect(formatClock(59_200)).toBe('1:00')
    expect(formatClock(1)).toBe('0:01')
    expect(formatClock(0)).toBe('0:00')
  })

  it('never shows a negative time', () => {
    expect(formatClock(-5000)).toBe('0:00')
  })

  it('adds hours when needed', () => {
    expect(formatClock(3725 * SEC)).toBe('1:02:05')
  })
})
