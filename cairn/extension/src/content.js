// Runs on BI pages. Reports the page URL to the service worker, again on every in-app navigation
// (Looker, Tableau and Power BI are single-page apps). Reads nothing from the page itself.
let last = '';
function report() {
    if (location.href === last) return;
    last = location.href;
    chrome.runtime.sendMessage({ type: 'cairn:page', url: location.href }).catch(() => {});
}
report();
setInterval(report, 1000);
window.addEventListener('hashchange', report);
window.addEventListener('popstate', report);
