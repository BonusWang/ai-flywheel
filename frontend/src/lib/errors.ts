/** Extract a human-readable message from an unknown caught value. */
export function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message.trim()) return err.message
  if (typeof err === 'object' && err !== null && 'message' in err) {
    const msg = (err as { message?: unknown }).message
    if (typeof msg === 'string' && msg.trim()) return msg
  }
  if (typeof err === 'string' && err.trim()) return err
  return fallback
}
