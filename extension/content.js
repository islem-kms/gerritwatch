/**
 * GerritWatch -- content script
 * ==========================================
 * Runs on Gerrit pages. Fetches the list of flagged changes produced by your
 * Tool 3 monitor script (served locally as flags.json) and annotates matching
 * rows directly in Gerrit's own change table with a small icon, in the same
 * visual style as Gerrit's own CR/RP/V/W status columns. Clicking the icon
 * opens a modal with the description diff and the classifier's reasoning.
 *
 * No floating panel -- annotation happens only inline, in the table itself,
 * per supervisor feedback.
 */

const FLAGS_URL = "http://127.0.0.1:8000/flags.json";
const FEEDBACK_URL = "http://127.0.0.1:8000/feedback.json";
const FEEDBACK_POST_URL = "http://127.0.0.1:8000/feedback";
const POLL_INTERVAL_MS = 60_000; // re-check for new flags once a minute

let flagsByChangeNumber = new Map();

// -----------------------------------------------------------------------
// "Only changes I opened" option (off by default, set in the popup).
// The extension remembers, per change, the latest patchset this browser
// opened. It is stored with chrome.storage.local and never leaves the browser.
// -----------------------------------------------------------------------
const SEEN_KEY = "seenPatchsets";
let onlyOpened = false;
let seenPatchsets = {}; // "<host>:<change number>" -> latest patchset opened

function seenKey(host, changeNumber) {
  return `${host}:${changeNumber}`;
}

// With the option off, every flag is shown. With it on, a flag is shown only
// if the reviewer opened the change at a patchset older than the one whose
// description changed, i.e. the description they read is now outdated.
function shouldShow(flag, host, seen, onlyOpenedOn) {
  if (!onlyOpenedOn) return true;
  const ps = seen[seenKey(flag.gerrit_host || host, flag.change_number)];
  return ps !== undefined && ps < Number(flag.new_patchset);
}

// /[prefix]/c/<project>/+/<number>[/<patchset> | /<a>..<b>][/<file path>]
function parseChangeUrl(pathname) {
  const m = pathname.match(/^(.*?)\/c\/(.+?)\/\+\/(\d+)(?:\/(?:\d+\.\.)?(\d+))?(?:\/|$)/);
  if (!m) return null;
  return { prefix: m[1], project: m[2], changeNumber: m[3], patchset: m[4] ? Number(m[4]) : null };
}

let lastRecordedUrl = null;
async function recordOpenedChange() {
  const href = window.location.href;
  if (href === lastRecordedUrl) return;
  lastRecordedUrl = href;
  const info = parseChangeUrl(window.location.pathname);
  if (!info) return;
  let ps = info.patchset;
  if (ps === null) {
    // No patchset in the URL: Gerrit shows the latest one, so ask Gerrit which it is.
    try {
      const url = `${window.location.origin}${info.prefix}/changes/` +
        `${encodeURIComponent(info.project)}~${info.changeNumber}?o=CURRENT_REVISION`;
      const res = await fetch(url, { credentials: "include" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const change = JSON.parse((await res.text()).replace(/^\)\]\}'\n?/, ""));
      ps = change.revisions[change.current_revision]._number;
    } catch (err) {
      console.warn("[GerritWatch] Could not read the current patchset", err);
      lastRecordedUrl = null; // try again on the next page update
      return;
    }
  }
  const key = seenKey(window.location.hostname, info.changeNumber);
  if (seenPatchsets[key] === undefined || ps > seenPatchsets[key]) {
    seenPatchsets[key] = ps;
    chrome.storage.local.set({ [SEEN_KEY]: seenPatchsets });
  }
  tryInlineBadges();
}

// -----------------------------------------------------------------------
// Fetch flags + feedback from your local monitor server
// -----------------------------------------------------------------------
async function fetchFlags() {
  try {
    const [flagsRes, feedbackRes] = await Promise.all([
      fetch(FLAGS_URL, { cache: "no-store" }),
      fetch(FEEDBACK_URL, { cache: "no-store" }).catch(() => null),
    ]);
    if (!flagsRes.ok) throw new Error(`HTTP ${flagsRes.status}`);
    const data = await flagsRes.json();
    const feedback = feedbackRes && feedbackRes.ok ? await feedbackRes.json() : {};

    flagsByChangeNumber = new Map(
      data.map((f) => [
        String(f.change_number),
        { ...f, feedback: feedback[f.flag_id] || { detection: null, summary: null } },
      ])
    );
    console.log(`[GerritWatch] Loaded ${flagsByChangeNumber.size} flag(s):`, [...flagsByChangeNumber.keys()]);
    tryInlineBadges();
  } catch (err) {
    // Most likely cause: the local server (Step 2) isn't running.
    console.warn("[GerritWatch] Could not fetch flags.json -- is your local server running on :8000?", err);
  }
}

// -----------------------------------------------------------------------
// Submit a thumbs up/down. Clicking an already-active thumb clears it
// (toggles back to no opinion) rather than forcing a choice.
// -----------------------------------------------------------------------
async function submitFeedback(flagId, feedbackType, value) {
  try {
    const res = await fetch(FEEDBACK_POST_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ flag_id: flagId, feedback_type: feedbackType, value }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (err) {
    console.warn("[GerritWatch] Failed to submit feedback", err);
  }
  await fetchFlags(); // refresh so the UI reflects the saved state
}

function thumbsHtml(flagId, current) {
  const makeButtons = (type, current) => `
      <button class="sdm-thumb sdm-thumb-up ${current === "up" ? "active" : ""}" data-flag-id="${escapeHtml(flagId)}" data-type="${escapeHtml(type)}" data-value="up" title="Helpful">👍</button>
      <button class="sdm-thumb sdm-thumb-down ${current === "down" ? "active" : ""}" data-flag-id="${escapeHtml(flagId)}" data-type="${escapeHtml(type)}" data-value="down" title="Not helpful">👎</button>`;
  return `
    <div class="sdm-feedback" onclick="event.stopPropagation()">
      <div class="sdm-feedback-row">
        <span class="sdm-feedback-label">Detection correct?</span>
        ${makeButtons("detection", current.detection)}
      </div>
      <div class="sdm-feedback-row">
        <span class="sdm-feedback-label">Summary helpful?</span>
        ${makeButtons("summary", current.summary)}
      </div>
    </div>`;
}

function attachFeedbackHandlers(container) {
  container.querySelectorAll(".sdm-thumb").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const { flagId, type, value } = btn.dataset;
      const isActive = btn.classList.contains("active");
      // update the open popup at once: this button toggles, its partner in the row is cleared
      btn.parentElement.querySelectorAll(".sdm-thumb").forEach((b) => b.classList.remove("active"));
      if (!isActive) btn.classList.add("active");
      submitFeedback(flagId, type, isActive ? null : value); // click again to clear
    });
  });
}

