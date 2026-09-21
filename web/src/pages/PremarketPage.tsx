import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, api } from '../lib/api'
import type { CheckResult, PremarketCatalog } from '../lib/types'

// Э-05: один вопрос на экран, пять шагов, прогресс точками.
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

  if (catalog.isLoading) {
    return <div className="hint">загрузка…</div>
  }
  if (!catalog.data) {
    return <div className="err">Не удалось получить состав чека.</div>
  }
  if (result) {
    return <Result result={result} onDone={() => navigate('/today')} />
  }
  if (catalog.data.already_done) {
    return (
      <div className="card" style={{ padding: '20px 22px', maxWidth: 560 }}>
        <div style={{ marginBottom: 8 }}>Чек за сегодня уже пройден.</div>
        <div className="hint" style={{ marginBottom: 14 }}>
          Перепройти его нельзя — в этом и смысл допуска.
        </div>
        <button className="primary" onClick={() => navigate('/today')}>
          К главному экрану
        </button>
      </div>
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
    <div style={{ maxWidth: 620 }}>
      <div style={{ display: 'flex', gap: 6, marginBottom: 28 }}>
        {questions.map((q, i) => (
          <span
            key={q.id}
            style={{
              width: 8,
              height: 8,
              borderRadius: 4,
              background:
                i < step ? 'var(--ok)' : i === step ? 'var(--fg)' : 'var(--line-2)',
            }}
          />
        ))}
        <span className="hint" style={{ marginLeft: 8 }}>
          {step + 1} из {total}
        </span>
      </div>

      <div className="serif" style={{ fontSize: 26, lineHeight: 1.3, marginBottom: 6 }}>
        {question.text}
      </div>
      {question.inverted && (
        <div className="hint" style={{ marginBottom: 18, color: 'var(--warn)' }}>
          Здесь шкала обратная: чем меньше — тем лучше для допуска.
        </div>
      )}
      {!question.inverted && <div style={{ height: 18 }} />}

      <div style={{ display: 'flex', gap: 10, marginBottom: 10 }}>
        {[1, 2, 3, 4, 5].map((value) => (
          <button
            key={value}
            onClick={() => answer(value)}
            disabled={submit.isPending}
            className="mono"
            style={{ flex: 1, fontSize: 20, padding: '20px 0' }}
          >
            {value}
          </button>
        ))}
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span className="hint">{question.labels[String(question.min)]}</span>
        <span className="hint">{question.labels[String(question.max)]}</span>
      </div>

      {step > 0 && (
        <button
          onClick={() => setStep(step - 1)}
          style={{ marginTop: 24, fontSize: 12, padding: '6px 12px' }}
        >
          Назад
        </button>
      )}
      {submit.isPending && (
        <div className="hint" style={{ marginTop: 20 }}>
          считаю балл…
        </div>
      )}
      {error && (
        <div className="err" style={{ marginTop: 20 }}>
          {error}
        </div>
      )}
    </div>
  )
}

const VERDICTS: Record<string, { title: string; color: string; action: string }> = {
  green: { title: 'Допуск зелёный', color: 'var(--ok)', action: 'Перейти к работе' },
  red: { title: 'Допуск под риском', color: 'var(--warn)', action: 'Открыть сессию' },
  denied: { title: 'Допуска на сегодня нет', color: 'var(--bad)', action: 'Дневник' },
}

function Result({ result, onDone }: { result: CheckResult; onDone: () => void }) {
  const verdict = VERDICTS[result.verdict]
  return (
    <div style={{ maxWidth: 520, paddingTop: 20, textAlign: 'center' }}>
      <div className="mono" style={{ fontSize: 44, marginBottom: 6 }}>
        {result.score} / {result.max_score}
      </div>
      <div style={{ fontSize: 18, color: verdict.color, marginBottom: 16 }}>
        {verdict.title}
      </div>

      {result.verdict === 'denied' ? (
        <div className="hint" style={{ marginBottom: 20, lineHeight: 1.8 }}>
          Порог допуска — {result.min_score}. Сессия не откроется, перепройти чек
          сегодня нельзя. Любая сделка, открытая сегодня, будет зафиксирована
          как инцидент.
        </div>
      ) : (
        <div className="hint" style={{ marginBottom: 20 }}>
          {result.session_opened_at
            ? `Сессия открыта в ${new Date(result.session_opened_at).toLocaleTimeString(
                'ru-RU',
                { hour: '2-digit', minute: '2-digit' },
              )}`
            : ''}
        </div>
      )}

      {result.weak.length > 0 && (
        <div className="hint" style={{ marginBottom: 20 }}>
          Просадили балл:{' '}
          {result.weak.map((w) => `${w.short} ${w.points}/${w.max}`).join(' · ')}
        </div>
      )}

      {result.restrictions && (
        <div
          style={{
            border: '1px solid #4a4326',
            borderRadius: 8,
            padding: '12px 14px',
            marginBottom: 20,
            textAlign: 'left',
          }}
        >
          <div style={{ fontSize: 13, marginBottom: 4 }}>{result.restrictions.text}</div>
          <div className="hint">{result.restrictions.note}</div>
        </div>
      )}

      <button className="primary" onClick={onDone}>
        {verdict.action}
      </button>
    </div>
  )
}
