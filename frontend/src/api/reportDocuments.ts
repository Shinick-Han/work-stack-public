import {
  COMMIT_UNKNOWN_REPORT,
  REPORT_LIST_PAGE_SIZE,
  reportArchiveDataSchema,
  reportCreateDataSchema,
  reportFinalizeDataSchema,
  reportIdempotencyKeySchema,
  reportListDataSchema,
  reportListFilterSchema,
  reportReadDataSchema,
  reportRestoreDataSchema,
  reportReviseDataSchema,
  reportMutationMetaSchema,
  reportUidSchema,
  reportWorkspaceUidSchema,
  type ReportArchiveData,
  type ReportCreateData,
  type ReportFinalizeData,
  type ReportListData,
  type ReportListFilter,
  type ReportPeriod,
  type ReportReadData,
  type ReportRestoreData,
  type ReportReviseData,
} from '../domain/reportDocuments'
import { z } from 'zod'
import {
  ApiError,
  CommitUnknownError,
  getData,
  mutateData,
  type MutateReceipt,
} from './transport'

const REPORTS_PATH = '/api/v1/reports'

export interface ListReportDocumentsInput {
  workspaceUid: string
  state?: ReportListFilter
  cursor?: string
}

export interface ReadReportDocumentInput {
  workspaceUid: string
  reportUid: string
}

export interface CreateReportDocumentInput {
  workspaceUid: string
  template: 'daily-v1'
  period: ReportPeriod
  sourceDigest: string
  sourceGeneratedAt: string
  markdown: string
}

export interface ReportTargetInput {
  workspaceUid: string
  reportUid: string
  expectedRevision: number
}

export interface ReviseReportDocumentInput extends ReportTargetInput {
  markdown: string
  note: string | null
}

export interface ArchiveReportDocumentInput extends ReportTargetInput {
  note: string | null
}

function ownerQuery(workspaceUid: string, extra: Record<string, string> = {}) {
  return new URLSearchParams({ workspace_uid: workspaceUid, ...extra })
}

function reportsUrl(workspaceUid: string, suffix = '', extra: Record<string, string> = {}) {
  return `${REPORTS_PATH}${suffix}?${ownerQuery(workspaceUid, extra)}`
}

function requireWorkspaceUid(workspaceUid: string): string {
  return reportWorkspaceUidSchema.parse(workspaceUid)
}

function requireReportUid(reportUid: string): string {
  return reportUidSchema.parse(reportUid)
}

function requireIdempotencyKey(idempotencyKey: string): string {
  return reportIdempotencyKeySchema.parse(idempotencyKey)
}

function mutationPath(workspaceUid: string, reportUid: string, verb: string) {
  return reportsUrl(workspaceUid, `/${encodeURIComponent(reportUid)}/${verb}`)
}

function assertMutationReceipt(receipt: MutateReceipt, expectedStatus: 200 | 201) {
  const meta = reportMutationMetaSchema.safeParse(receipt.meta)
  if (receipt.status !== expectedStatus || !meta.success) {
    throw new CommitUnknownError(COMMIT_UNKNOWN_REPORT, receipt)
  }
}

async function mutateReport<T>(
  path: string,
  body: unknown,
  schema: z.ZodType<T>,
  idempotencyKey: string,
  expectedStatus: 200 | 201,
): Promise<T> {
  const key = requireIdempotencyKey(idempotencyKey)
  const receipt: MutateReceipt = {}
  try {
    const data = await mutateData(path, 'POST', body, schema, key, false, { receipt })
    assertMutationReceipt(receipt, expectedStatus)
    return data
  } catch (error) {
    if (error instanceof ApiError || error instanceof CommitUnknownError) throw error
    throw new CommitUnknownError(COMMIT_UNKNOWN_REPORT, error)
  }
}

export function listReportDocuments(input: ListReportDocumentsInput): Promise<ReportListData> {
  const workspaceUid = requireWorkspaceUid(input.workspaceUid)
  const state = reportListFilterSchema.parse(input.state ?? 'active')
  const extra: Record<string, string> = { state, limit: String(REPORT_LIST_PAGE_SIZE) }
  if (input.cursor !== undefined) extra.cursor = input.cursor
  return getData(reportsUrl(workspaceUid, '', extra), reportListDataSchema({
    workspace_uid: workspaceUid,
    state,
  }))
}

