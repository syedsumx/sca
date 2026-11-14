// One HTTP request, read from stdin as JSON and answered on stdout: how the test harness makes Apps Script's
// UrlFetchApp.fetch synchronous in Node (it runs this as a child process and waits).
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { input += c; });
process.stdin.on('end', async () => {
    const req = JSON.parse(input);
    try {
        const r = await fetch(req.url, { method: req.method, headers: req.headers, body: req.body ?? undefined, redirect: 'manual' });
        process.stdout.write(JSON.stringify({ status: r.status, headers: Object.fromEntries(r.headers), body: await r.text() }));
    } catch (e) {
        process.stdout.write(JSON.stringify({ error: String(e && e.message) }));
    }
});
