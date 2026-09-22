// Заглушка блока, который появится на своём шаге. Текст честный: не «скоро»,
// а что там будет и на каком шаге. Компактная — блок стоит в ряду с рабочими
// карточками, и раздувать его ради обещания незачем.
export function Stub({ title, step, what }: { title: string; step: number; what: string }) {
  return (
    <div className="card" style={{ padding: '14px 18px' }}>
      <div className="klabel" style={{ marginBottom: 8 }}>
        {title}
      </div>
      <div className="hint">
        {what} Появится на шаге {step}.
      </div>
    </div>
  )
}
