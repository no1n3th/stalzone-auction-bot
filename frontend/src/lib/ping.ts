/** F-10: single ping-filter implementation — the test used to check its own copy. */
export function isPing(d: unknown): boolean {
  return (
    typeof d === "object" && d !== null && "type" in d && (d as { type: unknown }).type === "ping"
  );
}
