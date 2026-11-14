/**
 * iframe page renderer for `/embed/v1/frame/{card|quality|lineage}?urn=…&token=…[&theme=dark]`.
 * Used where BI tools can only embed a URL (Tableau dashboard extensions, Power BI web content,
 * Looker tiles). The backend serves a minimal HTML shell that loads this script.
 */
import { defineElements } from './elements';

export const FRAME_KINDS: Record<string, string> = {
    card: 'cairn-asset-card',
    quality: 'cairn-quality-badge',
    lineage: 'cairn-lineage',
};

export function renderFrame(doc: Document, location: { pathname: string; search: string }): HTMLElement | null {
    const params = new URLSearchParams(location.search);
    const kind = params.get('kind') || location.pathname.split('/').filter(Boolean).pop() || 'card';
    const tag = FRAME_KINDS[kind];
    const target = doc.getElementById('cairn-embed') ?? doc.body;
    if (!tag) {
        target.textContent = `Unknown embed kind “${kind}”.`;
        return null;
    }
    const node = doc.createElement(tag);
    ['urn', 'token', 'theme', 'depth', 'base-url'].forEach((a) => {
        const v = params.get(a === 'base-url' ? 'base' : a);
        if (v) node.setAttribute(a, v);
    });
    if (!node.getAttribute('base-url')) {
        // Frame is served from <api-base>/embed/v1/frame/<kind>
        const idx = location.pathname.indexOf('/embed/v1/');
        node.setAttribute('base-url', idx >= 0 ? location.pathname.slice(0, idx) : '/cairn/api');
    }
    target.replaceChildren(node);
    return node;
}

if (typeof window !== 'undefined' && typeof customElements !== 'undefined') {
    defineElements();
    const run = () => renderFrame(document, window.location);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
    else run();
}
