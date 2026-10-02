import { describe, expect, it, vi } from 'vitest'
import { waitForReport, type ReportResult } from './api'

const processing: ReportResult = { interview_id: 'i1', status: 'processing', report: null }
const done: ReportResult = { interview_id: 'i1', status: 'completed', report: { overall_score: 76 } }
const noSleep = { sleep: () => Promise.resolve() }

describe('waitForReport', () => {
  it('returns at once when finish already carried the report', async () => {
    const fetchReport = vi.fn()
    expect(await waitForReport(done, fetchReport, noSleep)).toBe(done)
    expect(fetchReport).not.toHaveBeenCalled()
  })

  it('polls until the report is ready', async () => {
    const fetchReport = vi.fn().mockResolvedValueOnce(processing).mockResolvedValueOnce(done)
    expect(await waitForReport(processing, fetchReport, noSleep)).toBe(done)
    expect(fetchReport).toHaveBeenCalledTimes(2)
  })

  it('reports a failed build with its reason', async () => {
    const failed = { ...processing, status: 'failed', error: 'database went away' }
    await expect(waitForReport(processing, () => Promise.resolve(failed), noSleep)).rejects.toThrow(
      'database went away',
    )
  })

  it('gives up after the timeout', async () => {
    const fetchReport = () => Promise.resolve(processing)
    await expect(waitForReport(processing, fetchReport, { ...noSleep, timeoutMs: 0 })).rejects.toThrow(
      'taking longer',
    )
  })
})
