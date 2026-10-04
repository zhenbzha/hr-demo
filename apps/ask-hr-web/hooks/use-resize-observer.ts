"use client";

import { useEffect, useState } from "react";

type ElementSize = {
  width: number;
  height: number;
};

export function useResizeObserver<T extends HTMLElement>() {
  const [element, setElement] = useState<T | null>(null);
  const [size, setSize] = useState<ElementSize>({ width: 0, height: 0 });

  useEffect(() => {
    if (!element) {
      return;
    }

    const updateSize = (width: number, height: number) => {
      setSize((current) =>
        current.width === width && current.height === height
          ? current
          : { width, height },
      );
    };
    const observer = new ResizeObserver(([entry]) => {
      if (entry) {
        updateSize(entry.contentRect.width, entry.contentRect.height);
      }
    });

    updateSize(element.clientWidth, element.clientHeight);
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);

  return { ref: setElement, size };
}
