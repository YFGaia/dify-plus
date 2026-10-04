import { zGetAuthCasdoorResultResponse } from '@dify/contracts/api/console/auth/zod.gen'

// The native decoder preserves raw fields. Reject extras before displaying any result.
const resultSchema = zGetAuthCasdoorResultResponse
  .strict()
  .refine((result) =>
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(result.correlation_id),
  )

export function parseSigninResult(raw: unknown) {
  const parsed = resultSchema.safeParse(raw)
  return parsed.success ? parsed.data : undefined
}
