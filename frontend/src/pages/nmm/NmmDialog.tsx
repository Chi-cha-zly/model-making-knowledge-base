import { useId, useLayoutEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
export default function NmmDialog({
  title,
  onClose,
  children,
}: {
  title: string
  onClose: () => void
  children: ReactNode
}) {
  const id = useId(),
    panel = useRef<HTMLDivElement>(null),
    overlay = useRef<HTMLDivElement>(null),
    close = useRef(onClose)
  close.current = onClose
  useLayoutEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const overflow = document.body.style.overflow
    const siblings = [...document.body.children]
      .filter((el) => el !== overlay.current)
      .map((el) => ({ el, inert: el.getAttribute('inert') }))
    siblings.forEach(({ el }) => el.setAttribute('inert', ''))
    document.body.style.overflow = 'hidden'
    panel.current?.querySelector<HTMLElement>('button')?.focus()
    function keyboard(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.preventDefault()
        close.current()
      }
      if (e.key !== 'Tab') return
      const items = [
        ...panel.current!.querySelectorAll<HTMLElement>(
          'button, a[href], input, select, textarea, [tabindex="0"]',
        ),
      ].filter((el) => !el.matches(':disabled'))
      const first = items[0],
        last = items.at(-1)
      if (
        e.shiftKey &&
        (document.activeElement === first ||
          !panel.current?.contains(document.activeElement))
      ) {
        e.preventDefault()
        last?.focus()
      } else if (
        !e.shiftKey &&
        (document.activeElement === last ||
          !panel.current?.contains(document.activeElement))
      ) {
        e.preventDefault()
        first?.focus()
      }
    }
    document.addEventListener('keydown', keyboard)
    return () => {
      document.removeEventListener('keydown', keyboard)
      document.body.style.overflow = overflow
      siblings.forEach(({ el, inert }) =>
        inert === null
          ? el.removeAttribute('inert')
          : el.setAttribute('inert', inert),
      )
      previous?.focus({ preventScroll: true })
    }
  }, [])
  return createPortal(
    <div
      className="nmm-dialog-backdrop"
      ref={overlay}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        className="nmm-dialog"
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={id}
      >
        <header>
          <h2 id={id}>{title}</h2>
          <button type="button" onClick={onClose} aria-label={`关闭${title}`}>
            关闭
          </button>
        </header>
        {children}
      </div>
    </div>,
    document.body,
  )
}
