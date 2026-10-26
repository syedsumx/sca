import React from 'react';
import ReactDOM from 'react-dom';

import { ConsumerApp, ConsumerAppProps } from './ConsumerApp';

/**
 * Module-Federation entry for DataHub's MFE host (`app/mfeframework/MFEConfigurableContainer.tsx`).
 *
 * The host loads `remoteEntry.js` as a webpack `var` remote, resolves `aegisConsumer/mount`, and calls
 * `mount(containerElement, {})`, keeping the return value as the cleanup function. Because the host
 * shares no dependencies, this bundle carries its own React 17 + antd and renders into the element.
 * The host route `/mfe<path>` is exact, so routing defaults to the URL hash.
 */
export function mount(el: HTMLElement, props: Partial<ConsumerAppProps> = {}): () => void {
    const routerMode = props.routerMode ?? (el.dataset.aegisRouter === 'browser' ? 'browser' : 'hash');
    ReactDOM.render(<ConsumerApp {...props} routerMode={routerMode} />, el);
    return () => {
        ReactDOM.unmountComponentAtNode(el);
    };
}

export default mount;
