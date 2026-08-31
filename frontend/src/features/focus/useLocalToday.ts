import { useEffect, useState } from 'react'
import { localIsoDate } from './focusModel'

const MIDNIGHT_GRACE_MS = 25

function delayUntilNextLocalMidnight(now: Date) {
  const nextMidnight = new Date(now.getTime())
  nextMidnight.setHours(24, 0, 0, MIDNIGHT_GRACE_MS)
  return Math.max(1, nextMidnight.getTime() - now.getTime())
}

/**
 * Tracks only the browser's local calendar date. It never fetches data. The one-shot
 * midnight timer is rescheduled after every fire and whenever a visible document may
 * have returned with a different clock or timezone.
 */
export function useLocalToday() {
  const [today, setToday] = useState(() => localIsoDate())

  useEffect(() => {
    let disposed = false
    let midnightTimer: number | null = null

    const scheduleMidnight = () => {
      if (midnightTimer !== null) window.clearTimeout(midnightTimer)
      const now = new Date()
      midnightTimer = window.setTimeout(() => {
        midnightTimer = null
        if (disposed) return
        setToday((current) => {
          const next = localIsoDate()
          return current === next ? current : next
        })
        scheduleMidnight()
      }, delayUntilNextLocalMidnight(now))
    }

    const refreshVisibleDate = () => {
      if (document.visibilityState !== 'visible') return
      setToday((current) => {
        const next = localIsoDate()
        return current === next ? current : next
      })
      // Recompute the delay even if the date string did not change: the system clock
      // or timezone may have changed while the document was hidden.
      scheduleMidnight()
    }

    scheduleMidnight()
    document.addEventListener('visibilitychange', refreshVisibleDate)
    return () => {
      disposed = true
      if (midnightTimer !== null) window.clearTimeout(midnightTimer)
      document.removeEventListener('visibilitychange', refreshVisibleDate)
    }
  }, [])

  return today
}
