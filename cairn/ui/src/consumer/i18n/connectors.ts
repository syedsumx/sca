/**
 * Messages of governed data connectors (L152): Ingestion › Connectors (NiFi), managed Apache NiFi deployments and the
 * flows Cairn deploys to them. English is the source and German complete; French, Spanish and Japanese have the
 * titles (the rest falls back to English).
 */
export const CONNECTORS_EN = {
    'nifi.title': 'Connectors (NiFi)',
    'nifi.intro': 'Apache NiFi’s connectors, run by Cairn: flows are kept in Cairn and deployed by it, secrets come only from secret scopes, and what lands in the lakehouse is written as the flow’s owner. Each flow is a pipeline in the catalog, with its lineage and runs.',
    'nifi.deployments': 'Deployments',
    'nifi.flows': 'Flows',
    'nifi.newDeployment': 'New deployment',
    'nifi.newFlow': 'New flow',
    'nifi.notEnabled': 'Managed NiFi is not enabled on this Cairn (CAIRN_NIFI_RUNTIME).',
    'nifi.noDeployments': 'No NiFi deployments yet',
    'nifi.noFlows': 'No flows yet',
    'nifi.openCanvas': 'Open NiFi',
    'nifi.canvasHint': 'The canvas opens through Cairn, signed in as you; what you may change is decided for each request.',
    'nifi.version': 'NiFi',
    'nifi.size': 'Size',
    'nifi.flowCount': '{n} flow(s)',
    'nifi.owner': 'Owner',
    'nifi.lands': 'Lands in',
    'nifi.state': 'State',
    'nifi.synced': 'Last run read',
    'nifi.start': 'Start',
    'nifi.stop': 'Stop',
    'nifi.redeploy': 'Deploy again',
    'nifi.delete': 'Delete',
    'nifi.deleteDeployment': 'Delete {name} and its flows?',
    'nifi.readRuns': 'Read runs now',
    'nifi.started': 'Started',
    'nifi.stopped': 'Stopped',
    'nifi.deployed': 'Deployed',
    'nifi.deleted': 'Deleted',
    'nifi.noRun': 'Nothing happened since the last read',
    'nifi.runRead': 'Run read: {state}',
    'nifi.secretsNote': 'Sensitive parameters are references to secret scopes; their values never appear here or in NiFi’s exports.',
};

type Catalog = Partial<Record<keyof typeof CONNECTORS_EN, string>>;

export const CONNECTORS_DE: Catalog = {
    'nifi.title': 'Konnektoren (NiFi)',
    'nifi.intro': 'Die Konnektoren von Apache NiFi, betrieben von Cairn: Flows werden in Cairn gehalten und von Cairn bereitgestellt, Geheimnisse kommen nur aus Secret Scopes, und was im Lakehouse landet, wird als Eigentümer des Flows geschrieben. Jeder Flow ist eine Pipeline im Katalog, mit seiner Herkunft und seinen Läufen.',
    'nifi.deployments': 'Deployments',
    'nifi.flows': 'Flows',
    'nifi.newDeployment': 'Neues Deployment',
    'nifi.newFlow': 'Neuer Flow',
    'nifi.notEnabled': 'Verwaltetes NiFi ist in diesem Cairn nicht aktiviert (CAIRN_NIFI_RUNTIME).',
    'nifi.noDeployments': 'Noch keine NiFi-Deployments',
    'nifi.noFlows': 'Noch keine Flows',
    'nifi.openCanvas': 'NiFi öffnen',
    'nifi.canvasHint': 'Die Arbeitsfläche öffnet sich über Cairn, angemeldet als Sie; was Sie ändern dürfen, wird für jede Anfrage entschieden.',
    'nifi.version': 'NiFi',
    'nifi.size': 'Größe',
    'nifi.flowCount': '{n} Flow(s)',
    'nifi.owner': 'Eigentümer',
    'nifi.lands': 'Landet in',
    'nifi.state': 'Status',
    'nifi.synced': 'Zuletzt gelesener Lauf',
    'nifi.start': 'Starten',
    'nifi.stop': 'Anhalten',
    'nifi.redeploy': 'Erneut bereitstellen',
    'nifi.delete': 'Löschen',
    'nifi.deleteDeployment': '{name} und seine Flows löschen?',
    'nifi.readRuns': 'Läufe jetzt lesen',
    'nifi.started': 'Gestartet',
    'nifi.stopped': 'Angehalten',
    'nifi.deployed': 'Bereitgestellt',
    'nifi.deleted': 'Gelöscht',
    'nifi.noRun': 'Seit dem letzten Lesen ist nichts passiert',
    'nifi.runRead': 'Lauf gelesen: {state}',
    'nifi.secretsNote': 'Sensible Parameter sind Verweise auf Secret Scopes; ihre Werte erscheinen weder hier noch in den Exporten von NiFi.',
};

export const CONNECTORS_FR: Catalog = {
    'nifi.title': 'Connecteurs (NiFi)',
    'nifi.deployments': 'Déploiements',
    'nifi.flows': 'Flux',
    'nifi.openCanvas': 'Ouvrir NiFi',
};

export const CONNECTORS_ES: Catalog = {
    'nifi.title': 'Conectores (NiFi)',
    'nifi.deployments': 'Despliegues',
    'nifi.flows': 'Flujos',
    'nifi.openCanvas': 'Abrir NiFi',
};

export const CONNECTORS_JA: Catalog = {
    'nifi.title': 'コネクタ (NiFi)',
    'nifi.deployments': 'デプロイメント',
    'nifi.flows': 'フロー',
    'nifi.openCanvas': 'NiFi を開く',
};
