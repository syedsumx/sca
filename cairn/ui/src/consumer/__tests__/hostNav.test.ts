import fs from 'fs';
import path from 'path';

import { HOST_SECTIONS, applyInitialSection, hostPathForRoute, isHosted, navigateHost, sectionForHostPath } from '../hostNav';

describe('catalog-shell navigation', () => {
    afterEach(() => window.history.replaceState(null, '', '/'));

    it('maps shell paths to workspace sections and back', () => {
        expect(sectionForHostPath('/mfe/cairn')).toBe('/discover');
        // L149: the marketplace entry opens the Browse view of Data products, its one home.
        expect(sectionForHostPath('/mfe/cairn-marketplace')).toBe('/products?view=browse');
        expect(sectionForHostPath('/mfe/cairn-marketplace/')).toBe('/products?view=browse');
        expect(sectionForHostPath('/mfe/cairn-context')).toBe('/semantics');
        expect(sectionForHostPath('/mfe/other-app')).toBeNull();
        expect(sectionForHostPath('/cairn/discover')).toBeNull();
        expect(hostPathForRoute('/products/urn:li:dataProduct:x')).toBe('/cairn-products');
        expect(hostPathForRoute('/requests?state=open')).toBe('/cairn-requests');
        expect(hostPathForRoute('/search?q=x')).toBeNull();          // no entry of its own: stays put
        expect(isHosted('/mfe/cairn-requests')).toBe(true);
        expect(isHosted('/cairn/discover')).toBe(false);              // standalone workspace
    });

    it('opens the section of the shell entry it is mounted on', () => {
        window.history.replaceState(null, '', '/mfe/cairn-requests');
        applyInitialSection();
        expect(window.location.hash).toBe('#/requests');
        window.history.replaceState(null, '', '/mfe/cairn-requests#/asset/x');
        applyInitialSection();
        expect(window.location.hash).toBe('#/asset/x');               // a deep link is kept
    });

    it('moves the shell to the entry that owns a section', () => {
        window.history.replaceState(null, '', '/mfe/cairn#/discover');
        const events: string[] = [];
        const onPop = () => events.push(window.location.pathname + window.location.hash);
        window.addEventListener('popstate', onPop);
        expect(navigateHost('/products')).toBe(true);
        expect(events).toEqual(['/mfe/cairn-products#/products']);
        expect(navigateHost('/products/urn:li:dataProduct:p')).toBe(false);   // same entry: in-app
        expect(navigateHost('/search')).toBe(false);
        window.removeEventListener('popstate', onPop);
    });

    it('matches the shell configuration', () => {
        const yaml = fs.readFileSync(path.join(__dirname, '../../../../deploy/mfe/mfe.config.yaml'), 'utf8');
        const paths = [...yaml.matchAll(/^\s+path: (\S+)$/gm)].map((m) => m[1]);
        expect(paths).toEqual(HOST_SECTIONS.map((s) => s.hostPath));
        expect(yaml).toMatch(/^subNavigationMode: false$/m);
        expect(fs.readFileSync(path.join(__dirname, '../../../mfe.config.yaml'), 'utf8')).toContain(
            yaml.slice(yaml.indexOf('topLevelMenuTitle')),
        );
    });
});
