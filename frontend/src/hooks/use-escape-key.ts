import React from "react";

/**
 * Hook that listens for the Escape key and invokes the provided callback.
 *
 * @param onEscape - Called when the Escape key is pressed.
 *                   If `undefined`, the listener is still registered but does nothing.
 */
export function useEscapeKey(onEscape?: () => void) {
  const callbackRef = React.useRef(onEscape);

  React.useEffect(() => {
    callbackRef.current = onEscape;
  }, [onEscape]);

  React.useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        callbackRef.current?.();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);
}
