import React, { useState } from 'react';
import ReactDOM from 'react-dom';

import { demoFleet } from './demoFleet';
import { FleetConsole, SignIn, httpApi } from './FleetConsole';

declare const FLEET_DEMO: boolean;

function App() {
    const [token, setToken] = useState<string | null>(() => {
        try { return sessionStorage.getItem('cairn-fleet-token'); } catch { return null; }
    });
    if (FLEET_DEMO) return <FleetConsole api={demoFleet} />;
    if (!token)
        return <SignIn onToken={(t) => { try { sessionStorage.setItem('cairn-fleet-token', t); } catch { /* private tab */ } setToken(t); }} />;
    return <FleetConsole api={httpApi('', token)} onSignOut={() => {
        try { sessionStorage.removeItem('cairn-fleet-token'); } catch { /* private tab */ }
        setToken(null);
    }} />;
}

ReactDOM.render(<App />, document.getElementById('cairn-root'));
