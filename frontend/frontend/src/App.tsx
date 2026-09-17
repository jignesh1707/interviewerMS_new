import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AnswerResult,
  InterviewStatus,
  Question,
  createInterview,
  createInterviewWithFiles,
  finishInterview,
  getModels,
  speak,
  submitAudioAnswer,
  submitTextAnswer,
} from './api'

type Stage = 'setup' | 'interview' | 'report'

function useRecorder() {
  const [recording, setRecording] = useState(false)
  const [seconds, setSeconds] = useState(0)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const startedAtRef = useRef(0)
  const timerRef = useRef<number | null>(null)

  const stopTimer = () => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
  }

  const start = useCallback(async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    const recorder = new MediaRecorder(stream)
    chunksRef.current = []
    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) chunksRef.current.push(event.data)
    }
    recorder.start()
    recorderRef.current = recorder
    startedAtRef.current = Date.now()
    setSeconds(0)
    setRecording(true)
    timerRef.current = window.setInterval(() => setSeconds((Date.now() - startedAtRef.current) / 1000), 200)
  }, [])

  const stop = useCallback((): Promise<{ blob: Blob; duration: number }> => {
    return new Promise((resolve, reject) => {
      const recorder = recorderRef.current
      if (!recorder) {
        reject(new Error('No active recording'))
        return
      }
      recorder.onstop = () => {
        const duration = (Date.now() - startedAtRef.current) / 1000
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' })
        recorder.stream.getTracks().forEach((track) => track.stop())
        recorderRef.current = null
        stopTimer()
        setRecording(false)
        resolve({ blob, duration })
      }
      recorder.stop()
    })
  }, [])

  useEffect(() => () => stopTimer(), [])

  return { recording, seconds, start, stop }
}

