import DOMPurify from "dompurify";
import katex from "katex";
import { marked } from "marked";

marked.setOptions({ gfm: true, breaks: true });

const MATH = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$([^\s$](?:[^$\n]*?[^\s$])?)\$/g;

/** Model answer → safe HTML with KaTeX formulas and clickable [n] citations. */
export function renderAnswer(text: string): string {
  const math: string[] = [];
  const withSlots = text.replace(MATH, (_m, d1, d2, i1, i2) => {
    const display = d1 !== undefined || d2 !== undefined;
    const expr = d1 ?? d2 ?? i1 ?? i2;
    math.push(
      katex.renderToString(expr, { displayMode: display, throwOnError: false, output: "html" }),
    );
    return `@@M${math.length - 1}@@`;
  });

  const html = DOMPurify.sanitize(marked.parse(withSlots, { async: false }) as string);

  return html
    .replace(/@@M(\d+)@@/g, (_m, i) => math[Number(i)] ?? "")
    .replace(/\[(\d{1,2}(?:\s*[,;–-]\s*\d{1,2})*)\]/g, (_m, list: string) => citeButtons(list));
}

/** "1, 6" → two buttons; "2-4" → buttons 2, 3, 4. */
function citeButtons(list: string): string {
  const nums: number[] = [];
  for (const part of list.split(/\s*[,;]\s*/)) {
    const [a, b] = part.split(/\s*[–-]\s*/).map(Number);
    if (b && b > a && b - a < 10) for (let n = a; n <= b; n++) nums.push(n);
    else nums.push(a);
  }
  return nums.map((n) => `<button class="cite" data-cite="${n}">${n}</button>`).join("");
}

/** Highlight query words in a snippet (returns safe HTML). */
export function highlight(text: string, query: string): string {
  const esc = (s: string) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const terms = query
    .toLowerCase()
    .split(/[^\p{L}\p{N}]+/u)
    .filter((t) => t.length >= 3)
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!terms.length) return esc(text);
  const re = new RegExp(`(${terms.join("|")})`, "giu");
  return esc(text).replace(re, "<mark>$1</mark>");
}

/** Strip Markdown/Obsidian syntax so note previews read as plain text. */
function plain(text: string): string {
  return text
    .replace(/\[!\w+\]-?/g, "")
    .replace(/\*\*|__|`|\$\$|(^|\s)>\s?/g, "$1")
    .replace(/(^|\s)#{1,6}\s/g, "$1")
    .replace(/\s{2,}/g, " ")
    .trim();
}

/** Take a window of the chunk around the first query term. */
export function snippet(raw: string, query: string, len = 420): string {
  const text = plain(raw);
  const terms = query
    .toLowerCase()
    .split(/[^\p{L}\p{N}]+/u)
    .filter((t) => t.length >= 4);
  const lower = text.toLowerCase();
  const idx = Math.min(...terms.map((t) => lower.indexOf(t)).filter((i) => i >= 0), Infinity);
  if (!isFinite(idx) || idx < len * 0.6) {
    return text.length > len ? text.slice(0, len).replace(/\s+\S*$/, "") + " …" : text;
  }
  const start = text.lastIndexOf(" ", Math.max(0, idx - 140)) + 1;
  const out = text.slice(start, start + len).replace(/\s+\S*$/, "");
  return "… " + out + (start + len < text.length ? " …" : "");
}