export function readReportDocument(input: ReadReportDocumentInput): Promise<ReportReadData> {
  const workspaceUid = requireWorkspaceUid(input.workspaceUid)
  const reportUid = requireReportUid(input.reportUid)
  return getData(
    reportsUrl(workspaceUid, `/${encodeURIComponent(reportUid)}`),
    reportReadDataSchema({ report_uid: reportUid }),
  )
}

export function createReportDocument(
  input: CreateReportDocumentInput,
  idempotencyKey: string,
): Promise<ReportCreateData> {
  const workspaceUid = requireWorkspaceUid(input.workspaceUid)
  return mutateReport(
    reportsUrl(workspaceUid),
    {
      workspace_uid: workspaceUid,
      template: input.template,
      period: input.period,
      source_digest: input.sourceDigest,
      source_generated_at: input.sourceGeneratedAt,
      markdown: input.markdown,
    },
    reportCreateDataSchema({
      workspace_uid: workspaceUid,
      template: input.template,
      period: input.period,
      source_digest: input.sourceDigest,
      source_generated_at: input.sourceGeneratedAt,
      markdown: input.markdown,
    }),
    idempotencyKey,
    201,
  )
}

export function reviseReportDocument(
  input: ReviseReportDocumentInput,
  idempotencyKey: string,
): Promise<ReportReviseData> {
  const workspaceUid = requireWorkspaceUid(input.workspaceUid)
  const reportUid = requireReportUid(input.reportUid)
  return mutateReport(
    mutationPath(workspaceUid, reportUid, 'revisions'),
    {
      workspace_uid: workspaceUid,
      expected_revision: input.expectedRevision,
      markdown: input.markdown,
      note: input.note,
    },
    reportReviseDataSchema({
      workspace_uid: workspaceUid,
      uid: reportUid,
      expected_revision: input.expectedRevision,
      markdown: input.markdown,
      note: input.note,
    }),
    idempotencyKey,
    200,
  )
}

export function finalizeReportDocument(
  input: ReportTargetInput,
  idempotencyKey: string,
): Promise<ReportFinalizeData> {
  const workspaceUid = requireWorkspaceUid(input.workspaceUid)
  const reportUid = requireReportUid(input.reportUid)
  return mutateReport(
    mutationPath(workspaceUid, reportUid, 'finalize'),
    { workspace_uid: workspaceUid, expected_revision: input.expectedRevision },
    reportFinalizeDataSchema({
      workspace_uid: workspaceUid,
      uid: reportUid,
      expected_revision: input.expectedRevision,
    }),
    idempotencyKey,
    200,
  )
}

export function archiveReportDocument(
  input: ArchiveReportDocumentInput,
  idempotencyKey: string,
): Promise<ReportArchiveData> {
  const workspaceUid = requireWorkspaceUid(input.workspaceUid)
  const reportUid = requireReportUid(input.reportUid)
  return mutateReport(
    mutationPath(workspaceUid, reportUid, 'archive'),
    {
      workspace_uid: workspaceUid,
      expected_revision: input.expectedRevision,
      note: input.note,
    },
    reportArchiveDataSchema({
      workspace_uid: workspaceUid,
      uid: reportUid,
      expected_revision: input.expectedRevision,
      note: input.note,
    }),
    idempotencyKey,
    200,
  )
}

export function restoreReportDocument(
  input: ReportTargetInput,
  idempotencyKey: string,
): Promise<ReportRestoreData> {
  const workspaceUid = requireWorkspaceUid(input.workspaceUid)
  const reportUid = requireReportUid(input.reportUid)
  return mutateReport(
    mutationPath(workspaceUid, reportUid, 'restore'),
    { workspace_uid: workspaceUid, expected_revision: input.expectedRevision },
    reportRestoreDataSchema({
      workspace_uid: workspaceUid,
      uid: reportUid,
      expected_revision: input.expectedRevision,
    }),
    idempotencyKey,
    200,
  )
}
