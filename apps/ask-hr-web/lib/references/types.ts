export type RichCitation = {
  label: string;
  location?: string;
  chunkIds?: string[];
  documentId?: string;
  version?: string;
  pageStart?: number;
  pageEnd?: number;
  excerpt?: string;
  region?: string;
  language?: string;
  pdfUrl?: string;
  contextUrl?: string;
};

export type AnnotationLine = {
  text: string;
  polygon: number[];
  spanOffset: number;
  spanLength: number;
};

export type AnnotationPage = {
  width: number;
  height: number;
  unit: string;
  angle?: number;
  lines: AnnotationLine[];
};

export type ReferenceContext = {
  documentId: string;
  pageStart: number;
  pageEnd: number;
  pages: Record<string, AnnotationPage>;
};
