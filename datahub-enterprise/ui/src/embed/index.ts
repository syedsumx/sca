/** Embed SDK entry: `<script src="…/embed/v1/sdk.js" defer></script>` registers the elements. */
import { defineElements } from './elements';

export * from './elements';
export * from './api';

if (typeof window !== 'undefined' && typeof customElements !== 'undefined') {
    defineElements();
}
