export type RunPauseStatus = 'waiting_approval' | 'waiting_input' | 'needs_reconciliation'

export function isPausedRunStatus(status: unknown): status is RunPauseStatus {
  return status === 'waiting_approval' || status === 'waiting_input' || status === 'needs_reconciliation'
}

export function shouldReplayPastPause(
  pause: RunPauseStatus, sequence: number, currentStatus: string, lastEventSeq: number | undefined,
) {
  return pause !== currentStatus || (lastEventSeq ?? 0) > sequence
}
