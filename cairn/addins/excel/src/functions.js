/*
 * Cairn's custom functions for Excel: =CAIRN.QUERY(sql), =CAIRN.METRIC(name, dimensions, filters), =CAIRN.ASK(question).
 * Each spills a grid (headers first) and runs as the person signed in to the add-in: their masks, row filters and
 * table permissions apply. Metadata: functions.json; loaded by taskpane.html (the shared runtime).
 */
(function (root) {
    'use strict';

    var client = null;
    function cairn() {
        if (!client) client = root.CairnAddin.fromConfig();
        return client;
    }

    function fail(e) {
        var msg = (e && e.message) || String(e);
        if (e && e.signIn) msg = 'Sign in to Cairn: Home ▸ Cairn ▸ Sign in (' + msg + ')';
        var code = e && e.status === 403 ? root.CustomFunctions.ErrorCode.invalidValue : root.CustomFunctions.ErrorCode.notAvailable;
        throw new root.CustomFunctions.Error(code, msg.slice(0, 255));
    }

    function text(v) {
        if (v === null || v === undefined) return '';
        if (Array.isArray(v)) return v;          // a range: the server reads its cells
        return String(v);
    }

    /**
     * Runs read-only SQL in Cairn as you.
     * @customfunction QUERY
     * @param {string} sql A SELECT over catalogued tables (schema.table).
     * @param {string} [connection] The connection; empty for the default.
     * @returns {any[][]} The rows, headers first.
     */
    function query(sql, connection) {
        return cairn().query(sql, text(connection) || null).catch(fail);
    }

    /**
     * A governed metric, broken down and filtered.
     * @customfunction METRIC
     * @param {string} name The metric's name.
     * @param {any[][]} [dimensions] Dimensions: "region, country", or a range of names.
     * @param {any[][]} [filters] Filters such as "region = EU"; several separated by ";", or a range.
     * @returns {any[][]} The metric per dimension, headers first.
     */
    function metric(name, dimensions, filters) {
        return cairn().metric(name, text(dimensions) || null, text(filters) || null).catch(fail);
    }

    /**
     * Asks Cairn a question of your data; the answer's rows.
     * @customfunction ASK
     * @param {string} question The question.
     * @returns {any[][]} The answer, headers first.
     */
    function ask(question) {
        return cairn().ask(question).catch(fail);
    }

    root.CustomFunctions.associate('QUERY', query);
    root.CustomFunctions.associate('METRIC', metric);
    root.CustomFunctions.associate('ASK', ask);
    root.CairnFunctions = { query: query, metric: metric, ask: ask, reset: function () { client = null; } };
})(typeof globalThis !== 'undefined' ? globalThis : this);
