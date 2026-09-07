import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react'
import type { WorkspaceTask } from './types'
import {
  seedTableLocalViewData,
  parseTableLocalViewData,
  type TableDensity,
  type TableLocalViewData,
  type TableNamedSortField,
} from './tablePreferences'
import { pruneDeletedTableOrder } from './tableOrdering'
import {
  createExternalUpdateGate,
  createLocalViewStore,
  LAYOUT_SAVE_FAILURE_MESSAGE,
  type LocalViewStore,
} from './localViewState'
import {
  clearTableManualOrder,
  enableTableManualSort,
  setTableDensity,
  toggleTableNamedSort,
} from './tablePreferenceUpdates'
import {
  abortTableInteraction,
  applyTableMoveKeyDown,
  cancelTableGesture,
  onTableHandlePointerCancel,
  onTableHandlePointerDown,
  onTableHandlePointerMove,
  onTableHandlePointerUp,
  tableVisibleSetChanged,
  visibleTableTasks,
  type HandlePointerEvent,
  type PendingPointer,
  type TableExternalGate,
  type TableGesture,
  type TableReorderController,
} from './tableReorderGesture'

function createTableStore(workspaceId: string, onPersistFailure: () => void) {
  return createLocalViewStore({
    workspaceId,
    view: 'table',
    defaultData: seedTableLocalViewData(),
    parseData: parseTableLocalViewData,
    onPersistFailure,
  })
}

function announceTable(
  persistFailedRef: { current: boolean },
  setAnnouncement: (message: string) => void,
  message: string,
) {
  setAnnouncement(persistFailedRef.current ? LAYOUT_SAVE_FAILURE_MESSAGE : message)
}

function swapTableStoreIdentity(args: {
  store: LocalViewStore<TableLocalViewData>
  storeRef: MutableRefObject<LocalViewStore<TableLocalViewData>>
  persistFailedRef: MutableRefObject<boolean>
  tableLocal: TableLocalViewData
  gesture: TableGesture | null
  setTableLocal: (data: TableLocalViewData) => void
  setGesture: (gesture: TableGesture | null) => void
  abort: () => void
}) {
  if (args.storeRef.current === args.store) {
    return { currentLocal: args.tableLocal, liveGesture: args.gesture }
  }
  args.abort()
  args.persistFailedRef.current = false
  args.storeRef.current = args.store
  const currentLocal = args.store.read()
  args.setTableLocal(currentLocal)
  if (args.gesture !== null) args.setGesture(null)
  return { currentLocal, liveGesture: null as TableGesture | null }
}

function useTableStoreSync(
  store: LocalViewStore<TableLocalViewData>,
  skipExternalRef: MutableRefObject<boolean>,
  persistFailedRef: MutableRefObject<boolean>,
  setTableLocal: (data: TableLocalViewData) => void,
  setAnnouncement: (message: string) => void,
  gateRef: TableReorderController['gateRef'],
  abort: () => void,
) {
  useEffect(() => {
    setTableLocal(store.read())
    gateRef.current = createExternalUpdateGate<TableLocalViewData>((data) => {
      setTableLocal(data)
      announceTable(persistFailedRef, setAnnouncement, 'Table order updated from another tab.')
    })
    const stop = store.subscribe((data) => {
      if (skipExternalRef.current) return
      gateRef.current.onExternal(data)
    })
    return () => {
      abort()
      stop()
    }
  }, [store])
}

function useTableGestureEffects(
  liveGesture: TableGesture | null,
  visibleIds: readonly string[],
  controllerRef: MutableRefObject<TableReorderController>,
  cancelGesture: (shouldAnnounce?: boolean) => void,
) {
  useEffect(() => {
    if (!liveGesture) return
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      cancelGesture()
    }
    const onPointerCancel = () => {
      const controller = controllerRef.current
      if (controller.ignoreLostCaptureRef.current) return
      if (controller.gestureRef.current?.source === 'pointer' || controller.pendingPointerRef.current) {
        cancelGesture()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('pointercancel', onPointerCancel)
    window.addEventListener('lostpointercapture', onPointerCancel)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('pointercancel', onPointerCancel)
      window.removeEventListener('lostpointercapture', onPointerCancel)
    }
  }, [liveGesture, cancelGesture])

  useEffect(() => {
    const active = controllerRef.current.gestureRef.current
    if (!active) return
    if (tableVisibleSetChanged(active, visibleIds)) cancelGesture()
  }, [cancelGesture, visibleIds])

  useEffect(() => {
    if (liveGesture?.source !== 'keyboard') return
    const handle = document.querySelector(`[data-table-move-handle="${liveGesture.id}"]`)
    if (handle instanceof HTMLElement) handle.focus()
  }, [liveGesture])
}

function bindTableController(
  controllerRef: MutableRefObject<TableReorderController>,
  fields: TableReorderController,
) {
  controllerRef.current = fields
}

function tableCaption(sortField: TableLocalViewData['sortField'], descending: boolean) {
  if (sortField === 'manual') return 'Tasks in Manual order'
  return `Tasks sorted by ${sortField} ${descending ? 'descending' : 'ascending'}`
}

