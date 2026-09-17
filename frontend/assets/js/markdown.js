// Minimal, safe-by-construction Markdown -> HTML renderer for untrusted
// Agent/model answer text (MA5-UI Post-UAT Enhancement 2). Deliberately
// NOT a spec-complete CommonMark implementation, and deliberately does
// NOT support raw HTML passthrough the way most Markdown flavors do --
// Agent/model output is untrusted content ("Security is mandatory"), so
// every literal character from the source is treated as plain text and
// HTML-escaped; the only markup this module ever emits is the small,
// fixed set of tags it builds itself. There is no HTML *parsing* step
// here and nothing ever does `element.innerHTML = <unescaped source>` --
// the string this returns is safe to assign to innerHTML precisely
// because of how it was constructed (allow-list generation), never
// because something filtered it afterwards.
//
// Supported: headings, paragraphs, bold, italics, ordered/unordered
// lists, inline code, fenced code blocks, blockquotes, links. Links are
// only ever emitted as a real <a href> when the URL scheme is http(s)/
// mailto; anything else (javascript:, data:, vbscript:, ...) degrades to
// plain text -- never a clickable/executable href. Pure functions only,
// no DOM/browser API used, so this runs identically in the browser and
// under plain Node (see frontend/tests/markdown.test.mjs).

const SAFE_LINK_SCHEME = /^(https?:|mailto:)/i;
const PLACEHOLDER_OPEN = "";
const PLACEHOLDER_CLOSE = "";
const PLACEHOLDER_RE = new RegExp(`${PLACEHOLDER_OPEN}(\\d+)${PLACEHOLDER_CLOSE}`, "g");

export function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function isSafeLinkUrl(url) {
  return SAFE_LINK_SCHEME.test(String(url).trim());
}

function renderInline(text) {
  const placeholders = [];
  const stash = (html) => {
    const token = `${PLACEHOLDER_OPEN}${placeholders.length}${PLACEHOLDER_CLOSE}`;
    placeholders.push(html);
    return token;
  };

  // 1) Code spans, read from the RAW source -- their content is never
  // treated as further markdown and is escaped exactly once.
  let working = String(text).replace(/`([^`]+)`/g, (_, code) => stash(`<code>${escapeHtml(code)}</code>`));

  // 2) Links -- validate the scheme before ever emitting an <a>; label
  // text is recursively rendered so bold/italic/code still work inside it.
  working = working.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, url) => {
    const safeLabel = renderInline(label);
    if (!isSafeLinkUrl(url)) return stash(safeLabel);
    return stash(`<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${safeLabel}</a>`);
  });

  // 3) Escape everything left -- only literal text and placeholder
  // tokens (which contain no HTML-special characters) remain now.
  working = escapeHtml(working);

  // 4) Emphasis, applied to the already-escaped text.
  working = working
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/_([^_]+)_/g, "<em>$1</em>");

  // 5) Restore code/link placeholders with their pre-built safe HTML.
  return working.replace(PLACEHOLDER_RE, (_, i) => placeholders[Number(i)]);
}

function renderParagraphLines(lines) {
  return lines
    .map((line) => {
      const hardBreak = /( {2,}|\\)$/.test(line);
      return renderInline(line.trim()) + (hardBreak ? "<br>" : "");
    })
    .join(" ");
}

export function renderMarkdown(source) {
  const lines = String(source || "").replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let i = 0;

  const isBlockStart = (l) =>
    /^\s*$/.test(l) || /^```/.test(l) || /^#{1,6}\s+/.test(l) || /^>\s?/.test(l) || /^[-*+]\s+/.test(l) || /^\d+\.\s+/.test(l);

  while (i < lines.length) {
    const line = lines[i];

    if (/^\s*$/.test(line)) {
      i += 1;
      continue;
    }

    const fenceMatch = /^```(\w*)\s*$/.exec(line);
    if (fenceMatch) {
      const codeLines = [];
      i += 1;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        codeLines.push(lines[i]);
        i += 1;
      }
      i += 1; // skip closing fence, or reach EOF if the fence was never closed
      const lang = fenceMatch[1] ? ` data-lang="${escapeHtml(fenceMatch[1])}"` : "";
      html.push(`<pre${lang}><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
      continue;
    }

    const headingMatch = /^(#{1,6})\s+(.*)$/.exec(line);
    if (headingMatch) {
      const level = headingMatch[1].length;
      html.push(`<h${level}>${renderInline(headingMatch[2].trim())}</h${level}>`);
      i += 1;
      continue;
    }

    if (/^>\s?/.test(line)) {
      const quoteLines = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        quoteLines.push(lines[i].replace(/^>\s?/, ""));
        i += 1;
      }
      html.push(`<blockquote><p>${renderParagraphLines(quoteLines)}</p></blockquote>`);
      continue;
    }

    if (/^[-*+]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^[-*+]\s+/.test(lines[i])) {
        items.push(renderInline(lines[i].replace(/^[-*+]\s+/, "").trim()));
        i += 1;
      }
      html.push(`<ul>${items.map((it) => `<li>${it}</li>`).join("")}</ul>`);
      continue;
    }

    if (/^\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
        items.push(renderInline(lines[i].replace(/^\d+\.\s+/, "").trim()));
        i += 1;
      }
      html.push(`<ol>${items.map((it) => `<li>${it}</li>`).join("")}</ol>`);
      continue;
    }

    const paraLines = [];
    while (i < lines.length && !isBlockStart(lines[i])) {
      paraLines.push(lines[i]);
      i += 1;
    }
    html.push(`<p>${renderParagraphLines(paraLines)}</p>`);
  }

  return html.join("\n");
}
