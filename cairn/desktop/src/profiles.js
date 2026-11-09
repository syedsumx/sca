'use strict';
/*
 * Workspaces are the command line's profiles: Cairn Desktop reads and writes the same `config.toml` as `cairn`
 * (cairn_cli.config), so a workspace added in either shows in both.
 *
 *     default_profile = "prod"
 *
 *     [profiles.prod]
 *     url = "https://cairn.example.com/cairn/api"
 *
 * The directory: CAIRN_CONFIG_DIR, else $XDG_CONFIG_HOME/cairn, else %APPDATA%\cairn on Windows, else
 * ~/.config/cairn. The file holds no secrets (the CLI keeps tokens in the OS keychain; the desktop keeps none).
 *
 * The parser reads the TOML the CLI writes and what people commonly hand-edit (comments, bare or quoted keys, basic
 * and literal strings, booleans, numbers). Anything else (arrays, inline tables, multi-line strings) is refused
 * rather than guessed: the desktop then shows the error and never rewrites the file.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');

const { PROFILE_NAME } = require('./logic');

function configDir(env = process.env, platform = process.platform, home = os.homedir()) {
    const explicit = (env.CAIRN_CONFIG_DIR || '').trim();
    if (explicit) return explicit.replace(/^~(?=$|[\\/])/, home);
    const xdg = (env.XDG_CONFIG_HOME || '').trim();
    if (xdg) return path.join(xdg.replace(/^~(?=$|[\\/])/, home), 'cairn');
    if (platform === 'win32' && env.APPDATA) return path.join(env.APPDATA, 'cairn');
    return path.join(home, '.config', 'cairn');
}

class TomlError extends Error {}

const ESCAPES = { b: '\b', t: '\t', n: '\n', f: '\f', r: '\r', '"': '"', '\\': '\\' };

function parseBasic(s, i, line) {
    let out = '';
    i += 1;
    while (i < s.length) {
        const c = s[i];
        if (c === '"') return [out, i + 1];
        if (c === '\\') {
            const e = s[i + 1];
            if (ESCAPES[e] !== undefined) {
                out += ESCAPES[e];
                i += 2;
            } else if (e === 'u' || e === 'U') {
                const n = e === 'u' ? 4 : 8;
                const hex = s.slice(i + 2, i + 2 + n);
                if (!new RegExp(`^[0-9A-Fa-f]{${n}}$`).test(hex)) throw new TomlError(`line ${line}: bad \\${e} escape`);
                out += String.fromCodePoint(parseInt(hex, 16));
                i += 2 + n;
            } else {
                throw new TomlError(`line ${line}: bad escape`);
            }
        } else {
            out += c;
            i += 1;
        }
    }
    throw new TomlError(`line ${line}: unterminated string`);
}

function parseKey(s, i, line) {
    if (s[i] === '"') return parseBasic(s, i, line);
    if (s[i] === "'") {
        const end = s.indexOf("'", i + 1);
        if (end < 0) throw new TomlError(`line ${line}: unterminated key`);
        return [s.slice(i + 1, end), end + 1];
    }
    const m = /^[A-Za-z0-9_-]+/.exec(s.slice(i));
    if (!m) throw new TomlError(`line ${line}: expected a key`);
    return [m[0], i + m[0].length];
}

function parseKeyPath(s, i, line) {
    const keys = [];
    for (;;) {
        while (s[i] === ' ' || s[i] === '\t') i += 1;
        const [k, j] = parseKey(s, i, line);
        keys.push(k);
        i = j;
        while (s[i] === ' ' || s[i] === '\t') i += 1;
        if (s[i] !== '.') return [keys, i];
        i += 1;
    }
}

function rest(s, i, line) {
    const tail = s.slice(i).trim();
    if (tail && !tail.startsWith('#')) throw new TomlError(`line ${line}: unexpected ${JSON.stringify(tail.slice(0, 20))}`);
}

function parseValue(s, i, line) {
    if (s.startsWith('"""', i) || s.startsWith("'''", i)) throw new TomlError(`line ${line}: multi-line strings are not supported`);
    if (s[i] === '"') return parseBasic(s, i, line);
    if (s[i] === "'") {
        const end = s.indexOf("'", i + 1);
        if (end < 0) throw new TomlError(`line ${line}: unterminated string`);
        return [s.slice(i + 1, end), end + 1];
    }
    const m = /^[^\s#]+/.exec(s.slice(i));
    const tok = m ? m[0] : '';
    if (tok === 'true' || tok === 'false') return [tok === 'true', i + tok.length];
    if (/^[+-]?(\d[\d_]*)$/.test(tok)) return [Number(tok.replace(/_/g, '')), i + tok.length];
    if (/^[+-]?\d[\d_]*(\.\d[\d_]*)?([eE][+-]?\d+)?$/.test(tok)) return [Number(tok.replace(/_/g, '')), i + tok.length];
    throw new TomlError(`line ${line}: unsupported value ${JSON.stringify(tok.slice(0, 20))} (arrays and tables are not read here)`);
}

/** The subset of TOML described above, as a plain object. */
function parseToml(text) {
    const root = {};
    let table = root;
    const lines = String(text).replace(/^﻿/, '').split(/\r?\n/);
    lines.forEach((raw, n) => {
        const line = n + 1;
        const s = raw;
        let i = 0;
        while (s[i] === ' ' || s[i] === '\t') i += 1;
        if (i >= s.length || s[i] === '#') return;
        if (s[i] === '[') {
            if (s[i + 1] === '[') throw new TomlError(`line ${line}: arrays of tables are not supported`);
            const [keys, j] = parseKeyPath(s, i + 1, line);
            if (s[j] !== ']') throw new TomlError(`line ${line}: expected ]`);
            rest(s, j + 1, line);
            table = root;
            for (const k of keys) {
                if (table[k] === undefined) table[k] = {};
                if (typeof table[k] !== 'object') throw new TomlError(`line ${line}: ${k} is not a table`);
                table = table[k];
            }
            return;
        }
        const [keys, j] = parseKeyPath(s, i, line);
        if (s[j] !== '=') throw new TomlError(`line ${line}: expected =`);
        let k = j + 1;
        while (s[k] === ' ' || s[k] === '\t') k += 1;
        const [value, end] = parseValue(s, k, line);
        rest(s, end, line);
        let t = table;
        for (const key of keys.slice(0, -1)) {
            if (t[key] === undefined) t[key] = {};
            t = t[key];
        }
        const last = keys[keys.length - 1];
        if (Object.prototype.hasOwnProperty.call(t, last)) throw new TomlError(`line ${line}: ${last} is set twice`);
        t[last] = value;
    });
    return root;
}

