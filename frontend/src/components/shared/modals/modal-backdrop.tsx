import React from "react";
import { useEscapeKey } from "#/hooks/use-escape-key";

interface ModalBackdropProps {
  children: React.ReactNode;
  onClose?: () => void;
  "aria-label"?: string;
}

export function ModalBackdrop({
  children,
  onClose,
  "aria-label": ariaLabel,
}: ModalBackdropProps) {
  useEscapeKey(onClose);

  const handleClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) onClose?.();
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={ariaLabel}
      className="fixed inset-0 z-60 flex items-center justify-center"
    >
      <div
        onClick={handleClick}
        className="fixed inset-0 bg-black opacity-60"
      />
      <div className="relative">{children}</div>
    </div>
  );
}