// -----------------------------------------------------------------------
// Detail modal, opened by clicking a row's icon
// -----------------------------------------------------------------------
function openDetail(f) {
  let modal = document.getElementById("sdm-modal");
  if (modal) modal.remove();

  modal = document.createElement("div");
  modal.id = "sdm-modal";
  modal.innerHTML = `
    <div class="sdm-modal-box">
      <div class="sdm-modal-head">
        <div>
          <div class="sdm-modal-title">${escapeHtml(f.subject)}</div>
          <div class="sdm-modal-sub">change #${escapeHtml(f.change_number)} · patchset ${escapeHtml(f.old_patchset)} → ${escapeHtml(f.new_patchset)}</div>
        </div>
        <button class="sdm-close">✕</button>
      </div>
      <div class="sdm-modal-body">
        <div class="sdm-label">Description change</div>
        <div class="sdm-diff">${wordDiff(f.old_description || "", f.new_description || "")}</div>
        <div class="sdm-label">Why this was flagged</div>
        <div class="sdm-reason">${escapeHtml(f.reason || "")}</div>
        ${thumbsHtml(f.flag_id, f.feedback || { detection: null, summary: null })}
        <a class="sdm-modal-cta" href="${escapeHtml(f.change_url || "#")}" target="_blank" rel="noopener">Open this change in Gerrit ↗</a>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  modal.querySelector(".sdm-close").addEventListener("click", () => modal.remove());
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.remove(); });
  attachFeedbackHandlers(modal);
}

// -----------------------------------------------------------------------
// Best-effort inline annotation, positioned as a floating overlay aligned
// to each matching row -- NOT inserted into Gerrit's own DOM.
//
// Why: this Gerrit instance is PolyGerrit, which renders inside Shadow DOM
// and (being Lit-based) actively re-renders/diffs its own shadow tree. Two
// consequences if we insert elements directly into that tree:
//   1. Outside stylesheets (our content.css) cannot reach inside shadow
//      DOM at all -- confirmed by testing: the icon rendered as unstyled
//      plain text.
//   2. Lit can wipe out foreign nodes we inserted on its next re-render,
//      which explained the inconsistent click behavior.
// Solution: never touch Gerrit's DOM. Instead, read the matching row's
// on-screen position (getBoundingClientRect) and draw our own icon at
// that position, as a `position: fixed` element living in the top-level
// document -- fully outside any shadow boundary, fully styleable, and
// immune to Gerrit's own re-renders.
// -----------------------------------------------------------------------
let activeIcons = new Map(); // changeNumber -> { el, anchor }

function positionIcon(entry) {
  const rect = entry.anchor.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) {
    entry.el.style.display = "none"; // anchor currently off-screen/unrendered
    return;
  }
  entry.el.style.display = "flex";
  entry.el.style.top = `${rect.top + rect.height / 2 - 9}px`;
  // Stay inside the subject cell: right after the text when there is room,
  // otherwise on the cell's right edge, so the icon never covers the Owner column.
  const cell = enclosingCell(entry.anchor);
  const maxLeft = cell ? cell.getBoundingClientRect().right - 20 : Infinity;
  entry.el.style.left = `${Math.min(rect.right + 6, maxLeft)}px`;
}

// Nearest table cell around the link, crossing shadow-root boundaries.
function enclosingCell(node) {
  let n = node;
  for (let k = 0; n && k < 10; k++) {
    if (n.tagName === "TD") return n;
    n = n.parentNode instanceof ShadowRoot ? n.parentNode.host : n.parentNode;
  }
  return null;
}

function repositionAllIcons() {
  activeIcons.forEach(positionIcon);
}

function tryInlineBadges() {
  if (flagsByChangeNumber.size === 0) {
    activeIcons.forEach((entry) => entry.el.remove());
    activeIcons.clear();
    return;
  }

  // NOTE: project segment uses `.+` (not `[^/]+`), because many real Gerrit
  // projects are nested paths containing their own slashes (e.g.
  // "policy/clamp", "aai/sparky-be"). END-ANCHORED (`$`) so a per-file
  // diff link (which has extra segments after the number) never matches --
  // only the change's own subject/commit-message link does.
  const CHANGE_LINK_RE = /\/c\/.+\/\+\/(\d+)\/?$/;

  const anchors = collectAllAnchors(document);
  const foundNumbers = new Set();

  anchors.forEach((a) => {
    let pathname;
    try {
      pathname = a.pathname || new URL(a.href, window.location.href).pathname;
    } catch (e) {
      return;
    }
    const match = pathname.match(CHANGE_LINK_RE);
    if (!match) return;
    const changeNumber = match[1];
    const flag = flagsByChangeNumber.get(changeNumber);
    if (!flag) return;
    if (flag.gerrit_host && flag.gerrit_host !== window.location.hostname) return;
    if (!shouldShow(flag, window.location.hostname, seenPatchsets, onlyOpened)) return;
    if (foundNumbers.has(changeNumber)) return; // one icon per change even if the link appears twice
    foundNumbers.add(changeNumber);

    let entry = activeIcons.get(changeNumber);
    if (!entry) {
      const el = document.createElement("div");
      el.className = "sdm-fixed-icon";
      el.textContent = "!";
      el.title = truncate(flag.reason, 140);
      el.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        openDetail(flag);
      });
      document.body.appendChild(el);
      entry = { el, anchor: a };
      activeIcons.set(changeNumber, entry);
    } else {
      entry.anchor = a; // Gerrit may have re-rendered a new anchor node for the same change
    }
    positionIcon(entry);
  });

  // Remove icons for flags no longer visible on the page (e.g. pagination changed)
  activeIcons.forEach((entry, num) => {
    if (!foundNumbers.has(num)) {
      entry.el.remove();
      activeIcons.delete(num);
    }
  });
}

window.addEventListener("scroll", repositionAllIcons, true);
window.addEventListener("resize", repositionAllIcons);

// Recursively collect anchors, including inside OPEN shadow roots.
// (Closed shadow roots are intentionally unreachable by design -- if a
// Gerrit instance uses those, strategy 1's floating panel is the fallback.)
function collectAllAnchors(root, acc = []) {
  root.querySelectorAll("a[href]").forEach((a) => acc.push(a));
  root.querySelectorAll("*").forEach((el) => {
    if (el.shadowRoot) collectAllAnchors(el.shadowRoot, acc);
  });
  return acc;
}

// -----------------------------------------------------------------------
// Small utilities
// -----------------------------------------------------------------------
function escapeHtml(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function truncate(s, n) {
  if (!s) return "";
  return s.length > n ? s.slice(0, n) + "…" : s;
}
function wordDiff(oldText, newText) {
  const a = oldText.split(/(\s+)/);
  const b = newText.split(/(\s+)/);
  const dp = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  let i = 0, j = 0, out = "";
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { out += escapeHtml(a[i]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out += `<del>${escapeHtml(a[i])}</del>`; i++; }
    else { out += `<ins>${escapeHtml(b[j])}</ins>`; j++; }
  }
  while (i < a.length) { out += `<del>${escapeHtml(a[i])}</del>`; i++; }
  while (j < b.length) { out += `<ins>${escapeHtml(b[j])}</ins>`; j++; }
  return out;
}

// -----------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------
chrome.storage.local.get({ onlyOpened: false, [SEEN_KEY]: {} }, (s) => {
  onlyOpened = Boolean(s.onlyOpened);
  seenPatchsets = s[SEEN_KEY] || {};
  recordOpenedChange();
  fetchFlags();
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.onlyOpened) onlyOpened = Boolean(changes.onlyOpened.newValue);
  if (changes[SEEN_KEY]) seenPatchsets = changes[SEEN_KEY].newValue || {};
  tryInlineBadges();
});
setInterval(fetchFlags, POLL_INTERVAL_MS);

// Re-scan for inline badges when the page's SPA content changes
// (PolyGerrit re-renders rows as you scroll/navigate), and record the
// change and patchset when the reviewer navigates to a change page.
const observer = new MutationObserver(() => {
  recordOpenedChange();
  tryInlineBadges();
});
observer.observe(document.body, { childList: true, subtree: true });
