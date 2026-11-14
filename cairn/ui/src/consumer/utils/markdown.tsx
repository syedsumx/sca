import React from 'react';

/**
 * Safe Markdown → React renderer for user- and AI-authored text.
 *
 * Never uses `dangerouslySetInnerHTML`: raw HTML is shown as text. Supported: headings, paragraphs,
 * bullet/numbered lists, fenced code, blockquotes, inline code, **bold**, *italic*, [links](url)
 * (http/https/mailto and app-relative only), @mentions, and bare urn:li: references.
 */

const SAFE_URL = /^(https?:\/\/|mailto:|\/(?!\/)|#\/)/i;

export function isSafeUrl(url: string): boolean {
    return SAFE_URL.test(url.trim()) && !/^\s*javascript:/i.test(url);
}

type InlineToken =
    | { kind: 'text'; value: string }
    | { kind: 'code'; value: string }
    | { kind: 'bold'; value: string }
    | { kind: 'italic'; value: string }
    | { kind: 'link'; label: string; href: string }
    | { kind: 'mention'; value: string }
    | { kind: 'urn'; value: string };

const INLINE =
    /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*|_[^_\s][^_]*_)|(\[[^\]]+\]\([^)\s]+\))|(@[A-Za-z0-9._-]+)|(urn:li:[A-Za-z]+:(?:\([^\s)]*\)|[^\s,;)]+))/g;

export function tokenizeInline(text: string): InlineToken[] {
    const out: InlineToken[] = [];
    let last = 0;
    let m: RegExpExecArray | null;
    INLINE.lastIndex = 0;
    // eslint-disable-next-line no-cond-assign
    while ((m = INLINE.exec(text))) {
        if (m.index > last) out.push({ kind: 'text', value: text.slice(last, m.index) });
        const [tok] = m;
        if (m[1]) out.push({ kind: 'code', value: tok.slice(1, -1) });
        else if (m[2]) out.push({ kind: 'bold', value: tok.slice(2, -2) });
        else if (m[3]) out.push({ kind: 'italic', value: tok.slice(1, -1) });
        else if (m[4]) {
            const mm = tok.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
            if (mm && isSafeUrl(mm[2])) out.push({ kind: 'link', label: mm[1], href: mm[2] });
            else out.push({ kind: 'text', value: mm ? mm[1] : tok });
        } else if (m[5]) out.push({ kind: 'mention', value: tok.slice(1) });
        else if (m[6]) out.push({ kind: 'urn', value: tok });
        last = m.index + tok.length;
    }
    if (last < text.length) out.push({ kind: 'text', value: text.slice(last) });
    return out;
}

export interface MarkdownOptions {
    /** Turn urn references into links; defaults to the in-app (hash) route #/asset/<urn>. */
    urnHref?: (urn: string) => string;
}

function renderInline(text: string, opts: MarkdownOptions, keyPrefix: string): React.ReactNode[] {
    return tokenizeInline(text).map((t, i) => {
        const key = `${keyPrefix}-${i}`;
        switch (t.kind) {
            case 'code':
                return <code key={key}>{t.value}</code>;
            case 'bold':
                return <strong key={key}>{t.value}</strong>;
            case 'italic':
                return <em key={key}>{t.value}</em>;
            case 'link':
                return (
                    <a key={key} href={t.href} rel="noopener noreferrer" target={t.href.startsWith('/') ? undefined : '_blank'}>
                        {t.label}
                    </a>
                );
            case 'mention':
                return (
                    <span key={key} className="cairn-mention">
                        @{t.value}
                    </span>
                );
            case 'urn': {
                const href = (opts.urnHref ?? ((u: string) => `#/asset/${encodeURIComponent(u)}`))(t.value);
                return (
                    <a key={key} href={href} className="cairn-urn">
                        {t.value.match(/,([^,]+),[A-Z_]+\)$/)?.[1] ?? t.value.split(':').slice(3).join(':')}
                    </a>
                );
            }
            default:
                return <React.Fragment key={key}>{t.value}</React.Fragment>;
        }
    });
}

export function renderMarkdown(source: string, opts: MarkdownOptions = {}): React.ReactNode[] {
    const lines = source.replace(/\r\n/g, '\n').split('\n');
    const blocks: React.ReactNode[] = [];
    let i = 0;
    let k = 0;
    const key = () => `b${(k += 1)}`;
    while (i < lines.length) {
        const line = lines[i];
        if (/^```/.test(line)) {
            const code: string[] = [];
            i += 1;
            while (i < lines.length && !/^```/.test(lines[i])) {
                code.push(lines[i]);
                i += 1;
            }
            i += 1;
            blocks.push(
                <pre key={key()}>
                    <code>{code.join('\n')}</code>
                </pre>,
            );
            continue;
        }
        const h = line.match(/^(#{1,4})\s+(.*)$/);
        if (h) {
            const level = Math.min(h[1].length + 2, 6);
            const Tag = `h${level}` as keyof JSX.IntrinsicElements;
            const kk = key();
            blocks.push(<Tag key={kk}>{renderInline(h[2], opts, kk)}</Tag>);
            i += 1;
            continue;
        }
        if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
            const ordered = /^\s*\d+\./.test(line);
            const items: string[] = [];
            while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
                items.push(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ''));
                i += 1;
            }
            const kk = key();
            const children = items.map((it, j) => <li key={`${kk}-${j}`}>{renderInline(it, opts, `${kk}-${j}`)}</li>);
            blocks.push(ordered ? <ol key={kk}>{children}</ol> : <ul key={kk}>{children}</ul>);
            continue;
        }
        if (/^>\s?/.test(line)) {
            const quote: string[] = [];
            while (i < lines.length && /^>\s?/.test(lines[i])) {
                quote.push(lines[i].replace(/^>\s?/, ''));
                i += 1;
            }
            const kk = key();
            blocks.push(<blockquote key={kk}>{renderInline(quote.join(' '), opts, kk)}</blockquote>);
            continue;
        }
        if (!line.trim()) {
            i += 1;
            continue;
        }
        const para: string[] = [];
        while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|>|\s*([-*]|\d+\.)\s)/.test(lines[i])) {
            para.push(lines[i]);
            i += 1;
        }
        const kk = key();
        blocks.push(<p key={kk}>{renderInline(para.join(' '), opts, kk)}</p>);
    }
    return blocks;
}

export function MarkdownView({ source, className, ...opts }: { source: string; className?: string } & MarkdownOptions) {
    return <div className={className ?? 'cairn-markdown'}>{renderMarkdown(source || '', opts)}</div>;
}

/** Extract @mentions (for the composer → API `mentions`). */
export function extractMentions(text: string): string[] {
    return Array.from(new Set((text.match(/@[A-Za-z0-9._-]+/g) ?? []).map((m) => m.slice(1))));
}
