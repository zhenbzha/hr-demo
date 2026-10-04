import { text } from "node:stream/consumers";

import { NextResponse } from "next/server";

import { getCurrentSession } from "@/lib/auth/session";
import { referencePaths } from "@/lib/references/paths";
import type { AnnotationPage } from "@/lib/references/types";
import { referenceFileSystem } from "@/lib/storage/datalake";

export const runtime = "nodejs";

type AnnotationDocument = {
  documentId: string;
  pages: Record<string, AnnotationPage>;
};

function errorStatus(error: unknown): number | undefined {
  if (
    typeof error === "object" &&
    error !== null &&
    "statusCode" in error &&
    typeof error.statusCode === "number"
  ) {
    return error.statusCode;
  }
  return undefined;
}

function isAnnotationPage(value: unknown): value is AnnotationPage {
  if (!value || typeof value !== "object") {
    return false;
  }
  const page = value as Partial<AnnotationPage>;
  return (
    typeof page.width === "number" &&
    page.width > 0 &&
    typeof page.height === "number" &&
    page.height > 0 &&
    typeof page.unit === "string" &&
    Array.isArray(page.lines) &&
    page.lines.every(
      (line) =>
        !!line &&
        typeof line === "object" &&
        typeof line.text === "string" &&
        Array.isArray(line.polygon) &&
        line.polygon.length >= 8 &&
        line.polygon.every((coordinate) => typeof coordinate === "number") &&
        typeof line.spanOffset === "number" &&
        typeof line.spanLength === "number",
    )
  );
}

function parseAnnotationDocument(value: string): AnnotationDocument {
  const parsed = JSON.parse(value) as unknown;
  if (!parsed || typeof parsed !== "object") {
    throw new Error("Annotation content must be a JSON object");
  }
  const document = parsed as {
    documentId?: unknown;
    pages?: unknown;
  };
  if (
    typeof document.documentId !== "string" ||
    !document.pages ||
    typeof document.pages !== "object" ||
    Array.isArray(document.pages)
  ) {
    throw new Error("Annotation content has an invalid shape");
  }
  const pages = Object.fromEntries(
    Object.entries(document.pages).map(([pageNumber, page]) => {
      if (!isAnnotationPage(page)) {
        throw new Error(`Annotation page ${pageNumber} has an invalid shape`);
      }
      return [pageNumber, page];
    }),
  );
  return { documentId: document.documentId, pages };
}

function pageRange(url: URL) {
  const pageStart = Number(url.searchParams.get("pageStart"));
  const pageEnd = Number(url.searchParams.get("pageEnd"));
  if (
    !Number.isInteger(pageStart) ||
    !Number.isInteger(pageEnd) ||
    pageStart < 1 ||
    pageEnd < pageStart ||
    pageEnd - pageStart > 20
  ) {
    throw new Error("Invalid page range");
  }
  return { pageStart, pageEnd };
}

export async function GET(
  request: Request,
  context: { params: Promise<{ documentId: string; version: string }> },
) {
  const session = await getCurrentSession();
  if (!session?.account) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { documentId, version } = await context.params;
  let annotationPath: string;
  let pages: { pageStart: number; pageEnd: number };
  try {
    annotationPath = referencePaths(documentId, version).annotationPath;
    pages = pageRange(new URL(request.url));
  } catch {
    return NextResponse.json(
      { error: "The reference request is invalid." },
      { status: 400 },
    );
  }

  try {
    const fileClient = referenceFileSystem.getFileClient(annotationPath);
    const download = await fileClient.read();
    if (!download.readableStreamBody) {
      throw new Error("ADLS did not return annotation content");
    }
    const annotation = parseAnnotationDocument(
      await text(download.readableStreamBody),
    );
    if (annotation.documentId !== documentId) {
      throw new Error("Annotation document ID did not match the request");
    }
    const selectedPages = Object.fromEntries(
      Array.from(
        { length: pages.pageEnd - pages.pageStart + 1 },
        (_, index) => pages.pageStart + index,
      )
        .map((pageNumber) => [
          String(pageNumber),
          annotation.pages[String(pageNumber)],
        ])
        .filter(([, page]) => page),
    );

    return NextResponse.json(
      {
        documentId,
        pageStart: pages.pageStart,
        pageEnd: pages.pageEnd,
        pages: selectedPages,
      },
      {
        headers: {
          "Cache-Control": "private, no-store",
        },
      },
    );
  } catch (error) {
    console.error("Reference context request failed", error);
    if (errorStatus(error) === 404) {
      return NextResponse.json(
        { error: "The reference context was not found." },
        { status: 404 },
      );
    }
    return NextResponse.json(
      { error: "The reference context is temporarily unavailable." },
      { status: 502 },
    );
  }
}
