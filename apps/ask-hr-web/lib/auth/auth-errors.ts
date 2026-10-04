export const authErrorMessages = {
  callback_failed: "Sign-in could not be completed. Please try again.",
  provider_error: "Microsoft Entra could not complete sign-in.",
  session_expired: "The sign-in session expired. Please try again.",
  validation_failed: "The sign-in response could not be validated.",
} as const;

export type AuthErrorCode = keyof typeof authErrorMessages;

export function authErrorMessage(code: string | undefined): string | undefined {
  return code && code in authErrorMessages
    ? authErrorMessages[code as AuthErrorCode]
    : undefined;
}
