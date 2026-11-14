'use strict';
/*
 * A read-only copy of the page a person is looking at, for the offline cache. It runs in an isolated world of the
 * workspace's page (the page's scripts cannot see or change it), waits until the page has settled (no spinner, no
 * change for a moment), and returns the page as static HTML: no scripts, no frames, no links, no event handlers,
 * charts drawn on canvases as images, and only the style rules something on the page uses. Shown later in a
 * sandboxed frame that runs nothing.
 */

/* eslint-disable no-undef -- runs in the page */
function pageSnapshot(quietMs, maxMs) {
    function keepRule(rule) {
        if (rule.type === 1) {                               // CSSStyleRule: kept when something on the page matches
            const sels = String(rule.selectorText || '').split(',');
            return sels.some((s) => {
                const plain = s.replace(/::?(before|after|placeholder|selection|marker|-webkit-[\w-]+|-moz-[\w-]+)/g, '')
                    .replace(/:(hover|focus|focus-visible|focus-within|active|visited|link|checked|disabled|enabled)\b/g, '')
                    .trim() || '*';
                try {
                    return !!document.querySelector(plain);
                } catch {
                    return true;                              // a selector the browser keeps but cannot query: keep it
                }
            });
        }
        if (rule.type === 5) return false;                   // @font-face: the fonts are not there offline
        return true;
    }
    function css(rules) {
        let out = '';
        for (const rule of Array.from(rules || [])) {
            if (rule.cssRules && (rule.type === 4 || rule.type === 12)) {   // @media, @supports: their rules filtered
                const inner = css(rule.cssRules);
                if (inner) out += `${rule.cssText.slice(0, rule.cssText.indexOf('{'))}{${inner}}\n`;
            } else if (keepRule(rule)) {
                out += `${rule.cssText}\n`;
            }
        }
        return out;
    }
    function styles() {
        let out = '';
        for (const sheet of [...Array.from(document.styleSheets), ...Array.from(document.adoptedStyleSheets || [])]) {
            try {
                out += css(sheet.cssRules);
            } catch { /* another origin's sheet: not readable, not kept */ }
        }
        return out;
    }
    function serialize() {
        const canvases = Array.from(document.querySelectorAll('canvas')).map((c) => {
            try {
                return c.toDataURL('image/png');
            } catch {
                return null;
            }
        });
        const images = Array.from(document.images).map((img) => {
            if (/^data:image\//.test(img.src)) return img.src;
            try {
                const c = document.createElement('canvas');
                c.width = img.naturalWidth;
                c.height = img.naturalHeight;
                c.getContext('2d').drawImage(img, 0, 0);
                return c.width && c.height ? c.toDataURL('image/png') : null;
            } catch {
                return null;
            }
        });
        const values = Array.from(document.querySelectorAll('input, textarea, select')).map((el) => el.value);
        const root = document.documentElement.cloneNode(true);
        root.querySelectorAll('img').forEach((img, i) => {
            if (img.getAttribute('src') && img.getAttribute('src').startsWith('data:image/')) return;
            const data = images[i];
            if (data) img.setAttribute('src', data);
            else img.removeAttribute('src');
            img.removeAttribute('srcset');
        });
        root.querySelectorAll('canvas').forEach((c, i) => {
            const img = document.createElement('img');
            if (canvases[i]) img.setAttribute('src', canvases[i]);
            img.setAttribute('style', c.getAttribute('style') || '');
            img.setAttribute('width', c.getAttribute('width') || c.width);
            img.setAttribute('height', c.getAttribute('height') || c.height);
            img.setAttribute('alt', c.getAttribute('aria-label') || 'chart');
            c.replaceWith(img);
        });
        root.querySelectorAll('input, textarea, select').forEach((el, i) => {
            if (el.tagName === 'TEXTAREA') el.textContent = values[i] || '';
            else if (el.tagName === 'INPUT') el.setAttribute('value', values[i] || '');
            el.setAttribute('disabled', '');
        });
        root.querySelectorAll('script, noscript, iframe, frame, object, embed, link, meta, base, template, audio, video, source, track')
            .forEach((el) => el.remove());
        root.querySelectorAll('*').forEach((el) => {
            for (const a of Array.from(el.attributes)) {
                const n = a.name.toLowerCase();
                if (n.startsWith('on') || n === 'href' || n === 'xlink:href' && !/^#/.test(a.value) || n === 'action'
                    || n === 'formaction' || n === 'srcdoc' || n === 'ping' || n === 'contenteditable'
                    || (n === 'src' && !/^data:image\//.test(a.value))) {
                    el.removeAttribute(a.name);
                }
            }
        });
        const head = root.querySelector('head');
        if (head) head.remove();
        const heading = document.querySelector('main h1, h1');
        const title = (heading && heading.textContent.trim()) || document.title || 'Cairn';
        const body = root.querySelector('body');
        const attrs = (el) => Array.from(el ? el.attributes : []).map((a) => ` ${a.name}="${a.value.replace(/&/g, '&amp;').replace(/"/g, '&quot;')}"`).join('');
        const html = `<!doctype html><html${attrs(root)}><head><meta charset="utf-8"><title></title><style>${styles()
            .replace(/<\/style/gi, '<\\/style')}</style></head><body${attrs(body)}>${body ? body.innerHTML : ''}</body></html>`;
        return { title: title.slice(0, 200), html, route: location.hash.replace(/^#/, '') };
    }
    return new Promise((resolve) => {
        const start = Date.now();
        let last = Date.now();
        const mo = new MutationObserver(() => { last = Date.now(); });
        mo.observe(document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
        const tick = () => {
            const busy = document.querySelector('.ant-spin-spinning, .ant-skeleton-active, [aria-busy="true"]');
            if ((Date.now() - last >= quietMs && !busy) || Date.now() - start > maxMs) {
                mo.disconnect();
                resolve(serialize());
            } else {
                setTimeout(tick, 200);
            }
        };
        setTimeout(tick, 200);
    });
}
/* eslint-enable no-undef */

/** The code to run in the page's isolated world (webContents.executeJavaScriptInIsolatedWorld). */
function snapshotCode(quietMs = 1200, maxMs = 15000) {
    return `(${pageSnapshot.toString()})(${Number(quietMs)}, ${Number(maxMs)})`;
}

module.exports = { snapshotCode, pageSnapshot };
