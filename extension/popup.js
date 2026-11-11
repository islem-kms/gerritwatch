// Popup with the "Only changes I opened" option. content.js listens to
// chrome.storage changes, so open Gerrit tabs update without a reload.
const box = document.getElementById("onlyOpened");
chrome.storage.local.get({ onlyOpened: false }, (s) => {
  box.checked = Boolean(s.onlyOpened);
});
box.addEventListener("change", () => {
  chrome.storage.local.set({ onlyOpened: box.checked });
});