export default function App() {
  const [apiKey, setApiKey] = useState(() => localStorage.getItem('vi_api_key') || 'dev-key-change-me')
  const [stage, setStage] = useState<Stage>('setup')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [providerStatus, setProviderStatus] = useState<any>(null)

  const [interview, setInterview] = useState<InterviewStatus | null>(null)
  const [questions, setQuestions] = useState<Question[]>([])
  const [currentIndex, setCurrentIndex] = useState(0)
  const [results, setResults] = useState<Record<number, AnswerResult>>({})
  const [report, setReport] = useState<any>(null)
  const [manualText, setManualText] = useState('')
  const recorder = useRecorder()

  useEffect(() => {
    localStorage.setItem('vi_api_key', apiKey)
  }, [apiKey])

  const refreshModels = async () => {
    try {
      setProviderStatus(await getModels(apiKey))
    } catch (err) {
      setProviderStatus(null)
    }
  }

  const currentQuestion = questions[currentIndex]
  const answeredCount = useMemo(() => Object.keys(results).length, [results])

  const handleCreate = async (form: {
    role: string
    candidateName: string
    resumeText: string
    jdText: string
    questionCount: number
    resumeFile: File | null
    jdFile: File | null
    callbackUrl: string
  }) => {
    setBusy(true)
    setError('')
    try {
      const config = {
        question_count: form.questionCount,
        analyze_per_answer: true,
        ask_followups: true,
      }
      let data
      if (form.resumeFile || form.jdFile) {
        const payload = new FormData()
        payload.append('role', form.role)
        if (form.candidateName) payload.append('candidate_name', form.candidateName)
        if (form.resumeText) payload.append('resume_text', form.resumeText)
        if (form.jdText) payload.append('jd_text', form.jdText)
        if (form.callbackUrl) payload.append('callback_url', form.callbackUrl)
        payload.append('config_json', JSON.stringify(config))
        if (form.resumeFile) payload.append('resume_file', form.resumeFile)
        if (form.jdFile) payload.append('jd_file', form.jdFile)
        data = await createInterviewWithFiles(apiKey, payload)
      } else {
        data = await createInterview(apiKey, {
          role: form.role,
          candidate_name: form.candidateName || undefined,
          resume_text: form.resumeText || undefined,
          jd_text: form.jdText || undefined,
          callback_url: form.callbackUrl || undefined,
          config,
        })
      }
      setInterview(data.interview)
      setQuestions(data.questions)
      setResults({})
      setCurrentIndex(0)
      setStage('interview')
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const submitRecording = async () => {
    if (!interview || !currentQuestion) return
    setBusy(true)
    setError('')
    try {
      const { blob, duration } = await recorder.stop()
      const result = await submitAudioAnswer(apiKey, interview.id, currentQuestion.index, blob, duration)
      setResults((prev) => ({ ...prev, [currentQuestion.index]: result }))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const submitTyped = async () => {
    if (!interview || !currentQuestion || !manualText.trim()) return
    setBusy(true)
    setError('')
    try {
      const result = await submitTextAnswer(apiKey, interview.id, currentQuestion.index, manualText)
      setResults((prev) => ({ ...prev, [currentQuestion.index]: result }))
      setManualText('')
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const playQuestion = async () => {
    if (!currentQuestion) return
    try {
      await speak(apiKey, currentQuestion.question)
    } catch (err) {
      setError((err as Error).message)
    }
  }

  const goNext = () => {
    if (currentIndex < questions.length - 1) setCurrentIndex(currentIndex + 1)
  }

  const handleFinish = async () => {
    if (!interview) return
    setBusy(true)
    setError('')
    try {
      const data = await finishInterview(apiKey, interview.id)
      setReport(data.report)
      setStage('report')
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const restart = () => {
    setStage('setup')
    setInterview(null)
    setQuestions([])
    setResults({})
    setReport(null)
    setError('')
  }

  return (
    <div className="app">
      <header>
        <h1>Voice Interviewer</h1>
        <p className="muted">
          STAR question generation, voice answers, transcript analytics and a scored improvement report.
        </p>
      </header>

      <section className="card">
        <div className="row">
          <label>
            API key
            <input value={apiKey} onChange={(event) => setApiKey(event.target.value)} />
          </label>
          <button className="ghost" onClick={refreshModels} type="button">
            Check router
          </button>
        </div>
        {providerStatus && (
          <div className="router-status">
            {Object.entries(providerStatus.providers || {}).map(([name, info]: [string, any]) => (
              <span key={name} className={info.available ? 'pill ok' : 'pill off'}>
                {name}: {info.available ? 'available' : info.configured ? 'circuit open' : 'no key'}
              </span>
            ))}
            <span className="pill">
              spend ${providerStatus.usage?.estimated_cost_usd ?? 0} / {providerStatus.usage?.calls ?? 0} calls
            </span>
          </div>
        )}
      </section>

      {error && <div className="error">{error}</div>}

      {stage === 'setup' && <SetupForm busy={busy} onCreate={handleCreate} />}

      {stage === 'interview' && interview && currentQuestion && (
        <section className="card">
          <div className="interview-head">
            <div>
              <strong>{interview.role}</strong>
              <div className="muted">
                Question {currentIndex + 1} of {questions.length} | answered {answeredCount}
              </div>
            </div>
            <span className={`pill ${currentQuestion.difficulty}`}>{currentQuestion.difficulty}</span>
          </div>

          <blockquote>{currentQuestion.question}</blockquote>
          <div className="muted small">
            Listen for: {currentQuestion.what_to_listen_for || 'STAR structure with measurable results'}
          </div>

          <div className="actions">
            <button className="ghost" onClick={playQuestion} type="button">
              Read aloud
            </button>
            {!recorder.recording ? (
              <button onClick={recorder.start} type="button">
                Start recording
              </button>
            ) : (
              <button className="danger" onClick={submitRecording} disabled={busy} type="button">
                Stop and analyse ({recorder.seconds.toFixed(1)}s)
              </button>
            )}
          </div>

          <div className="manual">
            <textarea
              placeholder="No microphone? Paste or type the answer here."
              value={manualText}
              onChange={(event) => setManualText(event.target.value)}
              rows={4}
            />
            <button className="ghost" onClick={submitTyped} disabled={busy || !manualText.trim()} type="button">
              Submit typed answer
            </button>
          </div>

          {results[currentQuestion.index] && (
            <AnswerFeedback
              result={results[currentQuestion.index]}
              isLast={currentIndex === questions.length - 1}
              busy={busy}
              onNext={goNext}
              onFinish={handleFinish}
            />
          )}

          {!results[currentQuestion.index] && currentIndex > 0 && (
            <button className="ghost" onClick={goNext} type="button">
              Skip to next
            </button>
          )}

          {currentIndex === questions.length - 1 && results[currentQuestion.index] && (
            <button onClick={handleFinish} disabled={busy} type="button">
              Generate final report
            </button>
          )}
        </section>
      )}

      {stage === 'report' && report && <ReportView report={report} onRestart={restart} />}
    </div>
  )
}

function AnswerFeedback({
  result,
  isLast,
  busy,
  onNext,
  onFinish,
}: {
  result: AnswerResult
  isLast: boolean
  busy: boolean
  onNext: () => void
  onFinish: () => void
}) {
  const analysis = result.analysis
  const scores = analysis?.scores || result.heuristic_scores || {}
  const dimensions = ['clarity', 'structure', 'depth', 'impact', 'overall']
  return (
    <div className="feedback">
      <h3>Answer analytics</h3>
      <div className="scores">
        {dimensions.map((key) => (
          <div key={key} className="score">
            <span>{key}</span>
            <strong>{Math.round(scores[key] ?? 0)}</strong>
          </div>
        ))}
      </div>
      <p className="muted small">
        {result.metrics?.word_count} words | {result.metrics?.words_per_minute ?? 'n/a'} wpm | fillers{' '}
        {result.metrics?.filler_total} | STAR coverage {result.metrics?.star_coverage}
      </p>
      {(analysis?.strengths?.length ?? 0) > 0 && (
        <p>
          <strong>Strengths:</strong> {analysis?.strengths.join('; ')}
        </p>
      )}
      {(analysis?.improvements?.length ?? 0) > 0 && (
        <p>
          <strong>Improve:</strong> {analysis?.improvements.join('; ')}
        </p>
      )}
      {result.followup && <p className="muted">Likely follow-up: {result.followup}</p>}
      {result.router?.analysis && (
        <p className="muted small">
          Routed to {result.router.analysis.provider}/{result.router.analysis.model} ({result.router.analysis.tier} tier,
          fallbacks {result.router.analysis.fallbacks})
        </p>
      )}
      <div className="actions">
        {!isLast ? (
          <button onClick={onNext} disabled={busy} type="button">
            Next question
          </button>
        ) : (
          <button onClick={onFinish} disabled={busy} type="button">
            Generate final report
          </button>
        )}
      </div>
    </div>
  )
}

function SetupForm({
  busy,
  onCreate,
}: {
  busy: boolean
  onCreate: (form: {
    role: string
    candidateName: string
    resumeText: string
    jdText: string
    questionCount: number
    resumeFile: File | null
    jdFile: File | null
    callbackUrl: string
  }) => void
}) {
  const [role, setRole] = useState('Backend Engineer')
  const [candidateName, setCandidateName] = useState('')
  const [resumeText, setResumeText] = useState('')
  const [jdText, setJdText] = useState('')
  const [questionCount, setQuestionCount] = useState(6)
  const [resumeFile, setResumeFile] = useState<File | null>(null)
  const [jdFile, setJdFile] = useState<File | null>(null)
  const [callbackUrl, setCallbackUrl] = useState('')

  return (
    <section className="card">
      <h2>Set up the interview</h2>
      <div className="grid">
        <label>
          Role
          <input value={role} onChange={(event) => setRole(event.target.value)} />
        </label>
        <label>
          Candidate name
          <input value={candidateName} onChange={(event) => setCandidateName(event.target.value)} />
        </label>
        <label>
          Questions
          <input
            type="number"
            min={3}
            max={15}
            value={questionCount}
            onChange={(event) => setQuestionCount(Number(event.target.value))}
          />
        </label>
        <label>
          Webhook callback URL
          <input value={callbackUrl} onChange={(event) => setCallbackUrl(event.target.value)} placeholder="optional" />
        </label>
      </div>
      <div className="grid">
        <label>
          Resume file (PDF/DOCX/TXT)
          <input type="file" accept=".pdf,.docx,.txt,.md" onChange={(event) => setResumeFile(event.target.files?.[0] || null)} />
        </label>
        <label>
          Job description file (PDF/DOCX/TXT)
          <input type="file" accept=".pdf,.docx,.txt,.md" onChange={(event) => setJdFile(event.target.files?.[0] || null)} />
        </label>
      </div>
      <label>
        Resume text
        <textarea rows={5} value={resumeText} onChange={(event) => setResumeText(event.target.value)} />
      </label>
      <label>
        Job description text
        <textarea rows={5} value={jdText} onChange={(event) => setJdText(event.target.value)} />
      </label>
      <button
        disabled={busy}
        onClick={() =>
          onCreate({ role, candidateName, resumeText, jdText, questionCount, resumeFile, jdFile, callbackUrl })
        }
        type="button"
      >
        {busy ? 'Generating STAR questions...' : 'Start interview'}
      </button>
    </section>
  )
}

function ReportView({ report, onRestart }: { report: any; onRestart: () => void }) {
  const dimensions = Object.entries(report.dimension_scores || {}) as [string, number][]
  return (
    <section className="card">
      <h2>Interview report</h2>
      <div className="headline">
        <div className="big-score">{report.overall_score}</div>
        <div>
          <strong>{report.readiness_level?.replace(/_/g, ' ')}</strong>
          <p className="muted">{report.summary}</p>
        </div>
      </div>

      <div className="scores">
        {dimensions.map(([key, value]) => (
          <div key={key} className="score">
            <span>{key.replace(/_/g, ' ')}</span>
            <strong>{value}</strong>
          </div>
        ))}
      </div>

      <div className="grid">
        <div>
          <h3>Strengths</h3>
          <ul>
            {(report.top_strengths || []).map((item: string, index: number) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
        <div>
          <h3>Critical gaps</h3>
          <ul>
            {(report.critical_gaps || []).map((item: string, index: number) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      </div>

      {report.improvement_plan && (
        <>
          <h3>Improvement plan</h3>
          {(['quick_wins', 'one_week_plan', 'thirty_day_plan', 'practice_prompts'] as const).map((key) =>
            report.improvement_plan[key]?.length ? (
              <div key={key}>
                <h4>{key.replace(/_/g, ' ')}</h4>
                <ul>
                  {report.improvement_plan[key].map((item: string, index: number) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null,
          )}
        </>
      )}

      <h3>Per-question</h3>
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Question</th>
            <th>Score</th>
          </tr>
        </thead>
        <tbody>
          {(report.per_question || []).map((item: any) => (
            <tr key={item.index}>
              <td>{item.index + 1}</td>
              <td>{item.question}</td>
              <td>{Math.round(item.overall)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {report.narrative && (
        <>
          <h3>Recruiter narrative</h3>
          <p>{report.narrative.narrative}</p>
          <p className="muted small">Recommendation: {report.narrative.recommendation}</p>
        </>
      )}

      <button className="ghost" onClick={onRestart} type="button">
        Start another interview
      </button>
    </section>
  )
}
