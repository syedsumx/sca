'use strict';
document.getElementById('form').addEventListener('submit', (e) => {
    e.preventDefault();
    const error = document.getElementById('error');
    error.textContent = '';
    window.cairnDesktop.setCairnUrl(document.getElementById('url').value).catch((err) => {
        error.textContent = String(err && err.message ? err.message : err).replace(/^Error invoking remote method '[^']+': (Error: )?/, '');
    });
});
