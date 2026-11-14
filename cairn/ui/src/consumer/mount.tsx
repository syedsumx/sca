import React from 'react';
import ReactDOM from 'react-dom';

import { ConsumerApp, ConsumerAppProps } from './ConsumerApp';
import { applyInitialSection } from './hostNav';

/**
 * Module-Federation entry for Cairn Core's MFE host (`app/mfeframework/MFEConfigurableContainer.tsx`).
 *
 * The host loads `remoteEntry.js` as a webpack `var` remote, resolves `cairnConsumer/mount`, and calls
 * `mount(containerElement, {})`, keeping the return value as the cleanup function. Because the host
 * shares no dependencies, this bundle carries its own React 17 + antd and renders into the element.
 * The host route `/mfe<path>` is exact, so routing defaults to the URL hash.
 */
export function mount(el: HTMLElement, props: Partial<ConsumerAppProps> = {}): () => void {
    const routerMode = props.routerMode ?? (el.dataset.cairnRouter === 'browser' ? 'browser' : 'hash');
    if (routerMode === 'hash') applyInitialSection();
    ReactDOM.render(<ConsumerApp {...props} routerMode={routerMode} />, el);
    return () => {
        ReactDOM.unmountComponentAtNode(el);
    };
}

export default mount;
