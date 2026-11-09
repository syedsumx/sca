'use strict';
/*
 * The `cairn` command line inside Cairn Desktop: where it is, "Sign in the command line too" (its device flow,
 * approved with the workspace's signed-in session), and "Install command line tool".
 *
 * The desktop never handles the command line's tokens: `cairn login` receives them from Cairn and keeps them in the
 * OS keychain itself. The desktop only reads the code the CLI shows and approves that code, which it knows is its own
 * child's because it started the sign-in and checks the device's name before approving.
 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const exe = (platform) => (platform === 'win32' ? 'cairn.exe' : 'cairn');

/**
 * The bundled binary: `<resources>/cli/cairn[.exe]` in a packaged app. A development run (`electron .`) uses
 * CAIRN_DESKTOP_CLI (a path) or `resources/cli/` in this folder; a packaged app never takes it from the environment.
 */
function cliPath({ packaged, resourcesPath, platform = process.platform, env = process.env, appDir }) {
    if (packaged) return path.join(resourcesPath, 'cli', exe(platform));
    if ((env.CAIRN_DESKTOP_CLI || '').trim()) return env.CAIRN_DESKTOP_CLI.trim();
    return path.join(appDir, 'resources', 'cli', exe(platform));
}

/** The code `cairn login --no-browser` shows: "To sign in, open <uri> and enter the code ABCD-EFGH". */
function parseDeviceCode(text) {
    const m = /open (\S+) and enter the code ([A-Z]{4}-[A-Z]{4})\b/.exec(String(text).replace(/\u001b\[[0-9;]*m/g, ''));
    return m ? { uri: m[1], code: m[2] } : null;
}

/** Is the device waiting for approval the CLI this app started for this profile? (cairn_cli.auth._device_name) */
function isOurDevice(described, profile, hostname = os.hostname()) {
    const name = described && typeof described.name === 'string' ? described.name : '';
    return name.startsWith(`cairn CLI on ${hostname || 'computer'} (`) && name.endsWith(`, profile ${profile})`);
}

/** The command line's environment: the person's own, minus what would point it at another Cairn or token. */
function cliEnv(env = process.env) {
    const out = { ...env, NO_COLOR: '1' };
    for (const k of ['CAIRN_URL', 'CAIRN_TOKEN', 'CAIRN_TOKEN_FILE', 'CAIRN_PROFILE', 'ELECTRON_RUN_AS_NODE']) delete out[k];
    return out;
}

/**
 * Runs `cairn login` for a profile and approves its code through `approve(code)` (the workspace's session). When that
 * cannot approve it (a passkey is needed, say), `fallback(code, error)` shows the code on the workspace's /device page
 * for the person to approve there, and the CLI keeps waiting. Resolves when the CLI ends: {ok: true, user,
 * credential_store} or {ok: false, error}. `run(cmd, args, opts)` spawns (child_process.spawn by default).
 */
function signInCommandLine({ cli, profile, url, approve, fallback = () => {}, run = spawn, env = process.env,
                             timeoutMs = 10 * 60 * 1000 }) {
    return new Promise((resolve) => {
        if (!fs.existsSync(cli)) {
            resolve({ ok: false, error: `the command line is not in this build (${cli})` });
            return;
        }
        const args = ['login', '--url', url, '--profile', profile, '--no-browser', '--no-color', '--output', 'json'];
        let child;
        try {
            child = run(cli, args, { env: cliEnv(env), stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
        } catch (e) {
            resolve({ ok: false, error: `could not start the command line: ${e.message}` });
            return;
        }
        let out = '';
        let err = '';
        let seen = false;
        let settled = false;
        const finish = (result) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            resolve(result);
        };
        const timer = setTimeout(() => {
            finish({ ok: false, error: 'the sign-in was not approved in time' });
            child.kill();
        }, timeoutMs);
        child.stdout.on('data', (d) => { out += d; });
        child.stderr.on('data', (d) => {
            err += d;
            if (seen) return;
            const found = parseDeviceCode(err);
            if (!found) return;
            seen = true;
            Promise.resolve().then(() => approve(found.code)).then(
                (r) => { if (!r || !r.ok) fallback(found.code, (r && r.error) || 'not approved'); },
                (e) => fallback(found.code, e.message));
        });
        child.on('error', (e) => finish({ ok: false, error: `the command line failed to start: ${e.message}` }));
        child.on('close', (status) => {
            if (status === 0) {
                let doc = {};
                try {
                    doc = JSON.parse(out.trim() || '{}');
                } catch { /* the text form */ }
                finish({ ok: true, user: doc.user || null, credential_store: doc.credential_store || null, profile });
            } else {
                const last = err.trim().split('\n').filter((l) => l.startsWith('cairn:') || l.startsWith('error')).pop();
                finish({ ok: false, error: last || `the command line stopped (exit ${status})` });
            }
        });
    });
}

/**
 * Where "Install command line tool" puts `cairn`. macOS and Linux: a link in /usr/local/bin when it can be written,
 * else ~/.local/bin (a copy for an AppImage, whose files move each launch). Windows: the folder with cairn.exe on the
 * user's PATH. → {method: 'symlink'|'copy'|'user-path', dir, target?, onPath}
 */
function planInstall({ platform = process.platform, home = os.homedir(), pathEnv = process.env.PATH || '', cli,
                       usrLocalWritable = false, appImage = false }) {
    const sep = platform === 'win32' ? ';' : ':';
    const onPath = (dir) => pathEnv.split(sep).some((p) => p && path.resolve(p).toLowerCase() === path.resolve(dir).toLowerCase());
    if (platform === 'win32') {
        const dir = path.dirname(cli);
        return { method: 'user-path', dir, onPath: onPath(dir) };
    }
    const dir = usrLocalWritable && !appImage ? '/usr/local/bin' : path.join(home, '.local', 'bin');
    return { method: appImage ? 'copy' : 'symlink', dir, target: path.join(dir, 'cairn'), onPath: onPath(dir) };
}

/**
 * Carries out a plan (after the person said yes). Refuses to replace a `cairn` that is not this app's (a Homebrew or
 * pip install stays). `exec(cmd, args, opts)` runs PowerShell on Windows (child_process.execFile by default).
 */
function install(plan, cli, { exec } = {}) {
    if (plan.method === 'user-path') {
        // The folder goes in through an environment variable, never into the command's text.
        const script = "$d=$env:CAIRN_CLI_DIR; $p=[Environment]::GetEnvironmentVariable('Path','User'); " +
            "if (-not $p) { $p='' }; if (($p -split ';') -notcontains $d) { " +
            "[Environment]::SetEnvironmentVariable('Path', ($(if ($p) { $p.TrimEnd(';') + ';' } else { '' }) + $d), 'User') }";
        const run = exec || require('child_process').execFile;
        return new Promise((resolve, reject) => run('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script],
            { env: { ...process.env, CAIRN_CLI_DIR: plan.dir }, windowsHide: true },
            (e) => (e ? reject(e) : resolve({ ...plan, done: true }))));
    }
    fs.mkdirSync(plan.dir, { recursive: true });
    let existing = null;
    try {
        existing = fs.lstatSync(plan.target);
    } catch { /* none */ }
    if (existing) {
        const ours = existing.isSymbolicLink()
            ? /cairn/i.test(fs.readlinkSync(plan.target)) && fs.readlinkSync(plan.target).includes(`${path.sep}cli${path.sep}`)
            : plan.method === 'copy' && fs.existsSync(`${plan.target}.cairn-desktop`);
        if (!ours) {
            return Promise.reject(new Error(`${plan.target} exists and was not installed by Cairn Desktop; remove it first or keep using it`));
        }
        fs.rmSync(plan.target, { force: true });
    }
    if (plan.method === 'copy') {
        fs.copyFileSync(cli, plan.target);
        fs.chmodSync(plan.target, 0o755);
        fs.writeFileSync(`${plan.target}.cairn-desktop`, 'installed by Cairn Desktop\n');
    } else {
        fs.symlinkSync(cli, plan.target);
    }
    return Promise.resolve({ ...plan, done: true });
}

module.exports = { cliPath, parseDeviceCode, isOurDevice, cliEnv, signInCommandLine, planInstall, install };
