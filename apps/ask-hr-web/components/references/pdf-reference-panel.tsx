"use client";

import { ChevronLeft, ChevronRight, FileText, X } from "lucide-react";
import type { PDFDocumentLoadingTask, PDFDocumentProxy } from "pdfjs-dist";
import {
  type CSSProperties,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { useResizeObserver } from "@/hooks/use-resize-observer";
import type {
  AnnotationPage,
  ReferenceContext,
  RichCitation,
} from "@/lib/references/types";

type PdfReferencePanelProps = {
  citation: RichCitation;
  onClose: () => void;
  showCloseButton?: boolean;
};

function normalizedText(value: string): string {
  const container = document.createElement("div");
  container.innerHTML = value;
  return (container.textContent || "")
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/[*_#`~>|[\](){}]/g, " ")
    .replace(/[^\p{L}\p{N}]+/gu, "");
}

function matchingPolygons(
  page: AnnotationPage | undefined,
  excerpt: string | undefined,
): number[][] {
  if (!page || !excerpt) {
    return [];
  }
  const normalizedExcerpt = normalizedText(excerpt);
  if (!normalizedExcerpt) {
    return [];
  }
  return page.lines
    .filter((line) => {
      const normalizedLine = normalizedText(line.text);
      return (
        normalizedLine.length >= 3 && normalizedExcerpt.includes(normalizedLine)
      );
    })
    .map((line) => line.polygon)
    .filter((polygon) => polygon.length >= 8);
}

export function PdfReferencePanel({
  citation,
  onClose,
  showCloseButton = true,
}: PdfReferencePanelProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const pageContainer = useResizeObserver<HTMLDivElement>();
  const [context, setContext] = useState<ReferenceContext>();
  const [pdf, setPdf] = useState<PDFDocumentProxy>();
  const [pageNumber, setPageNumber] = useState(citation.pageStart || 1);
  const [viewportSize, setViewportSize] = useState({ width: 0, height: 0 });
  const [renderWidth, setRenderWidth] = useState(0);
  const [pageReady, setPageReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();

  const pdfUrl = citation.pdfUrl;
  const contextUrl = citation.contextUrl;
  const annotationPage = context?.pages[String(pageNumber)];
  const polygons = useMemo(
    () => matchingPolygons(annotationPage, citation.excerpt),
    [annotationPage, citation.excerpt],
  );

  useEffect(() => {
    if (!pdfUrl || !contextUrl) {
      setError("Detailed reference information is unavailable.");
      setLoading(false);
      return;
    }
    const resolvedPdfUrl = pdfUrl;
    const resolvedContextUrl = contextUrl;

    const abortController = new AbortController();
    let cancelled = false;
    let loadingTask: PDFDocumentLoadingTask | undefined;
    async function load() {
      setLoading(true);
      try {
        const [contextResponse, pdfjs] = await Promise.all([
          fetch(resolvedContextUrl, {
            cache: "no-store",
            signal: abortController.signal,
          }),
          import("pdfjs-dist"),
        ]);
        if (!contextResponse.ok) {
          throw new Error("The reference context could not be loaded.");
        }
        pdfjs.GlobalWorkerOptions.workerSrc = new URL(
          "pdfjs-dist/build/pdf.worker.min.mjs",
          import.meta.url,
        ).toString();
        if (cancelled) {
          return;
        }
        loadingTask = pdfjs.getDocument({
          url: resolvedPdfUrl,
          withCredentials: true,
        });
        const [referenceContext, document] = await Promise.all([
          contextResponse.json() as Promise<ReferenceContext>,
          loadingTask.promise,
        ]);
        if (!cancelled) {
          setContext(referenceContext);
          setPdf(document);
        }
      } catch (caught) {
        if (!cancelled && (caught as Error).name !== "AbortError") {
          setError(
            caught instanceof Error
              ? caught.message
              : "The reference could not be loaded.",
          );
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
      abortController.abort();
      void loadingTask?.destroy();
    };
  }, [contextUrl, pdfUrl]);

  useEffect(() => {
    const measuredWidth = Math.round(pageContainer.size.width);
    if (measuredWidth <= 0) {
      return;
    }
    const timer = window.setTimeout(() => {
      setRenderWidth((current) =>
        current === measuredWidth ? current : measuredWidth,
      );
    }, 120);
    return () => window.clearTimeout(timer);
  }, [pageContainer.size.width]);

  useEffect(() => {
    if (!pdf || !canvasRef.current || renderWidth <= 0) {
      return;
    }

    let cancelled = false;
    let renderTask:
      | { cancel: () => void; promise: Promise<unknown> }
      | undefined;
    async function render() {
      const page = await pdf?.getPage(pageNumber);
      if (!page || cancelled || !canvasRef.current) {
        return;
      }
      setPageReady(false);
      const initialViewport = page.getViewport({ scale: 1 });
      const availableWidth = Math.max(280, renderWidth - 32);
      const scale = availableWidth / initialViewport.width;
      const viewport = page.getViewport({ scale });
      const outputScale = window.devicePixelRatio || 1;
      const canvas = canvasRef.current;
      const canvasContext = canvas.getContext("2d");
      if (!canvasContext) {
        throw new Error("PDF canvas is unavailable");
      }
      canvas.width = Math.floor(viewport.width * outputScale);
      canvas.height = Math.floor(viewport.height * outputScale);
      canvas.style.width = `${Math.floor(viewport.width)}px`;
      canvas.style.height = `${Math.floor(viewport.height)}px`;
      setViewportSize({ width: viewport.width, height: viewport.height });
      renderTask = page.render({
        canvas,
        canvasContext,
        transform:
          outputScale === 1
            ? undefined
            : [outputScale, 0, 0, outputScale, 0, 0],
        viewport,
      });
      await renderTask.promise;
      if (!cancelled) {
        setPageReady(true);
      }
    }
    void render().catch((caught) => {
      if (
        !cancelled &&
        (caught as Error).name !== "RenderingCancelledException"
      ) {
        setError(
          caught instanceof Error
            ? caught.message
            : "The PDF page failed to render.",
        );
      }
    });
    return () => {
      cancelled = true;
      renderTask?.cancel();
    };
  }, [pdf, pageNumber, renderWidth]);

  const canGoPrevious = pageNumber > 1;
  const canGoNext = !!pdf && pageNumber < pdf.numPages;
  const pageStyle = {
    "--pdf-page-width": `${viewportSize.width}px`,
    "--pdf-page-height": `${viewportSize.height}px`,
  } as CSSProperties;

  return (
    <div className="flex h-full min-h-0 flex-col bg-slate-100">
      <div className="flex shrink-0 items-center gap-3 border-b bg-white px-4 py-3">
        <div className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-emerald-50 text-emerald-700">
          <FileText className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-slate-900">
            {citation.label}
          </p>
        </div>
        {showCloseButton && (
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onClose}
            aria-label="Close reference"
          >
            <X />
          </Button>
        )}
      </div>

      <div className="flex shrink-0 items-center border-b bg-white px-3 py-2">
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon-sm"
            disabled={!canGoPrevious}
            onClick={() => setPageNumber((current) => current - 1)}
            aria-label="Previous page"
          >
            <ChevronLeft />
          </Button>
          <span className="min-w-20 text-center text-xs text-slate-600">
            Page {pageNumber}
            {pdf ? ` of ${pdf.numPages}` : ""}
          </span>
          <Button
            variant="ghost"
            size="icon-sm"
            disabled={!canGoNext}
            onClick={() => setPageNumber((current) => current + 1)}
            aria-label="Next page"
          >
            <ChevronRight />
          </Button>
        </div>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="p-4">
          {loading && <Skeleton className="h-[680px] w-full" />}
          {error && (
            <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
              {error}
            </div>
          )}
          {!loading && !error && (
            <div
              ref={pageContainer.ref}
              className="overflow-auto rounded-xl border bg-slate-200 p-4"
            >
              <div
                className="relative mx-auto overflow-hidden bg-white"
                style={{
                  ...pageStyle,
                  width:
                    viewportSize.width > 0 ? "var(--pdf-page-width)" : "100%",
                  height:
                    viewportSize.height > 0
                      ? "var(--pdf-page-height)"
                      : undefined,
                  aspectRatio:
                    viewportSize.width > 0 && viewportSize.height > 0
                      ? `${viewportSize.width} / ${viewportSize.height}`
                      : "0.707",
                }}
              >
                {!pageReady && (
                  <Skeleton className="absolute inset-0 h-full w-full rounded-none" />
                )}
                <canvas
                  ref={canvasRef}
                  className={`bg-white shadow-sm transition-opacity duration-100 ${
                    pageReady ? "opacity-100" : "opacity-0"
                  }`}
                />
                {annotationPage &&
                  pageReady &&
                  viewportSize.width > 0 &&
                  polygons.length > 0 && (
                    <svg
                      className="pointer-events-none absolute inset-0"
                      viewBox={`0 0 ${annotationPage.width} ${annotationPage.height}`}
                      preserveAspectRatio="none"
                      aria-hidden="true"
                    >
                      {polygons.map((polygon) => (
                        <polygon
                          key={polygon.join("-")}
                          points={Array.from(
                            { length: polygon.length / 2 },
                            (_, pointIndex) =>
                              `${polygon[pointIndex * 2]},${
                                polygon[pointIndex * 2 + 1]
                              }`,
                          ).join(" ")}
                          fill="rgba(250, 204, 21, 0.32)"
                          stroke="rgba(202, 138, 4, 0.65)"
                          strokeWidth={0.015}
                        />
                      ))}
                    </svg>
                  )}
              </div>
            </div>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
