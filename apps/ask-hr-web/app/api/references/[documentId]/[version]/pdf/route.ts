import { Readable } from "node:stream";

import { create as contentDisposition } from "content-disposition";
import { NextResponse } from "next/server";
import parseHttpRange from "range-parser";

import { getCurrentSession } from "@/lib/auth/session";
import { config } from "@/lib/config";
import { referencePaths } from "@/lib/references/paths";
import { referenceFileSystem } from "@/lib/storage/datalake";

export const runtime = "nodejs";
export const maxDuration = 180;

type ByteRange = {
  offset: number;
  count: number;
  end: number;
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

function parseRange(value: string | null, size: number): ByteRange | undefined {
  if (!value) {
    return undefined;
  }
  const parsed = parseHttpRange(size, value, { combine: true });
  if (
    typeof parsed === "number" ||
    parsed.type !== "bytes" ||
    parsed.length !== 1
  ) {
    throw new RangeError("Invalid byte range");
  }
  const [{ start: offset, end }] = parsed;
  return { offset, end, count: end - offset + 1 };
}

async function handle(
  request: Request,
  context: { params: Promise<{ documentId: string; version: string }> },
  includeBody: boolean,
) {
  if (!config.publicDemoEnabled && !(await getCurrentSession())?.account) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { documentId, version } = await context.params;
  let documentPath: string;
  try {
    documentPath = referencePaths(documentId, version).documentPath;
  } catch {
    return NextResponse.json(
      { error: "The document reference is invalid." },
      { status: 400 },
    );
  }

  const fileClient = referenceFileSystem.getFileClient(documentPath);
  try {
    const properties = await fileClient.getProperties();
    const size = properties.contentLength;
    if (typeof size !== "number" || !Number.isInteger(size) || size <= 0) {
      throw new Error("The PDF file has no readable content");
    }
    const etag = properties.etag;
    const lastModified = properties.lastModified?.toUTCString();

    let range: ByteRange | undefined;
    try {
      range = parseRange(request.headers.get("range"), size);
    } catch {
      return new Response(null, {
        status: 416,
        headers: {
          "Accept-Ranges": "bytes",
          "Cache-Control": "private, no-store",
          "Content-Range": `bytes */${size}`,
        },
      });
    }
    const ifRange = request.headers.get("if-range");
    if (range && ifRange && etag && ifRange !== etag) {
      range = undefined;
    }

    const headers = new Headers({
      "Accept-Ranges": "bytes",
      "Cache-Control": "private, no-store",
      "Content-Disposition": contentDisposition(`${documentId}.pdf`, {
        type: "inline",
      }),
      "Content-Length": String(range?.count ?? size),
      "Content-Type": properties.contentType || "application/pdf",
      "Cross-Origin-Resource-Policy": "same-origin",
      "X-Content-Type-Options": "nosniff",
    });
    if (etag) {
      headers.set("ETag", etag);
    }
    if (lastModified) {
      headers.set("Last-Modified", lastModified);
    }
    if (range) {
      headers.set(
        "Content-Range",
        `bytes ${range.offset}-${range.end}/${size}`,
      );
    }
    if (!includeBody) {
      return new Response(null, { status: range ? 206 : 200, headers });
    }

    const download = await fileClient.read(range?.offset, range?.count, {
      conditions: etag ? { ifMatch: etag } : undefined,
    });
    if (!download.readableStreamBody) {
      throw new Error("ADLS did not return a PDF stream");
    }
    return new Response(
      Readable.toWeb(download.readableStreamBody) as ReadableStream,
      {
        status: range ? 206 : 200,
        headers,
      },
    );
  } catch (error) {
    console.error("PDF reference request failed", error);
    if (errorStatus(error) === 404) {
      return NextResponse.json(
        { error: "The referenced PDF was not found." },
        { status: 404 },
      );
    }
    return NextResponse.json(
      { error: "The referenced PDF is temporarily unavailable." },
      { status: 502 },
    );
  }
}

export async function GET(
  request: Request,
  context: { params: Promise<{ documentId: string; version: string }> },
) {
  return handle(request, context, true);
}

export async function HEAD(
  request: Request,
  context: { params: Promise<{ documentId: string; version: string }> },
) {
  return handle(request, context, false);
}
