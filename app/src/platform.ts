// Keyboard conventions: ⌘ on the Mac where Windows uses Ctrl.
export const isMac = navigator.userAgent.includes("Mac");

/** Running inside the desktop app (not a plain browser during development). */
export const inTauri = "__TAURI_INTERNALS__" in window;

/** The primary modifier is held (⌘ on macOS, Ctrl elsewhere). */
export const modKey = (e: { metaKey: boolean; ctrlKey: boolean }) => (isMac ? e.metaKey : e.ctrlKey);

export const MOD = isMac ? "⌘" : "Ctrl";
export const ALT = isMac ? "⌥" : "Alt";
export const SHIFT = isMac ? "⇧" : "Shift";

/** Global shortcut that shows/hides the window (registered in src-tauri/src/lib.rs). */
export const TOGGLE_KEYS = [MOD, SHIFT, "Space"];

/** Native folder picker; null if the user cancels. */
export async function pickFolder(current?: string): Promise<string | null> {
  const { open } = await import("@tauri-apps/plugin-dialog");
  const picked = await open({
    directory: true,
    defaultPath: current || undefined,
    title: "Folder with your study materials",
  });
  return typeof picked === "string" ? picked : null;
}
