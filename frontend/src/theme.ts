export type ThemePreference = 'light' | 'dark' | 'system'
const storageKey = 'modelkb-theme'
let preference: ThemePreference = 'system'
let media: MediaQueryList | undefined
function apply() {
  document.documentElement.dataset.theme =
    preference === 'system' ? (media?.matches ? 'dark' : 'light') : preference
}
export function initializeTheme() {
  try {
    const saved = localStorage.getItem(storageKey)
    preference = saved === 'light' || saved === 'dark' ? saved : 'system'
  } catch {
    preference = 'system'
  }
  media =
    typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-color-scheme: dark)')
      : undefined
  const listener = () => {
    if (preference === 'system') apply()
  }
  media?.addEventListener('change', listener)
  const currentMedia = media
  apply()
  return () => currentMedia?.removeEventListener('change', listener)
}
export function getTheme() {
  return preference
}
export function setTheme(value: ThemePreference) {
  preference = value
  try {
    localStorage.setItem(storageKey, value)
  } catch {
    /* Theme remains usable in memory. */
  }
  apply()
}
