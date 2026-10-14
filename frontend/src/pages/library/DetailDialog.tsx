import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { ChevronLeft, ChevronRight, X } from 'lucide-react'
import type { Entry } from './types'

export default function DetailDialog({
  entry,
  onClose,
  children,
}: {
  entry: Entry
  onClose: () => void
  children: ReactNode
}) {
  const dialog = useRef<HTMLDivElement>(null)
  const overlay = useRef<HTMLDivElement>(null)
  const close = useRef(onClose)
  close.current = onClose
  const [index, setIndex] = useState(0)
  const blocks = 'content_blocks' in entry.item ? entry.item.content_blocks : []
  const imageBlocks = blocks.filter((block) => block.type === 'image')
  const images = [
    ...new Set([
      ...imageBlocks.map((block) => block.attachment_id),
      ...(entry.item.attachment_ids || []),
    ]),
  ].map((id) => ({
    id,
    caption:
      imageBlocks.find((block) => block.attachment_id === id)?.caption ||
      `资料图片 ${id}`,
  }))
  const current = images[Math.min(index, images.length - 1)]

  useLayoutEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null
    const x = window.scrollX
    const y = window.scrollY
    const bodyStyle = document.body.getAttribute('style')
    const siblings = [...document.body.children].filter(
      (element) => element !== overlay.current,
    )
    const previousAttributes = siblings.map((element) => ({
      element,
      inert: element.getAttribute('inert'),
      hidden: element.getAttribute('aria-hidden'),
    }))
    document.body.style.position = 'fixed'
    document.body.style.top = `-${y}px`
    document.body.style.left = `-${x}px`
    document.body.style.width = '100%'
    document.body.style.overflow = 'hidden'
    for (const element of siblings) {
      element.setAttribute('inert', '')
      element.setAttribute('aria-hidden', 'true')
    }
    const panel = dialog.current!
    panel
      .querySelector<HTMLButtonElement>('button')
      ?.focus({ preventScroll: true })
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        close.current()
      }
      if (event.key !== 'Tab') return
      const focusable = [
        ...panel.querySelectorAll<HTMLElement>(
          'button, a[href], input, select, textarea, [tabindex]',
        ),
      ].filter(
        (element) =>
          !element.matches(':disabled, [tabindex="-1"]') &&
          !element.closest('[hidden], [inert]') &&
          getComputedStyle(element).display !== 'none' &&
          getComputedStyle(element).visibility !== 'hidden',
      )
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (!first) {
        event.preventDefault()
        panel.focus()
      } else if (
        event.shiftKey &&
        (document.activeElement === first ||
          !panel.contains(document.activeElement))
      ) {
        event.preventDefault()
        last.focus()
      } else if (
        !event.shiftKey &&
        (document.activeElement === last ||
          !panel.contains(document.activeElement))
      ) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      if (bodyStyle === null) document.body.removeAttribute('style')
      else document.body.setAttribute('style', bodyStyle)
      for (const { element, inert, hidden } of previousAttributes) {
        if (inert === null) element.removeAttribute('inert')
        else element.setAttribute('inert', inert)
        if (hidden === null) element.removeAttribute('aria-hidden')
        else element.setAttribute('aria-hidden', hidden)
      }
      window.scrollTo(x, y)
      if (previousFocus?.isConnected)
        previousFocus.focus({ preventScroll: true })
    }
  }, [])

  return createPortal(
    <div
      className="detail-overlay"
      ref={overlay}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        className={`detail-dialog ${current ? 'has-gallery' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-label="资料详情"
        tabIndex={-1}
        ref={dialog}
      >
        <header className="detail-dialog-header">
          <span>资料详情</span>
          <button
            className="detail-close"
            aria-label="关闭资料详情"
            onClick={onClose}
          >
            <X size={22} />
          </button>
        </header>
        <div className="detail-dialog-body">
          {current && (
            <section className="detail-gallery" aria-label="资料图片">
              <div className="detail-gallery-image">
                <img
                  src={`/api/v1/attachments/${current.id}`}
                  alt={current.caption}
                />
              </div>
              <div className="detail-gallery-controls">
                <button
                  aria-label="上一张图片"
                  disabled={index === 0}
                  onClick={() => setIndex((value) => value - 1)}
                >
                  <ChevronLeft size={20} />
                </button>
                <span aria-live="polite">
                  {Math.min(index + 1, images.length)} / {images.length}
                </span>
                <button
                  aria-label="下一张图片"
                  disabled={index >= images.length - 1}
                  onClick={() => setIndex((value) => value + 1)}
                >
                  <ChevronRight size={20} />
                </button>
                <a
                  href={`/api/v1/attachments/${current.id}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  查看原图
                </a>
              </div>
              <p className="detail-gallery-caption">{current.caption}</p>
            </section>
          )}
          <section
            className="detail-reading"
            role="region"
            aria-label="资料详情"
          >
            {children}
          </section>
        </div>
      </div>
    </div>,
    document.body,
  )
}
