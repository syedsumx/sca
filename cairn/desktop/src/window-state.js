'use strict';
/*
 * Remembering the window: its size, place, and whether it was maximized or full screen, restored only where it
 * still fits on a screen (a monitor unplugged since puts it back on the main one).
 */
const fs = require('fs');

const DEFAULT = { width: 1280, height: 860 };
const MIN = { width: 480, height: 400 };

function intersects(a, b) {
    return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

/** Saved bounds made safe for the screens there are now (`displays`: their workAreas; the first is the main one). */
function restoreBounds(saved, displays) {
    const main = displays[0] || { x: 0, y: 0, width: DEFAULT.width, height: DEFAULT.height };
    const ok = saved && ['x', 'y', 'width', 'height'].every((k) => Number.isFinite(saved[k]));
    if (!ok) {
        const width = Math.min(DEFAULT.width, main.width);
        const height = Math.min(DEFAULT.height, main.height);
        return { width, height, maximized: false, fullScreen: false };
    }
    const width = Math.max(MIN.width, Math.min(saved.width, Math.max(...displays.map((d) => d.width), MIN.width)));
    const height = Math.max(MIN.height, Math.min(saved.height, Math.max(...displays.map((d) => d.height), MIN.height)));
    const box = { x: saved.x, y: saved.y, width, height };
    // At least a corner of the title bar must be on a screen, else the window opens centred on the main one.
    const titleBar = { x: box.x, y: box.y, width: box.width, height: 40 };
    const visible = displays.some((d) => intersects(titleBar, d));
    const placed = visible ? box : {
        x: main.x + Math.max(0, Math.round((main.width - width) / 2)),
        y: main.y + Math.max(0, Math.round((main.height - height) / 2)), width, height,
    };
    return { ...placed, maximized: !!saved.maximized, fullScreen: !!saved.fullScreen };
}

function load(file) {
    try {
        return JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch {
        return null;
    }
}

function save(file, state) {
    try {
        fs.writeFileSync(file, JSON.stringify(state));
    } catch { /* not worth failing for */ }
}

module.exports = { restoreBounds, load, save, DEFAULT };
