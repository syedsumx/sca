'use strict';
// Workspaces are the CLI's profiles: the same config.toml, read and written by both (and checked against the CLI's
// own reader and writer, cairn_cli.config, when Python 3.11+ is here).
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { Profiles, configDir, parseToml, dumpConfig } = require('../src/profiles');

const SRC = path.join(__dirname, '..', '..', 'src');
const python = (() => {
    for (const exe of [process.env.CAIRN_TEST_PYTHON, 'python3', 'python'].filter(Boolean)) {
        const r = spawnSync(exe, ['-c', 'import sys, tomllib; sys.path.insert(0, sys.argv[1]); import cairn_cli.config', SRC]);
        if (r.status === 0) return exe;
    }
    return null;
})();

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-profiles-'));

test('the configuration folder is the CLI\'s', () => {
    const home = '/home/ana';
    assert.equal(configDir({ CAIRN_CONFIG_DIR: '/etc/cairn-x' }, 'linux', home), '/etc/cairn-x');
    assert.equal(configDir({ CAIRN_CONFIG_DIR: '~/c' }, 'linux', home), '/home/ana/c');
    assert.equal(configDir({ XDG_CONFIG_HOME: '/x' }, 'linux', home), '/x/cairn');
    assert.equal(configDir({ APPDATA: 'C:\\Users\\ana\\AppData\\Roaming' }, 'win32', home), path.win32.join('C:\\Users\\ana\\AppData\\Roaming', 'cairn'));
    assert.equal(configDir({}, 'darwin', home), '/home/ana/.config/cairn');
    assert.equal(configDir({ APPDATA: 'C:\\x' }, 'linux', home), '/home/ana/.config/cairn');
});

test('the TOML the CLI writes, and what people hand-edit, is read; the rest is refused, never guessed', () => {
    const doc = parseToml([
        '\uFEFF# a comment', 'default_profile = "prod"   # trailing', '',
        '[profiles."prod"]', 'url = "https://cairn.example.com/cairn/api"', "ca_file = 'C:\\certs\\ca.pem'",
        '[profiles.dev-1]', 'url = "http://localhost:9002"', 'verify = false', 'timeout = 1_000', 'ratio = 0.5',
        '[profiles."ünï \\"q\\""]', 'url = "https://x.example/\\u00e9"',
    ].join('\r\n'));
    assert.equal(doc.default_profile, 'prod');
    assert.deepEqual(doc.profiles.prod, { url: 'https://cairn.example.com/cairn/api', ca_file: 'C:\\certs\\ca.pem' });
    assert.deepEqual(doc.profiles['dev-1'], { url: 'http://localhost:9002', verify: false, timeout: 1000, ratio: 0.5 });
    assert.equal(doc.profiles['ünï "q"'].url, 'https://x.example/é');
    for (const bad of ['a = [1, 2]', 'a = { b = 1 }', 'a = """x"""', '[[profiles]]', 'a = "x', 'a = 1\na = 2',
        'a = "x" y', '= 1', '[profiles', 'a = "\\q"', 'a = 1979-05-27']) {
        assert.throws(() => parseToml(bad), undefined, bad);
    }
});

test('adding, defaulting and removing profiles; other keys the CLI keeps survive', () => {
    const dir = tmp();
    const p = new Profiles(dir);
    assert.deepEqual(p.list(), []);
    p.put('prod', 'https://cairn.example.com/cairn/api');
    p.put('dev', 'http://localhost:9002/cairn/api');
    assert.deepEqual(p.list().map((x) => [x.name, x.default]), [['dev', false], ['prod', true]]);
    const text = fs.readFileSync(path.join(dir, 'config.toml'), 'utf8');
    assert.equal(text, dumpConfig(p.load()));
    if (process.platform !== 'win32') assert.equal(fs.statSync(path.join(dir, 'config.toml')).mode & 0o777, 0o600);
    fs.writeFileSync(path.join(dir, 'config.toml'), `${text}ca_file = "/etc/ca.pem"\n`);      // the CLI adds to prod
    p.put('prod', 'https://cairn2.example.com/cairn/api');
    assert.deepEqual(p.load().profiles.prod, { url: 'https://cairn2.example.com/cairn/api', ca_file: '/etc/ca.pem' });
    p.setDefault('dev');
    assert.equal(p.load().default_profile, 'dev');
    assert.equal(p.remove('dev'), true);
    assert.equal(p.load().default_profile, 'prod');
    assert.equal(p.remove('nope'), false);
    assert.throws(() => p.put('bad name', 'https://x'), /profile name/);
    fs.writeFileSync(path.join(dir, 'config.toml'), 'profiles = [1]\n');
    assert.throws(() => p.list(), /config\.toml/);
    assert.throws(() => p.put('x', 'https://x'), /config\.toml/);             // a file it cannot read is never rewritten
    assert.equal(fs.readFileSync(path.join(dir, 'config.toml'), 'utf8'), 'profiles = [1]\n');
});

test('the CLI reads what the desktop writes, and the desktop reads what the CLI writes', { skip: !python && 'needs Python 3.11+' }, () => {
    const dir = tmp();
    const p = new Profiles(dir);
    p.put('prod', 'https://cairn.example.com/cairn/api');
    p.put('dev.local_1', 'http://localhost:9002/café/cairn/api');
    const script = `
import json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from cairn_cli.config import Config
c = Config.load(Path(sys.argv[2]))
print(json.dumps({"default": c.default, "profiles": c.profiles}))
c.put("ci", "https://ci.example.com/cairn/api", "/etc/ssl/ci.pem")
c.set_default("ci")
c.save()
`;
    const r = spawnSync(python, ['-c', script, SRC, dir], { encoding: 'utf8' });
    assert.equal(r.status, 0, r.stderr);
    assert.deepEqual(JSON.parse(r.stdout), { default: 'prod', profiles: {
        prod: { url: 'https://cairn.example.com/cairn/api' }, 'dev.local_1': { url: 'http://localhost:9002/café/cairn/api' } } });
    const back = p.load();
    assert.equal(back.default_profile, 'ci');
    assert.deepEqual(back.profiles.ci, { url: 'https://ci.example.com/cairn/api', ca_file: '/etc/ssl/ci.pem' });
    assert.deepEqual(p.list().map((x) => x.name), ['ci', 'dev.local_1', 'prod']);
    // And the text is byte for byte what the CLI would write.
    assert.equal(fs.readFileSync(path.join(dir, 'config.toml'), 'utf8'), dumpConfig(back));
});
