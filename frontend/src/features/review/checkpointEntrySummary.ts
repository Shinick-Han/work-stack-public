/**
 * Compatibility re-export of the checkpoint payload reader.
 *
 * The reader itself now lives in `domain/checkpointEntrySummary`, because the
 * Task resume view has to read the same opaque worklog payload and a feature
 * may not import another feature. Nothing about the interpretation moved with
 * it: this file exists so Review keeps its own import path while there is
 * exactly ONE implementation of what a checkpoint record says.
 */

export * from '../../domain/checkpointEntrySummary'
