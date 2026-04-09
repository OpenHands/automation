/**
 * Calculate toast duration based on message length.
 * Uses average reading speed of 200 words per minute with a 1.5x buffer.
 *
 * @param message - The message to display
 * @param minDuration - Minimum duration in ms (default: 5000 for success, 4000 for error)
 * @param maxDuration - Maximum duration in ms (default: 10000)
 * @returns Duration in milliseconds
 */
export const calculateToastDuration = (
  message: string | null | undefined,
  minDuration: number = 5000,
  maxDuration: number = 10000,
): number => {
  if (!message) {
    return minDuration;
  }

  const wordsPerMinute = 200;
  const charactersPerMinute = wordsPerMinute * 5;
  const charactersPerSecond = charactersPerMinute / 60;

  const readingTimeMs = (message.length / charactersPerSecond) * 1000;
  const durationWithBuffer = readingTimeMs * 1.5;

  return Math.min(Math.max(durationWithBuffer, minDuration), maxDuration);
};
