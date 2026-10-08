// Frontend mirror of the backend merchant normalizers
// (bank_sync.normalize_merchant_name / finance._normalize_merchant): lowercase,
// strip punctuation to spaces, collapse whitespace, drop a trailing " com" so
// "Netflix.com" collapses onto "Netflix". Used only as a matching key for the
// merchant-detail view — transactions already carry `normalized_merchant`.
export function normalizeMerchant(name: string | null | undefined): string {
  if (!name) return "";
  let s = name.toLowerCase().replace(/[^a-z0-9 ]+/g, " ");
  s = s.replace(/\s+/g, " ").trim();
  if (s.endsWith(" com")) s = s.slice(0, -4).trim();
  return s;
}

// True when a route param identifies the same merchant as a candidate string.
// Exact normalized match first; then a substring match (min 4 chars) so
// "netflix" still finds "netflix subscription" and vice versa.
export function merchantMatches(
  param: string,
  candidate: string | null | undefined,
): boolean {
  const a = normalizeMerchant(param);
  const b = normalizeMerchant(candidate);
  if (!a || !b) return false;
  if (a === b) return true;
  const shorter = a.length <= b.length ? a : b;
  const longer = a.length <= b.length ? b : a;
  return shorter.length >= 4 && longer.includes(shorter);
}

export function merchantPath(name: string): string {
  return `/merchant/${encodeURIComponent(name)}`;
}
