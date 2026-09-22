import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, api } from '../lib/api'
import type { CheckResult, PremarketCatalog } from '../lib/types'

// Э-05 по прототипу: один вопрос на экран, пять шагов полосами прогресса.
// Одно решение за раз — так меньше соблазна проставить всё «нормально»
// одним движением, а это и есть смысл чека.
export function PremarketPage() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const catalog = useQuery<PremarketCatalog>({
    queryKey: ['premarket-questions'],
    queryFn: () => api.get<PremarketCatalog>('/premarket/questions'),
  })

  const [answers, setAnswers] = useState<Record<string, number>>({})
  const [step, setStep] = useState(0)
  const [result, setResult] = useState<CheckResult | null>(null)
  const [error, setError] = useState('')

  const submit = useMutation({
    mutationFn: (filled: Record<string, number>) =>
      api.post<CheckResult>('/premarket/checks', { answers: filled }),
    onSuccess: (data) => {
      setResult(data)
      setError('')
      qc.invalidateQueries({ queryKey: ['today'] })
      qc.invalidateQueries({ queryKey: ['premarket-questions'] })
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Не получилось.'),
  })

  if (catalog.isLoading) return <Centered>загрузка…</Centered>
  if (!catalog.data) return <Centered>Не удалось получить состав чека.</Centered>
  if (result) return <Result result={result} onDone={() => navigate('/today')} />
  if (catalog.data.already_done) {
    return (
      <Centered>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: 17, marginBottom: 8 }}>Чек за сегодня уже пройден</div>
          <div className="hint" style={{ marginBottom: 20 }}>
            Перепройти его нельзя — в этом и смысл допуска.
          </div>
          <button className="cta" onClick={() => navigate('/today')}>
            К главному экрану
          </button>
        </div>
      </Centered>
    )
  }

  const questions = catalog.data.questions
  const question = questions[step]
  const total = questions.length

  function answer(value: number) {
    const filled = { ...answers, [question.id]: value }
    setAnswers(filled)
    if (step + 1 < total) {
      setStep(step + 1)
      return
    }
    submit.mutate(filled)
  }

  return (
    <Centered>
      <div style={{ width: 640, maxWidth: '100%' }}>
        <div style={{ display: 'flex', gap: 7, marginBottom: 34 }}>
          {questions.map((q, i) => (
            <span
              key={q.id}
              style={{
                height: 4,
                flexGrow: 1,
                borderRadius: 2,
                background: i <= step ? 'var(--accent)' : 'var(--line)',
              }}
            />
          ))}
        </div>

        <div className="klabel" style={{ marginBottom: 14 }}>
          Вопрос {step + 1} из {total}
        </div>
        <div className="serif" style={{ fontSize: 36, lineHeight: 1.2 }}>
          {question.text}
        </div>
        <div className="hint" style={{ marginTop: 12, minHeight: 20 }}>
          {question.hint}
        </div>

        <div style={{ display: 'flex', gap: 12, marginTop: 34 }}>
          {[1, 2, 3, 4, 5].map((value) => (
            <button
              key={value}
              onClick={() => answer(value)}
              disabled={submit.isPending}
              className="mono"
              style={{
                width: 82,
                height: 82,
                borderRadius: 10,
                fontSize: 20,
                padding: 0,
              }}
            >
              {value}
            </button>
          ))}
        </div>

        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            marginTop: 12,
            fontSize: 12,
            color: 'var(--faint)',
          }}
        >
          <span>{question.labels[String(question.min)]}</span>
          <span>{question.labels[String(question.max)]}</span>
        </div>

        <div style={{ marginTop: 40, height: 20 }}>
          {step > 0 && (
            <button
              onClick={() => setStep(step - 1)}
              style={{
                border: 'none',
                background: 'transparent',
                color: 'var(--accent)',
                fontSize: 13,
                padding: 0,
              }}
            >
              ← Предыдущий вопрос
            </button>
          )}
          {submit.isPending && <span className="hint">считаю балл…</span>}
        </div>
        {error && <div className="err" style={{ marginTop: 16 }}>{error}</div>}
      </div>
    </Centered>
  )
}

const VERDICTS: Record<string, { title: string; color: string; action: string }> = {
  green: { title: 'Допуск зелёный', color: 'var(--ok)', action: 'Открыть сессию' },
  red: { title: 'Допуск под риском', color: 'var(--warn)', action: 'Открыть сессию' },
  denied: { title: 'Допуска на сегодня нет', color: 'var(--bad)', action: 'Дальше' },
}

function Result({ result, onDone }: { result: CheckResult; onDone: () => void }) {
  const verdict = VERDICTS[result.verdict]
  return (
    <Centered>
      <div style={{ width: 560, maxWidth: '100%', textAlign: 'center' }}>
        <div className="mono" style={{ fontSize: 64, lineHeight: 1 }}>
          {result.score}
          <span style={{ fontSize: 28, color: 'var(--faint)' }}> / {result.max_score}</span>
        </div>

        <div className="serif" style={{ fontSize: 34, marginTop: 26, color: verdict.color }}>
          {verdict.title}
        </div>
        <div
          style={{ fontSize: 14, color: 'var(--dim)', marginTop: 12, lineHeight: 1.6 }}
        >
          {result.verdict === 'denied'
            ? `Порог допуска — ${result.min_score}. Сессия не откроется, перепройти чек сегодня нельзя. Любая сделка, открытая сегодня, будет зафиксирована как инцидент.`
            : result.session_opened_at
              ? `Сессия открыта в ${new Date(result.session_opened_at).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}`
              : ''}
        </div>

        {result.weak.length > 0 && (
          <div
            className="card"
            style={{ marginTop: 26, padding: '16px 20px', textAlign: 'left' }}
          >
            <div className="klabel" style={{ marginBottom: 9 }}>
              Балл просадили
            </div>
            {result.weak.map((w) => (
              <div key={w.id} style={{ display: 'flex', fontSize: 13, padding: '3px 0' }}>
                <span>{w.short}</span>
                <span
                  className="mono"
                  style={{
                    marginLeft: 'auto',
                    color: w.points <= 1 ? 'var(--bad)' : 'var(--warn)',
                  }}
                >
                  {w.points} / {w.max}
                </span>
              </div>
            ))}
          </div>
        )}

        {result.restrictions && (
          <div
            style={{
              marginTop: 20,
              padding: '14px 18px',
              border: '1px solid #4a4326',
              borderRadius: 10,
              textAlign: 'left',
            }}
          >
            <div style={{ fontSize: 13, marginBottom: 4 }}>{result.restrictions.text}</div>
            <div className="hint">{result.restrictions.note}</div>
          </div>
        )}

        <div style={{ marginTop: 34 }}>
          <button className="cta" onClick={onDone}>
            {verdict.action}
          </button>
        </div>
      </div>
    </Centered>
  )
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        minHeight: 'calc(100vh - 140px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      {children}
    </div>
  )
}
