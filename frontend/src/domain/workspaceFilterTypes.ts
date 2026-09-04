/**
 * Dependency-free Workspace filter coordinates.
 *
 * Q3: these declarations are shared by the domain layer and the Workspace
 * feature, so they live here and import NOTHING - not even domain/types. The
 * feature paths keep exporting the same constant instance and the same types,
 * so no consumer needs to change and no second array or union exists.
 */

export const DONE_VISIBILITIES = ["default", "hide", "show"] as const;
export type DoneVisibility = (typeof DONE_VISIBILITIES)[number];

export type OutcomeFilter =
  | { kind: 'all' }
  | { kind: 'unassigned' }
  | { kind: 'pair'; objectiveId: string; keyResultId: string }
