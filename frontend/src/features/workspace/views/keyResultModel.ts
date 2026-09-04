/**
 * Compatibility surface for the pure Key Result projection.
 *
 * Q3: the implementation moved verbatim to domain/keyResultModel; this path
 * re-exports it so every existing Workspace consumer, and its existing test,
 * keeps the same runtime functions and the same types. There is no second
 * implementation here.
 */

export * from '../../../domain/keyResultModel'
