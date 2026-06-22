import React from "react";

/**
 * Hook that synchronizes state across browser tabs by listening to
 * `storage` events and optionally re-checking on window `focus`.
 *
 * @param onStorageChange - Called when a `storage` event fires.
 * @param onWindowFocus   - Called when the window regains focus (optional).
 */
export function useCrossTabState(
  onStorageChange: (event: StorageEvent) => void,
  onWindowFocus?: () => void,
) {
  const storageRef = React.useRef(onStorageChange);
  const focusRef = React.useRef(onWindowFocus);

  React.useEffect(() => {
    storageRef.current = onStorageChange;
  }, [onStorageChange]);

  React.useEffect(() => {
    focusRef.current = onWindowFocus;
  }, [onWindowFocus]);

  React.useEffect(() => {
    const handleStorage = (event: StorageEvent) => {
      storageRef.current(event);
    };

    const handleFocus = focusRef.current
      ? () => {
          focusRef.current?.();
        }
      : undefined;

    window.addEventListener("storage", handleStorage);
    if (handleFocus) {
      window.addEventListener("focus", handleFocus);
    }

    return () => {
      window.removeEventListener("storage", handleStorage);
      if (handleFocus) {
        window.removeEventListener("focus", handleFocus);
      }
    };
  }, []);
}
