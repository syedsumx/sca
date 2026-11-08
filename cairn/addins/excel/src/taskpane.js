/* The task pane: sign in and out, browse governed metrics and insert them (as a live formula or as values). */
(function () {
    'use strict';

    var client = CairnAddin.fromConfig();
    var $ = function (id) { return document.getElementById(id); };
    var chosen = null, searchTimer = null;

    function status(text, isError) {
        $('status').textContent = text || '';
        $('status').className = isError ? 'error' : '';
    }

    function show(signedIn) {
        $('signed-in').hidden = !signedIn;
        $('signed-out').hidden = signedIn;
    }

    function refresh() {
        return client.signedIn().then(function (yes) {
            if (!yes) { show(false); return; }
            return client.me().then(function (me) {
                $('who').textContent = String(me.urn || '').replace(/^urn:li:corpuser:/, '');
                show(true);
                return loadMetrics('');
            });
        }).catch(function (e) { show(false); status(e.message, true); });
    }

    function signIn() {
        status('Opening Cairn…');
        client.beginSignIn().then(function (begun) {
            var page = location.origin + '/dialog.html?go=' + encodeURIComponent(begun.url);
            Office.context.ui.displayDialogAsync(page, { height: 70, width: 35, promptBeforeOpen: false }, function (res) {
                if (res.status !== Office.AsyncResultStatus.Succeeded) { status(res.error.message, true); return; }
                var dialog = res.value;
                dialog.addEventHandler(Office.EventType.DialogMessageReceived, function (arg) {
                    dialog.close();
                    client.completeSignIn(arg.message).then(function () { status(''); return refresh(); })
                        .catch(function (e) { status(e.message, true); });
                });
                dialog.addEventHandler(Office.EventType.DialogEventReceived, function () { status('Sign-in closed.'); });
            });
        }).catch(function (e) { status(e.message, true); });
    }

    function loadMetrics(q) {
        return client.metrics(q).then(function (list) {
            var ul = $('metrics');
            ul.textContent = '';
            if (!list.length) {
                var li = document.createElement('li');
                li.className = 'muted';
                li.textContent = q ? 'No metric matches.' : 'No metrics you may see yet.';
                ul.appendChild(li);
            }
            list.forEach(function (m) {
                var li = document.createElement('li');
                var b = document.createElement('button');
                b.type = 'button';
                b.className = 'metric';
                b.textContent = m.label + (m.certified ? ' ✓' : '');
                b.setAttribute('aria-label', m.label + (m.certified ? ' (certified)' : ''));
                b.addEventListener('click', function () { pick(m); });
                li.appendChild(b);
                ul.appendChild(li);
            });
        });
    }

    function pick(m) {
        chosen = m;
        $('metric-form').hidden = false;
        $('metric-name').textContent = m.label + ' (' + m.name + ')';
        $('metric-description').textContent = m.description || '';
        var dims = $('metric-dimensions');
        dims.textContent = '';
        (m.dimensions || []).forEach(function (d, i) {
            var id = 'dim-' + i;
            var box = document.createElement('input');
            box.type = 'checkbox'; box.id = id; box.value = d;
            var label = document.createElement('label');
            label.htmlFor = id; label.textContent = d; label.className = 'check';
            label.prepend(box);
            dims.appendChild(label);
        });
        if (!(m.dimensions || []).length) dims.textContent = 'This metric lists no dimensions.';
    }

    function writeToSelection(values) {
        return Excel.run(function (ctx) {
            var cell = ctx.workbook.getSelectedRange().getCell(0, 0);
            var target = cell.getResizedRange(values.length - 1, values[0].length - 1);
            if (values.length === 1 && values[0].length === 1 && String(values[0][0]).charAt(0) === '=') target.formulas = values;
            else target.values = values;
            return ctx.sync();
        });
    }

    function insertMetric(ev) {
        ev.preventDefault();
        if (!chosen) return;
        var mode = ev.submitter ? ev.submitter.value : 'formula';
        var dims = Array.prototype.slice.call(document.querySelectorAll('#metric-dimensions input:checked'))
            .map(function (b) { return b.value; }).join(', ');
        var filters = $('metric-filters').value.trim();
        if (mode === 'formula') {
            writeToSelection([[CairnAddin.formula('CAIRN.METRIC', [chosen.name, dims, filters])]])
                .then(function () { status('Inserted.'); }, function (e) { status(e.message, true); });
            return;
        }
        status('Running…');
        client.metric(chosen.name, dims || null, filters || null).then(writeToSelection)
            .then(function () { status('Inserted.'); }, function (e) { status(e.message, true); });
    }

    Office.onReady(function () {
        $('sign-in').addEventListener('click', signIn);
        $('sign-out').addEventListener('click', function () {
            client.signOut().then(function () { status('Signed out.'); show(false); });
        });
        $('metric-search').addEventListener('input', function (e) {
            clearTimeout(searchTimer);
            var q = e.target.value.trim();
            searchTimer = setTimeout(function () { loadMetrics(q).catch(function (err) { status(err.message, true); }); }, 250);
        });
        $('metric-form').addEventListener('submit', insertMetric);
        $('insert-query').addEventListener('click', function () {
            var sql = $('sql').value.trim();
            if (!sql) return;
            writeToSelection([[CairnAddin.formula('CAIRN.QUERY', [sql])]])
                .then(function () { status('Inserted.'); }, function (e) { status(e.message, true); });
        });
        refresh();
    });
})();
