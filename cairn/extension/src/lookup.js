// Fetches the catalog card for a BI page with the user's Cairn session. No extension APIs, so it can be tested.

/** The card for a page key: {status: 'ok', data} | {status: 'unknown'} | {status: 'signed-out'} | {status: 'error', message}. */
export async function lookup(cairnUrl, key, fetchImpl = fetch) {
    if (!cairnUrl) return { status: 'unconfigured' };
    let resp;
    try {
        resp = await fetchImpl(`${cairnUrl}/cairn/api/embed/v1/context?url=${encodeURIComponent(key)}`, {
            credentials: 'include',
            headers: { Accept: 'application/json' },
        });
    } catch (e) {
        return { status: 'error', message: `Cannot reach ${cairnUrl}` };
    }
    if (resp.status === 401 || resp.status === 403) return { status: 'signed-out', login: `${cairnUrl}/login` };
    if (!resp.ok) return { status: 'error', message: `Cairn answered ${resp.status}` };
    const data = await resp.json();
    return data.urn ? { status: 'ok', data } : { status: 'unknown', page: data.page };
}
