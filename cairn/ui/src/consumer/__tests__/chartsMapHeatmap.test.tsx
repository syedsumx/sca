import { fireEvent, render, screen } from '@testing-library/react';
import React from 'react';

import { QueryCache } from '../api/cache';
import { ResultChart } from '../components/ResultChart';
import { CairnProvider } from '../context/CairnProvider';
import { UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const wrap = (config: object, el: React.ReactElement) => {
    const { client } = makeClient({ 'GET /ui/config': { ...UI_CONFIG, ...config } });
    return render(<CairnProvider client={client} cache={new QueryCache()}>{el}</CairnProvider>);
};

it('a heatmap colours one value by two columns, and a click selects its column', () => {
    const picked: unknown[] = [];
    const { container } = wrap({}, <ResultChart columns={['day', 'region', 'n']}
        rows={[['Mon', 'EU', 1], ['Mon', 'US', 9], ['Tue', 'EU', 5]]}
        chart={{ type: 'heatmap', x: 'day', series: 'region', y: ['n'] }} onSelect={(v) => picked.push(v)} />);
    expect(screen.getByRole('img', { name: 'heatmap of n by day and region' })).toBeTruthy();
    const cells = [...container.querySelectorAll('rect title')].map((t) => t.textContent);
    expect(cells).toEqual(['Mon · EU: 1', 'Mon · US: 9', 'Tue · EU: 5', 'Tue · US: none']);
    const fill = (label: string) => [...container.querySelectorAll('rect')].find((r) => r.textContent === label)!.getAttribute('fill');
    expect(fill('Mon · US: 9')).toBe('rgb(24,100,171)');        // the most is the darkest
    expect(fill('Mon · EU: 1')).toBe('rgb(231,245,255)');       // the least the lightest
    expect(fill('Tue · US: none')).toBe('#f6f8fa');             // no value: no colour
    fireEvent.click([...container.querySelectorAll('rect')].find((r) => r.textContent === 'Tue · EU: 5')!);
    expect(picked).toEqual(['Tue']);
});

it('a heatmap without its columns says what it needs', () => {
    wrap({}, <ResultChart columns={['day', 'n']} rows={[['Mon', 1]]} chart={{ type: 'heatmap', x: 'day', y: ['n'] }} />);
    expect(screen.getByText(/one down \(series\)/)).toBeTruthy();
});

it('a map places rows at their latitude and longitude, sized by a value, and skips rows without a place', () => {
    const picked: unknown[] = [];
    const { container } = wrap({}, <ResultChart columns={['city', 'lat', 'lon', 'sales']}
        rows={[['Paris', 48.86, 2.35, 100], ['Berlin', 52.52, 13.4, 25], ['Nowhere', null, null, 5], ['Bad', 91, 0, 1]]}
        chart={{ type: 'map', x: 'city', lat: 'lat', lon: 'lon', y: ['sales'] }} onSelect={(v) => picked.push(v)} />);
    expect(screen.getByRole('img', { name: 'map of 2 points sized by sales' })).toBeTruthy();
    const dots = [...container.querySelectorAll('circle')];
    expect(dots.map((c) => c.textContent)).toEqual(['Paris: 100', 'Berlin: 25']);
    const [paris, berlin] = dots.map((c) => ({ x: Number(c.getAttribute('cx')), y: Number(c.getAttribute('cy')), r: Number(c.getAttribute('r')) }));
    expect(berlin.x).toBeGreaterThan(paris.x);       // east
    expect(berlin.y).toBeLessThan(paris.y);          // north
    expect(paris.r).toBeGreaterThan(berlin.r);       // more sales, bigger
    expect(container.querySelectorAll('image')).toHaveLength(0);   // no tile server: nothing fetched
    expect([...container.querySelectorAll('svg > text')].map((t) => t.textContent)).toEqual(['Paris', 'Berlin']);   // named
    fireEvent.click(dots[1]);
    expect(picked).toEqual(['Berlin']);
});

it('a map draws on the operator\'s tiles, with their attribution', async () => {
    const { container } = wrap({ map_tiles: 'https://tiles.example.com/{z}/{x}/{y}.png', map_attribution: '© Example' },
        <ResultChart columns={['lat', 'lon']} rows={[[48.86, 2.35], [52.52, 13.4]]} chart={{ type: 'map', lat: 'lat', lon: 'lon' }} />);
    expect(await screen.findByText('© Example')).toBeTruthy();
    const hrefs = [...container.querySelectorAll('image')].map((i) => i.getAttribute('href') ?? '');
    expect(hrefs.length).toBeGreaterThan(0);
    expect(hrefs.every((h) => /^https:\/\/tiles\.example\.com\/\d+\/\d+\/\d+\.png$/.test(h))).toBe(true);
    // Paris and Berlin, 880 km apart, fit a phone-width (400×260) chart at zoom 5.
    expect(new Set(hrefs.map((h) => h.split('/')[3]))).toEqual(new Set(['5']));
});