export function useTableViewSession({
  workspaceId,
  tasks,
  referenceTasks,
}: {
  workspaceId?: string
  tasks: readonly WorkspaceTask[]
  referenceTasks: readonly WorkspaceTask[]
}) {
  const persistWorkspaceId = workspaceId ?? ''
  const skipExternalRef = useRef(false)
  const persistFailedRef = useRef(false)
  const [announcement, setAnnouncement] = useState('')
  const announce = (message: string) => announceTable(persistFailedRef, setAnnouncement, message)
  const store = useMemo(
    () => createTableStore(persistWorkspaceId, () => {
      persistFailedRef.current = true
      setAnnouncement(LAYOUT_SAVE_FAILURE_MESSAGE)
    }),
    [persistWorkspaceId],
  )
  const storeRef = useRef(store)
  const [tableLocal, setTableLocal] = useState<TableLocalViewData>(() => store.read())
  const [gesture, setGesture] = useState<TableGesture | null>(null)
  const controllerRef = useRef<TableReorderController>(null as unknown as TableReorderController)
  const gestureRef = useRef<TableGesture | null>(null)
  const pendingPointerRef = useRef<PendingPointer | null>(null)
  const ignoreLostCaptureRef = useRef(false)
  const tableLocalRef = useRef(tableLocal)
  const canonicalRef = useRef<readonly string[]>([])
  const visibleRef = useRef<readonly string[]>([])
  const gateRef = useRef<TableExternalGate>(createExternalUpdateGate<TableLocalViewData>((data) => {
    setTableLocal(data)
    announce('Table order updated from another tab.')
  }))
  const writeLocal = useCallback((mutator: (current: TableLocalViewData) => TableLocalViewData) => {
    skipExternalRef.current = true
    const next = store.update((current) => {
      const proposed = mutator(current)
      return parseTableLocalViewData({
        ...proposed,
        order: pruneDeletedTableOrder(proposed.order, canonicalRef.current),
      }) ?? current
    })
    skipExternalRef.current = false
    setTableLocal(next)
    return next
  }, [store])
  const abort = useCallback(() => abortTableInteraction(controllerRef.current), [])
  const swapped = swapTableStoreIdentity({
    store, storeRef, persistFailedRef, tableLocal, gesture, setTableLocal, setGesture, abort,
  })
  const { currentLocal, liveGesture } = swapped
  gestureRef.current = liveGesture
  tableLocalRef.current = currentLocal
  const canonicalIds = useMemo(() => referenceTasks.map((task) => task.id), [referenceTasks])
  canonicalRef.current = canonicalIds
  const visibleIds = useMemo(() => tasks.map((task) => task.id), [tasks])
  visibleRef.current = visibleIds
  const { density, descending, sortField } = currentLocal
  const manual = sortField === 'manual'
  bindTableController(controllerRef, {
    manual, gestureRef, pendingPointerRef, ignoreLostCaptureRef, tableLocalRef,
    canonicalRef, visibleRef, gateRef, setGesture, announce, writeLocal,
  })
  useTableStoreSync(store, skipExternalRef, persistFailedRef, setTableLocal, setAnnouncement, gateRef, abort)
  const displayed = useMemo(
    () => visibleTableTasks(tasks, sortField, descending, liveGesture, currentLocal.order, canonicalIds),
    [canonicalIds, currentLocal.order, descending, liveGesture, sortField, tasks],
  )
  const cancelGesture = useCallback((shouldAnnounce = true) => {
    cancelTableGesture(controllerRef.current, shouldAnnounce)
  }, [])
  useTableGestureEffects(liveGesture, visibleIds, controllerRef, cancelGesture)
  return tableSessionApi({
    announcement, density, descending, displayed, gestureRef, liveGesture, manual, sortField,
    controllerRef, tableLocalRef, cancelGesture, writeLocal, abort, announce, setGesture,
  })
}

function tableSessionApi(args: {
  announcement: string
  density: TableDensity
  descending: boolean
  displayed: WorkspaceTask[]
  gestureRef: MutableRefObject<TableGesture | null>
  liveGesture: TableGesture | null
  manual: boolean
  sortField: TableLocalViewData['sortField']
  controllerRef: MutableRefObject<TableReorderController>
  tableLocalRef: MutableRefObject<TableLocalViewData>
  cancelGesture: (shouldAnnounce?: boolean) => void
  writeLocal: TableReorderController['writeLocal']
  abort: () => void
  announce: (message: string) => void
  setGesture: (gesture: TableGesture | null) => void
}) {
  const updatePreferences = (mutator: (current: TableLocalViewData) => TableLocalViewData) => {
    if (args.controllerRef.current.gestureRef.current) args.cancelGesture(false)
    args.writeLocal(mutator)
  }
  const changeToManual = () => {
    if (args.tableLocalRef.current.sortField === 'manual' && !args.gestureRef.current) return
    updatePreferences(enableTableManualSort)
  }
  return {
    announcement: args.announcement,
    density: args.density,
    descending: args.descending,
    displayed: args.displayed,
    gestureRef: args.gestureRef,
    liveGesture: args.liveGesture,
    manual: args.manual,
    sortField: args.sortField,
    caption: tableCaption(args.sortField, args.descending),
    changeSort: (field: TableNamedSortField) => updatePreferences((current) => toggleTableNamedSort(current, field)),
    changeToManual,
    changeDensity: (nextDensity: TableDensity) => updatePreferences((current) => setTableDensity(current, nextDensity)),
    resetTableOrder: () => {
      args.abort()
      if (args.liveGesture !== null) args.setGesture(null)
      args.writeLocal(clearTableManualOrder)
      args.announce('Table order reset')
    },
    handleMoveKeyDown: (id: string, event: Parameters<typeof applyTableMoveKeyDown>[2]) => {
      applyTableMoveKeyDown(args.controllerRef.current, id, event)
    },
    onHandlePointerDown: (id: string, event: HandlePointerEvent) => {
      onTableHandlePointerDown(args.controllerRef.current, id, event)
    },
    onHandlePointerMove: (event: HandlePointerEvent) => onTableHandlePointerMove(args.controllerRef.current, event),
    onHandlePointerUp: (event: HandlePointerEvent) => onTableHandlePointerUp(args.controllerRef.current, event),
    onHandlePointerCancel: (event: HandlePointerEvent) => onTableHandlePointerCancel(args.controllerRef.current, event),
  }
}
