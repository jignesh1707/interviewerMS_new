// Interview countdown maths. Pure functions so they can be tested without a browser or React.
//
// The server answers with `seconds_remaining` (time until the hard deadline, measured on the server) and
// `grace_seconds` (the last part of that time). The nominal end the student is shown is the deadline minus the
// grace. Elapsed time is measured with a monotonic clock from the moment the answer arrived, so a wrong clock on
// the student's computer cannot change the countdown.

export const WARNING_SECONDS = 300
export const URGENT_SECONDS = 60

export type Phase = 'running' | 'warning' | 'urgent' | 'grace' | 'expired'

export type Snapshot = {
  secondsRemaining: number
  graceSeconds: number
  takenAt: number // monotonic ms (performance.now) when the server's figure was received
}

export type CountdownState = {
  phase: Phase
  msToEnd: number // to the nominal end, never below 0
  msToDeadline: number // to the hard deadline, never below 0
}

export function snapshotFrom(
  status: { seconds_remaining?: number | null; grace_seconds?: number | null },
  now: number,
): Snapshot | null {
  if (status.seconds_remaining === null || status.seconds_remaining === undefined) return null
  return { secondsRemaining: status.seconds_remaining, graceSeconds: status.grace_seconds ?? 0, takenAt: now }
}

export function countdownAt(snapshot: Snapshot, now: number): CountdownState {
  const elapsed = Math.max(0, now - snapshot.takenAt)
  const msToDeadline = Math.max(0, snapshot.secondsRemaining * 1000 - elapsed)
  const msToEnd = Math.max(0, msToDeadline - snapshot.graceSeconds * 1000)
  let phase: Phase
  if (msToDeadline <= 0) phase = 'expired'
  else if (msToEnd <= 0) phase = 'grace'
  else if (msToEnd <= URGENT_SECONDS * 1000) phase = 'urgent'
  else if (msToEnd <= WARNING_SECONDS * 1000) phase = 'warning'
  else phase = 'running'
  return { phase, msToEnd, msToDeadline }
}

export function formatClock(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  const ss = String(seconds).padStart(2, '0')
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, '0')}:${ss}` : `${minutes}:${ss}`
}
