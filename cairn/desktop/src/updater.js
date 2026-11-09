'use strict';
/*
 * Updates through electron-updater, from GitHub Releases (the rolling `desktop-latest` release, which carries the
 * latest*.yml files) or the feed an administrator sets (update_feed_url: electron-updater's generic provider).
 *
 * Every download is checked against the SHA-512 in the feed's latest*.yml before it is installed. That is all that
 * protects an unsigned build: on macOS an unsigned app cannot install updates at all, so there it only says an
 * update is out and links to the download (policy.updateDecision). Turned off by the person (Settings), by any
 * workspace's administrator (updates: off) or the computer's policy file; pinned versions are never passed.
 */
const { updateDecision, compareVersions } = require('./policy');

const CHECK_EVERY_MS = 6 * 60 * 60 * 1000;
const FIRST_CHECK_MS = 30 * 1000;

function createUpdater({ app, shell, loadUpdater = () => require('electron-updater').autoUpdater, getPolicy, signed,
                         platform = process.platform, env = process.env, releasesUrl, report = () => {} }) {
    const state = { status: 'idle', available: null, decision: null, error: null, checkedAt: null };
    let updater = null;
    let timer = null;

    function packaged() {
        return !!app.isPackaged;
    }

    function load() {
        if (updater) return updater;
        updater = loadUpdater();
        updater.autoDownload = false;
        updater.autoInstallOnAppQuit = true;
        updater.allowDowngrade = false;
        updater.logger = null;
        updater.on('error', (e) => {
            state.status = 'error';
            state.error = String(e && e.message ? e.message : e).split('\n')[0];
            report(state);
        });
        updater.on('update-downloaded', (info) => {
            state.status = 'downloaded';
            state.available = info.version;
            report(state, { kind: 'downloaded', version: info.version });
        });
        return updater;
    }

    async function check({ manual = false } = {}) {
        const policy = getPolicy();
        state.checkedAt = Date.now();
        if (policy.updates === 'off') {
            state.status = 'off';
            state.decision = { action: 'blocked', reason: `updates are turned off (${policy.source.updates || 'policy'})` };
            report(state, manual ? { kind: 'blocked', reason: state.decision.reason } : null);
            return state;
        }
        if (!packaged()) {
            state.status = 'development';
            state.decision = { action: 'none', reason: 'a development build does not check for updates' };
            report(state, manual ? { kind: 'blocked', reason: state.decision.reason } : null);
            return state;
        }
        const u = load();
        if (policy.feed) u.setFeedURL({ provider: 'generic', url: policy.feed });
        state.status = 'checking';
        report(state);
        let result;
        try {
            result = await u.checkForUpdates();
        } catch (e) {
            state.status = 'error';
            state.error = String(e && e.message ? e.message : e).split('\n')[0];
            report(state, manual ? { kind: 'error', reason: state.error } : null);
            return state;
        }
        const available = result && result.updateInfo ? result.updateInfo.version : null;
        state.available = available;
        const decision = updateDecision({ policy, current: app.getVersion(), available, platform, signed,
            packaged: true, appImage: platform !== 'linux' || !!env.APPIMAGE });
        state.decision = decision;
        if (decision.action === 'download') {
            state.status = 'downloading';
            report(state);
            try {
                await u.downloadUpdate();
            } catch (e) {
                state.status = 'error';
                state.error = String(e && e.message ? e.message : e).split('\n')[0];
                report(state, { kind: 'error', reason: state.error });
            }
        } else {
            state.status = decision.action === 'none' ? 'current' : decision.action;
            const say = decision.action === 'notify' || (manual && decision.action !== 'none') ? decision.action : null;
            report(state, say ? { kind: say, version: available, reason: decision.reason }
                : manual ? { kind: 'current' } : null);
        }
        return state;
    }

    function start() {
        if (timer) return;
        setTimeout(() => check().catch(() => {}), FIRST_CHECK_MS).unref();
        timer = setInterval(() => check().catch(() => {}), CHECK_EVERY_MS);
        timer.unref();
    }

    function installNow() {
        if (state.status === 'downloaded' && updater) updater.quitAndInstall();
    }

    function openDownloads() {
        const policy = getPolicy();
        shell.openExternal(policy.feed || releasesUrl);
    }

    return { state, check, start, installNow, openDownloads, compareVersions };
}

module.exports = { createUpdater };
