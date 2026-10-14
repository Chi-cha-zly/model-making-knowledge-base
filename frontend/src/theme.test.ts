import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { getTheme, initializeTheme, setTheme } from './theme'
beforeEach(() => {
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
  })
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
it('follows system changes only in system mode and persists only preference', () => {
  let listener: (() => void) | undefined
  const media = {
    matches: true,
    addEventListener: vi.fn((_: string, cb: () => void) => {
      listener = cb
    }),
    removeEventListener: vi.fn(),
  }
  vi.stubGlobal('matchMedia', () => media)
  const stop = initializeTheme()
  expect(document.documentElement.dataset.theme).toBe('dark')
  setTheme('light')
  expect(getTheme()).toBe('light')
  expect(localStorage.getItem('modelkb-theme')).toBe('light')
  media.matches = true
  listener?.()
  expect(document.documentElement.dataset.theme).toBe('light')
  setTheme('system')
  expect(document.documentElement.dataset.theme).toBe('dark')
  media.matches = false
  listener?.()
  expect(document.documentElement.dataset.theme).toBe('light')
  stop()
  expect(media.removeEventListener).toHaveBeenCalled()
})
it('works when browser storage is blocked', () => {
  vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
    throw Error('blocked')
  })
  vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
    throw Error('blocked')
  })
  const stop = initializeTheme()
  expect(() => setTheme('dark')).not.toThrow()
  expect(document.documentElement.dataset.theme).toBe('dark')
  stop()
})
