/** The text of the last QR code scanned, handed from the scan screen back to the connect screen. */
let pending: string | null = null;
export const scanned = {
  set(text: string) { pending = text; },
  take(): string | null { const t = pending; pending = null; return t; },
};
