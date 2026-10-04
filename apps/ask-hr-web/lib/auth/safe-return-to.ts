export function safeReturnTo(value: string | null, appBaseUrl: string): string {
  if (!value) {
    return "/";
  }

  try {
    const baseUrl = new URL(appBaseUrl);
    const target = new URL(value, baseUrl);
    if (target.origin !== baseUrl.origin) {
      return "/";
    }
    return `${target.pathname}${target.search}${target.hash}`;
  } catch {
    return "/";
  }
}
