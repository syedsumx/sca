// Turns a lookup result into the panel's HTML. Pure (no browser APIs) so it can be tested.

const esc = (s) =>
    String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const who = (urn) => esc(String(urn).split(':').pop());
const QUALITY = {
    HEALTHY: ['good', 'Healthy'],
    WARNING: ['warn', 'Warning'],
    FAILING: ['bad', 'Failing'],
    ERROR: ['bad', 'Check error'],
    LEARNING: ['none', 'Learning'],
};

export function render(result) {
    switch (result.status) {
        case 'unconfigured':
            return '<p>Set your Cairn address in the <a href="options.html" target="_blank">extension options</a>.</p>';
        case 'not-bi':
            return '<p class="muted">Open a Looker, Tableau or Power BI dashboard to see what the catalog knows about it.</p>';
        case 'signed-out':
            return `<p>Sign in to Cairn to see this dashboard's owners and trust signals.</p><p><a href="${esc(result.login)}" target="_blank">Sign in ↗</a></p>`;
        case 'unknown':
            return `<p>This ${esc(result.page?.kind || 'page')} is not in the catalog yet.</p><p class="muted">${esc(result.page?.key)}</p>`;
        case 'error':
            return `<p class="bad">${esc(result.message)}</p>`;
        default:
            break;
    }
    const c = result.data.card;
    const [cls, label] = QUALITY[c.quality_status] || ['none', 'Not monitored'];
    const certified = (c.certification || '').toUpperCase() === 'CERTIFIED';
    const monitors = (result.data.monitors || [])
        .map((m) => `<li>${esc(m.name)}: <span class="${m.status === 'FAIL' ? 'bad' : m.status === 'PASS' ? 'good' : 'none'}">${esc(m.status || 'not run')}</span></li>`)
        .join('');
    return [
        `<h1>${esc(c.name)}</h1>`,
        `<div class="muted">${esc(c.type || '')}${c.platform ? ` · ${esc(String(c.platform).split(':').pop())}` : ''}</div>`,
        '<div class="pills">',
        certified ? '<span class="pill good">✔ Certified</span>' : '',
        `<span class="pill ${cls}">● ${label}</span>`,
        c.freshness?.last_updated ? `<span class="pill none">⏱ updated ${esc(c.freshness.last_updated)}</span>` : '',
        '</div>',
        c.description ? `<p>${esc(c.description)}</p>` : '<p class="muted">No description yet.</p>',
        '<dl>',
        `<dt>Owners</dt><dd>${(c.owners || []).map(who).join(', ') || '<span class="muted">none</span>'}</dd>`,
        '</dl>',
        monitors ? `<h3>Checks</h3><ul>${monitors}</ul>` : '',
    ].join('');
}
