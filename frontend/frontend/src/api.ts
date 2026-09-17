const BASE = '/api/v1'

export type Question = {
  index: number
  category: string
  question: string
  star_focus: string
  difficulty: string
  what_to_listen_for: string
}

export type InterviewStatus = {
  id: string
  status: string
  role: string
  candidate_name: string | null
  question_count: number
  answered_count: number
  created_at: string
  updated_at: string
  finished_at: string | null
  error: string | null
}

export type AnswerResult = {
  answer_id: string
  question_index: number
  transcript: string
  metrics: Record<string, any>
  heuristic_scores: Record<string, number>
  analysis: Record<string, any> | null
  followup: string | null
  router: Record<string, any> | null
  transcript_meta?: Record<string, any> | null
}

async function request<T>(path: string, apiKey: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('X-API-Key', apiKey)
  if (init.body && !(init.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(`${BASE}${path}`, { ...init, headers })
  const text = await response.text()
  const data = text ? JSON.parse(text) : null
  if (!response.ok) {
    const message = data?.error?.message || `Request failed (${response.status})`
    throw new Error(message)
  }
  return data as T
}

export function createInterview(
  apiKey: string,
  payload: {
    role: string
    candidate_name?: string
    resume_text?: string
    jd_text?: string
    callback_url?: string
    config?: Record<string, unknown>
  },
) {
  return request<{ interview: InterviewStatus; questions: Question[] }>('/interviews', apiKey, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function createInterviewWithFiles(apiKey: string, form: FormData) {
  return request<{ interview: InterviewStatus; questions: Question[] }>('/interviews/upload', apiKey, {
    method: 'POST',
    body: form,
  })
}

export function submitAudioAnswer(
  apiKey: string,
  interviewId: string,
  questionIndex: number,
  blob: Blob,
  durationSeconds: number,
) {
  const form = new FormData()
  form.append('question_index', String(questionIndex))
  form.append('duration_seconds', durationSeconds.toFixed(2))
  form.append('audio', blob, 'answer.webm')
  return request<AnswerResult>(`/interviews/${interviewId}/answers/audio`, apiKey, {
    method: 'POST',
    body: form,
  })
}

export function submitTextAnswer(apiKey: string, interviewId: string, questionIndex: number, transcript: string) {
  return request<AnswerResult>(`/interviews/${interviewId}/answers`, apiKey, {
    method: 'POST',
    body: JSON.stringify({ question_index: questionIndex, transcript }),
  })
}

export function finishInterview(apiKey: string, interviewId: string) {
  return request<{ interview_id: string; status: string; report: any }>(
    `/interviews/${interviewId}/finish`,
    apiKey,
    { method: 'POST' },
  )
}

export function getReport(apiKey: string, interviewId: string) {
  return request<{ interview_id: string; status: string; report: any }>(
    `/interviews/${interviewId}/report`,
    apiKey,
  )
}

export function getModels(apiKey: string) {
  return request<any>('/models', apiKey)
}

export async function synthesize(apiKey: string, text: string): Promise<Blob | null> {
  const form = new FormData()
  form.append('text', text)
  const response = await fetch(`${BASE}/speech/synthesize`, {
    method: 'POST',
    headers: { 'X-API-Key': apiKey },
    body: form,
  })
  if (!response.ok) return null
  return response.blob()
}

export async function speak(apiKey: string, text: string): Promise<void> {
  const blob = await synthesize(apiKey, text)
  if (blob) {
    const audio = new Audio(URL.createObjectURL(blob))
    await audio.play()
    return
  }
  if ('speechSynthesis' in window) {
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(text))
  }
}
