/**
 * Messages of Snowflake and Databricks tables in place (L151): the cards above Lakehouse › External catalogs that open
 * the warehouse presets' forms. English is the source and German complete; French, Spanish and Japanese have the
 * titles (the rest falls back to English).
 */
export const WAREHOUSES_EN = {
    'wh.title': 'Connect a warehouse',
    'wh.intro': 'Read Snowflake and Databricks tables where they are. Their tables join the catalog under the warehouse’s name, and the same grants, masks and row filters apply as on the lakehouse.',
    'wh.inPlace': 'In place',
    'wh.live': 'Live queries',
    'wh.perVersion': 'Copied per version',
    'wh.connect': 'Connect {name}',
    'wh.secrets': 'Credentials stay in a secret scope: the form takes a reference, never the value.',
    'wh.openCatalog.title': 'Snowflake Open Catalog',
    'wh.openCatalog.body': 'Iceberg tables in Open Catalog (managed Apache Polaris), with a service connection’s client ID and secret.',
    'wh.horizon.title': 'Snowflake-managed Iceberg',
    'wh.horizon.body': 'Iceberg tables through Snowflake Horizon’s Iceberg REST endpoint, with a programmatic access token and a role.',
    'wh.unity.title': 'Databricks Unity Catalog',
    'wh.unity.body': 'Managed Iceberg and UniForm tables through Unity Catalog’s Iceberg REST endpoint, with a personal access token or a service principal.',
    'wh.snowflakeLive.title': 'Snowflake, live',
    'wh.snowflakeLive.body': 'Any Snowflake table through Trino’s Snowflake connector. The queries run on your Snowflake warehouse and Snowflake bills them.',
    'wh.deltaSharing.title': 'Databricks Delta Sharing',
    'wh.deltaSharing.body': 'A table shared with you, read with the provider’s profile file into a governed lakehouse table at each new version.',
};

type Catalog = Partial<Record<keyof typeof WAREHOUSES_EN, string>>;

export const WAREHOUSES_DE: Record<keyof typeof WAREHOUSES_EN, string> = {
    'wh.title': 'Ein Data Warehouse verbinden',
    'wh.intro': 'Snowflake- und Databricks-Tabellen dort lesen, wo sie liegen. Ihre Tabellen kommen unter dem Namen des Warehouses in den Katalog, und es gelten dieselben Berechtigungen, Maskierungen und Zeilenfilter wie im Lakehouse.',
    'wh.inPlace': 'Am Ort',
    'wh.live': 'Live-Abfragen',
    'wh.perVersion': 'Je Version kopiert',
    'wh.connect': '{name} verbinden',
    'wh.secrets': 'Zugangsdaten bleiben in einem Secret Scope: Das Formular nimmt einen Verweis, nie den Wert.',
    'wh.openCatalog.title': 'Snowflake Open Catalog',
    'wh.openCatalog.body': 'Iceberg-Tabellen in Open Catalog (verwaltetes Apache Polaris), mit Client-ID und Secret einer Service-Verbindung.',
    'wh.horizon.title': 'Von Snowflake verwaltetes Iceberg',
    'wh.horizon.body': 'Iceberg-Tabellen über den Iceberg-REST-Endpunkt von Snowflake Horizon, mit einem Programmatic Access Token und einer Rolle.',
    'wh.unity.title': 'Databricks Unity Catalog',
    'wh.unity.body': 'Verwaltete Iceberg- und UniForm-Tabellen über den Iceberg-REST-Endpunkt von Unity Catalog, mit einem Personal Access Token oder einem Service Principal.',
    'wh.snowflakeLive.title': 'Snowflake, live',
    'wh.snowflakeLive.body': 'Jede Snowflake-Tabelle über den Snowflake-Connector von Trino. Die Abfragen laufen auf Ihrem Snowflake-Warehouse und werden von Snowflake abgerechnet.',
    'wh.deltaSharing.title': 'Databricks Delta Sharing',
    'wh.deltaSharing.body': 'Eine mit Ihnen geteilte Tabelle, mit der Profildatei des Anbieters bei jeder neuen Version in eine geregelte Lakehouse-Tabelle gelesen.',
};

export const WAREHOUSES_FR: Catalog = {
    'wh.title': 'Connecter un entrepôt de données',
    'wh.inPlace': 'Sur place',
    'wh.live': 'Requêtes en direct',
    'wh.connect': 'Connecter {name}',
};

export const WAREHOUSES_ES: Catalog = {
    'wh.title': 'Conectar un almacén de datos',
    'wh.inPlace': 'En su sitio',
    'wh.live': 'Consultas en vivo',
    'wh.connect': 'Conectar {name}',
};

export const WAREHOUSES_JA: Catalog = {
    'wh.title': 'データウェアハウスを接続',
    'wh.inPlace': 'その場で',
    'wh.live': 'ライブクエリ',
    'wh.connect': '{name} を接続',
};
