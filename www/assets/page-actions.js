// "Copy page": put the page's Markdown twin on the clipboard.
// Listeners are delegated from the document, because instant navigation swaps
// the content without re-running scripts.
const pages = new Map();

function markdown(url) {
  if (!pages.has(url)) {
    const text = fetch(url).then((response) => {
      if (!response.ok) throw new Error(`${url}: ${response.status}`);
      return response.text();
    });
    text.catch(() => pages.delete(url));
    pages.set(url, text);
  }
  return pages.get(url);
}

// Fetched on hover or focus, so the text is usually in hand by the click and
// can be written while the click still counts as a user gesture.
for (const type of ["pointerover", "focusin"]) {
  document.addEventListener(type, (event) => {
    const button = event.target.closest?.("[data-copy-markdown]");
    if (button) markdown(button.dataset.copyMarkdown).catch(() => {});
  });
}

// The fallback when the async clipboard is refused (an embedded frame, an old
// browser): a selected textarea and execCommand, as the theme's code-copy
// button does.
function copyBySelection(text) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.append(area);
  area.select();
  const ok = document.execCommand("copy");
  area.remove();
  if (!ok) throw new Error("copy refused");
}

async function copy(url) {
  const text = markdown(url);
  try {
    if (typeof ClipboardItem === "function") {
      // A ClipboardItem built from a promise keeps the gesture across the
      // fetch, which Safari requires.
      const blob = text.then((body) => new Blob([body], { type: "text/plain" }));
      await navigator.clipboard.write([new ClipboardItem({ "text/plain": blob })]);
    } else {
      await navigator.clipboard.writeText(await text);
    }
  } catch {
    copyBySelection(await text);
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy-markdown]");
  if (!button) return;
  const label = button.dataset.label || (button.dataset.label = button.textContent);
  copy(button.dataset.copyMarkdown).then(
    () => { button.textContent = "Copied"; },
    () => { button.textContent = "Copy failed"; },
  ).finally(() => setTimeout(() => { button.textContent = label; }, 2000));
});
