/* Web Audio notification sound — plays only when the tab is hidden. */

let ctx: AudioContext | null = null;

function getCtx(): AudioContext | null {
  if (typeof window === "undefined") return null;
  if (!ctx) {
    try {
      ctx = new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
    } catch {
      return null;
    }
  }
  if (ctx.state === "suspended") void ctx.resume();
  return ctx;
}

export function playNotificationSound(volume: number): void {
  // не играем, если пользователь смотрит на вкладку
  if (typeof document !== "undefined" && document.visibilityState === "visible") return;
  const audio = getCtx();
  if (!audio) return;

  const t = audio.currentTime;
  const osc = audio.createOscillator();
  const gain = audio.createGain();

  osc.type = "sine";
  osc.frequency.setValueAtTime(880, t);          // A5
  osc.frequency.setValueAtTime(1320, t + 0.08);  // E6

  gain.gain.setValueAtTime(0.0001, t);
  gain.gain.exponentialRampToValueAtTime(Math.max(0.001, volume * 0.3), t + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.25);

  osc.connect(gain);
  gain.connect(audio.destination);
  osc.start(t);
  osc.stop(t + 0.3);
}
