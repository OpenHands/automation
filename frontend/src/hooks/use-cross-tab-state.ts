import React from "react";

/**
 * Hook that synchronizes state across browser tabs by listening to
 * `storage` events and re-checking on window `focus`.
 *
 * @param onStorageChange - Called when a `storage` event fires.
 * @param onWindowFocus   - Called when the window regains focus.
 */
export function useCrossTabState(
  onStorageChange: (event: StorageEvent) => void,
  onWindowFocus: () => void,
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

    const handleFocus = () => {
      focusRef.current();
    };

    window.addEventListener("storage", handleStorage);
    window.addEventListener("focus", handleFocus);

    return () => {
      window.removeEventListener("storage", handleStorage);
      window.removeEventListener("focus", handleFocus);
    };
  }, []);
}
