import { test } from "node:test";
import assert from "node:assert/strict";
import { renderMarkdown, escapeHtml, isSafeLinkUrl } from "../assets/js/markdown.js";

test("renders headings h1 through h6", () => {
  for (let level = 1; level <= 6; level++) {
    const html = renderMarkdown(`${"#".repeat(level)} Title ${level}`);
    assert.equal(html, `<h${level}>Title ${level}</h${level}>`);
  }
});

test("renders paragraphs, separated by a blank line", () => {
  const html = renderMarkdown("First paragraph.\n\nSecond paragraph.");
  assert.equal(html, "<p>First paragraph.</p>\n<p>Second paragraph.</p>");
});

test("renders bold with ** and __", () => {
  assert.equal(renderMarkdown("**bold**"), "<p><strong>bold</strong></p>");
  assert.equal(renderMarkdown("__bold__"), "<p><strong>bold</strong></p>");
});

test("renders italics with * and _", () => {
  assert.equal(renderMarkdown("*italic*"), "<p><em>italic</em></p>");
  assert.equal(renderMarkdown("_italic_"), "<p><em>italic</em></p>");
});

test("renders an unordered list from -, *, or +", () => {
  const expected = "<ul><li>one</li><li>two</li><li>three</li></ul>";
  assert.equal(renderMarkdown("- one\n- two\n- three"), expected);
  assert.equal(renderMarkdown("* one\n* two\n* three"), expected);
  assert.equal(renderMarkdown("+ one\n+ two\n+ three"), expected);
});

test("renders an ordered list", () => {
  assert.equal(renderMarkdown("1. one\n2. two\n3. three"), "<ol><li>one</li><li>two</li><li>three</li></ol>");
});

test("renders inline code without applying emphasis rules inside it", () => {
  const html = renderMarkdown("Use `**not bold**` here.");
  assert.equal(html, "<p>Use <code>**not bold**</code> here.</p>");
});

test("renders a fenced code block, preserving whitespace/indentation", () => {
  const source = "```python\ndef f(x):\n    return x + 1\n```";
  const html = renderMarkdown(source);
  assert.equal(html, '<pre data-lang="python"><code>def f(x):\n    return x + 1</code></pre>');
});

test("fenced code block content is never itself interpreted as markdown", () => {
  const html = renderMarkdown("```\n**not bold** # not a heading\n```");
  assert.match(html, /<pre><code>\*\*not bold\*\* # not a heading<\/code><\/pre>/);
});

test("renders a blockquote", () => {
  const html = renderMarkdown("> quoted text");
  assert.equal(html, "<blockquote><p>quoted text</p></blockquote>");
});

test("a complete multi-construct answer preserves every distinct piece of content", () => {
  const source = [
    "## Summary",
    "",
    "Paris is the **capital** of France.",
    "",
    "- Population: large",
    "- Founded: ancient",
    "",
    "```js",
    "console.log('ok');",
    "```",
    "",
    "> A closing remark.",
  ].join("\n");
  const html = renderMarkdown(source);
  for (const fragment of ["Summary", "capital", "Population: large", "Founded: ancient", "console.log('ok');", "A closing remark."]) {
    const present = html.includes(fragment) || html.includes(escapeHtml(fragment));
    assert.ok(present, `expected output to contain "${fragment}"`);
  }
});

// -- Security: untrusted model/Agent output ---------------------------------

test("never lets a literal <script> tag through as an actual element", () => {
  const html = renderMarkdown("Ignore this: <script>alert('xss')</script>");
  assert.ok(!/<script/i.test(html), "output must not contain a live <script> tag");
  assert.ok(html.includes("&lt;script&gt;"), "the literal text should be escaped, not dropped silently");
});

test("never lets an <img onerror=...> become a live element/attribute", () => {
  const html = renderMarkdown('<img src=x onerror="alert(1)">');
  // The whole tag must be escaped to inert text (angle brackets/quotes
  // neutralized) -- "onerror=" surviving as plain, non-attribute text is
  // safe and expected; a live <img ...> element would not be.
  assert.ok(!/<img/i.test(html), "output must not contain a live <img> element from source text");
  assert.ok(html.includes("&lt;img"), "the literal text should be escaped, not silently dropped");
});

test("never lets an inline style/expression or iframe through", () => {
  const html = renderMarkdown('<iframe src="javascript:alert(1)"></iframe>');
  assert.ok(!/<iframe/i.test(html));
});

test("escapeHtml neutralizes every HTML-special character", () => {
  assert.equal(escapeHtml(`<>&"'`), "&lt;&gt;&amp;&quot;&#39;");
});

// -- Links --------------------------------------------------------------------

test("renders a safe http(s) link with target=_blank and rel=noopener noreferrer", () => {
  const html = renderMarkdown("[docs](https://example.com/page)");
  assert.equal(html, '<p><a href="https://example.com/page" target="_blank" rel="noopener noreferrer">docs</a></p>');
});

test("renders a safe mailto link", () => {
  const html = renderMarkdown("[email me](mailto:a@example.com)");
  assert.match(html, /^<p><a href="mailto:a@example\.com"/);
});

test("isSafeLinkUrl accepts only http(s)/mailto", () => {
  assert.equal(isSafeLinkUrl("https://example.com"), true);
  assert.equal(isSafeLinkUrl("http://example.com"), true);
  assert.equal(isSafeLinkUrl("mailto:a@example.com"), true);
  assert.equal(isSafeLinkUrl("javascript:alert(1)"), false);
  assert.equal(isSafeLinkUrl("data:text/html,<script>alert(1)</script>"), false);
  assert.equal(isSafeLinkUrl("vbscript:msgbox(1)"), false);
});

test("a javascript: link never becomes a clickable href -- only its label text survives", () => {
  const html = renderMarkdown("[click me](javascript:alert(1))");
  assert.ok(!/<a /.test(html), "must not render an <a> tag for an unsafe scheme");
  assert.ok(!/href/.test(html));
  assert.ok(html.includes("click me"));
});

test("a data: link never becomes a clickable href", () => {
  const html = renderMarkdown("[open](data:text/html,<script>alert(1)</script>)");
  assert.ok(!/<a /.test(html));
  assert.ok(!/<script/i.test(html));
});
