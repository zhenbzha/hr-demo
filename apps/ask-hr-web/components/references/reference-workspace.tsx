"use client";

import type { ReactNode } from "react";

import { PdfReferencePanel } from "@/components/references/pdf-reference-panel";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useMediaQuery } from "@/hooks/use-media-query";
import type { RichCitation } from "@/lib/references/types";

export function ReferenceWorkspace({
  children,
  citation,
  onClose,
}: {
  children: ReactNode;
  citation?: RichCitation;
  onClose: () => void;
}) {
  const isDesktop = useMediaQuery("(min-width: 1024px)");
  const viewerKey = citation
    ? `${citation.contextUrl ?? citation.documentId}:${
        citation.pageStart ?? 1
      }:${citation.pageEnd ?? citation.pageStart ?? 1}`
    : undefined;

  if (isDesktop) {
    if (!citation) {
      return <div className="flex min-w-0 flex-1">{children}</div>;
    }
    return (
      <ResizablePanelGroup
        orientation="horizontal"
        className="min-h-0 min-w-0 flex-1"
      >
        <ResizablePanel defaultSize="58" minSize="34">
          <div className="flex h-full min-h-0 min-w-0">{children}</div>
        </ResizablePanel>
        <ResizableHandle withHandle />
        <ResizablePanel defaultSize="42" minSize="30" maxSize="66">
          <PdfReferencePanel
            key={viewerKey}
            citation={citation}
            onClose={onClose}
          />
        </ResizablePanel>
      </ResizablePanelGroup>
    );
  }

  return (
    <div className="flex min-w-0 flex-1">
      {children}
      <Sheet open={!!citation} onOpenChange={(open) => !open && onClose()}>
        <SheetContent
          side="right"
          className="max-w-none gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-none"
          showCloseButton={false}
        >
          <SheetHeader className="sr-only">
            <SheetTitle>{citation?.label || "Policy reference"}</SheetTitle>
            <SheetDescription>
              View the cited policy document and highlighted context.
            </SheetDescription>
          </SheetHeader>
          {citation && (
            <PdfReferencePanel
              key={viewerKey}
              citation={citation}
              onClose={onClose}
            />
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}
