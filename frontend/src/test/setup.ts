import '@testing-library/jest-dom/vitest'

import { beforeEach, vi } from 'vitest'

// jsdom has no viewport scrolling implementation.
beforeEach(() => {
  window.scrollTo = vi.fn()
})
