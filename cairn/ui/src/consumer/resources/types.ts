import type { IconName } from '../components/Icons';

export interface ResourceAction {
    label: string;
    method: 'POST' | 'PUT' | 'PATCH' | 'DELETE';
    /** `{field}` is filled from the row; paths without one are page-level actions (create, run all). */
    path: string;
    /** An example request body, editable before sending; null for none. */
    body?: unknown;
}

export interface ResourceSpec {
    key: string;
    title: string;
    /** The GET that lists the area's items (null: the API has no list). */
    path: string | null;
    /** Where the items sit in the answer (null: the answer is the array itself). */
    listKey: string | null;
    description: string;
    /** Fields shown as table columns, in order (the rest are in the row's details). */
    columns: string[];
    actions?: ResourceAction[];
    rowKey?: string;
    /** Fields holding epoch seconds rather than milliseconds. */
    seconds?: string[];
    /** A dedicated view instead of the table (the list endpoint is what it reads). */
    view?: 'ontology-graph';
    notes?: string;
}

export interface Hub {
    route: string;
    title: string;
    icon: IconName;
    description: string;
    tabs: string[];
}