/** A TOML basic string, as the CLI writes it (JSON's escapes; non-ASCII as itself). */
function quote(v) {
    return JSON.stringify(String(v));
}

/** The file as the CLI writes it (cairn_cli.config._dumps), so either can rewrite what the other wrote. */
function dumpConfig(data) {
    const lines = ['# Cairn command line: profiles (written by `cairn login` and `cairn profiles`; no secrets here).'];
    if (data.default_profile) lines.push(`default_profile = ${quote(data.default_profile)}`);
    const profiles = data.profiles || {};
    for (const name of Object.keys(profiles).sort()) {
        lines.push('', `[profiles.${quote(name)}]`);
        const entry = profiles[name] || {};
        for (const k of Object.keys(entry).sort()) {
            const v = entry[k];
            if (typeof v === 'boolean') lines.push(`${k} = ${v ? 'true' : 'false'}`);
            else if (typeof v === 'number') lines.push(`${k} = ${v}`);
            else if (v !== null && v !== undefined) lines.push(`${k} = ${quote(v)}`);
        }
    }
    return `${lines.join('\n')}\n`;
}

/** The CLI's profiles: read, change and save `config.toml`. */
class Profiles {
    constructor(dir = configDir()) {
        this.dir = dir;
        this.file = path.join(dir, 'config.toml');
    }

    /** {profiles: {name: {url, ...}}, default_profile} (empty when there is no file yet). Throws on a bad file. */
    load() {
        let text;
        try {
            text = fs.readFileSync(this.file, 'utf8');
        } catch (e) {
            if (e.code === 'ENOENT') return { profiles: {} };
            throw e;
        }
        let data;
        try {
            data = parseToml(text);
        } catch (e) {
            throw new Error(`${this.file}: ${e.message}; fix it, or run \`cairn profiles\` to see what the command line reads`);
        }
        const profiles = {};
        if (data.profiles !== undefined && (typeof data.profiles !== 'object' || data.profiles === null)) {
            throw new Error(`${this.file}: [profiles] must be a table`);
        }
        for (const [name, entry] of Object.entries(data.profiles || {})) {
            if (entry && typeof entry === 'object' && typeof entry.url === 'string') profiles[name] = { ...entry };
        }
        data.profiles = profiles;
        return data;
    }

    save(data) {
        fs.mkdirSync(this.dir, { recursive: true, mode: 0o700 });
        const tmp = path.join(this.dir, `.config.toml.${process.pid}`);
        fs.writeFileSync(tmp, dumpConfig(data), { mode: 0o600 });
        fs.renameSync(tmp, this.file);
    }

    /** Workspaces: [{name, url, default}] sorted by name. */
    list() {
        const data = this.load();
        return Object.keys(data.profiles).sort().map((name) => ({
            name, url: data.profiles[name].url, default: data.default_profile === name,
        }));
    }

    /** Adds or updates a profile (other keys the CLI keeps, such as ca_file, are left as they are). */
    put(name, url) {
        if (!PROFILE_NAME.test(name)) throw new Error(`profile name ${name}: letters, digits, “.”, “_” and “-”`);
        const data = this.load();
        data.profiles[name] = { ...(data.profiles[name] || {}), url };
        if (!data.default_profile || !data.profiles[data.default_profile]) data.default_profile = name;
        this.save(data);
    }

    remove(name) {
        const data = this.load();
        if (!data.profiles[name]) return false;
        delete data.profiles[name];
        if (data.default_profile === name) {
            delete data.default_profile;
            const left = Object.keys(data.profiles).sort();
            if (left.length) data.default_profile = left[0];
        }
        this.save(data);
        return true;
    }

    setDefault(name) {
        const data = this.load();
        if (!data.profiles[name]) throw new Error(`no profile ${name}`);
        data.default_profile = name;
        this.save(data);
    }
}

module.exports = { configDir, parseToml, dumpConfig, Profiles, TomlError };
