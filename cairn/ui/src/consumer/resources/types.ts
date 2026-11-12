import type { IconName } from '../components/Icons';

export interface ResourceAction {
    label: string;
    method: 'POST' | 'PUT' | 'PATCH' | 'DELETE';
    /** `{field}` is filled from the row; paths without one are page-level actions (create, run all). */
    path: string;
    /** An example request body: the form's starting values and placeholders (and Raw JSON's); null for none. */
    body?: unknown;
    /** Values a new item's form starts with beyond the fields' defaults: one create form, several starting points
     *  (a warehouse preset, L151). The person can still change them. */
    values?: Record<string, unknown>;
    /** The body is sent as it is: the action is a confirmation, not a form (Pause, Hide, Require intents…). */
    fixed?: boolean;
    /** The form to draw (a key of forms.generated.json); default: the action's own `METHOD path`. */
    form?: string;
    /** A page-level action whose path names what the form creates (`PUT /workspaces/{id}`), not a row's. */
    scope?: 'page' | 'row';
}

export interface ResourceSpec {
    key: string;
    title: string;
    /** The GET that lists the area's items (null: the API has no list). */
    path: string | null;
    /** Where the items sit in the answer (null: the answer is the array itself). */
    listKey: string | null;
    description: string;
    /** The area's section of the in-product docs (`administration.html#secret-scopes`), linked as "Learn more". */
    docs?: string;
    /** Fields shown as table columns, in order (the rest are in the row's details). */
    columns: string[];
    actions?: ResourceAction[];
    rowKey?: string;
    /** Fields holding epoch seconds rather than milliseconds. */
    seconds?: string[];
    /** A dedicated view instead of the table (the list endpoint is what it reads). */
    view?: 'ontology-graph' | 'postgres-instances' | 'airflow-deployments';
    notes?: string;
    /** The registry type the rows are (GET /v1/iac/schema), and the row field holding its Terraform id (default id):
     *  a row's details then offer "Copy as Terraform" and "Copy as YAML" (GET /v1/iac/export). */
    iac?: { kind: string; id?: string };
}

export interface Hub {
    route: string;
    title: string;
    icon: IconName;
    description: string;
    tabs: string[];
}
