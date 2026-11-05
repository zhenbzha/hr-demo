// Accept generated UUIDs and existing IDs without permitting path separators.
const documentIdPattern = /^[A-Za-z0-9][A-Za-z0-9-]{1,63}$/;
const versionPattern = /^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$/;

export function referencePaths(documentId: string, version: string) {
  if (!documentIdPattern.test(documentId)) {
    throw new Error("Invalid document ID");
  }
  if (!versionPattern.test(version)) {
    throw new Error("Invalid document version");
  }
  return {
    documentPath: `documents/${documentId}/${version}.pdf`,
    annotationPath: `annotations/${documentId}/${version}.json`,
  };
}
