// Заглушка раздела, который появится на своём шаге. Текст честный:
// не «скоро», а на каком шаге и что там будет.
export function Stub({ title, step, what }: { title: string; step: number; what: string }) {
  return (
    <div className="card" style={{ padding: '20px 22px', maxWidth: 620 }}>
      <div className="klabel" style={{ marginBottom: 10 }}>
        {title}
      </div>
      <div style={{ color: 'var(--dim)' }}>{what}</div>
      <div className="hint" style={{ marginTop: 10 }}>
        Появится на шаге {step} плана разработки.
      </div>
    </div>
  )
}
