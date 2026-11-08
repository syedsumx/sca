/* The sign-in dialog starts on the add-in's own origin (Office requires it), then goes to Cairn's authorization
   page, and only there: any other address is refused. */
(function () {
    'use strict';
    var go = new URLSearchParams(location.search).get('go') || '';
    var api = String((window.CAIRN_ADDIN_CONFIG || {}).apiBase || '').replace(/\/+$/, '');
    if (api && go.indexOf(api + '/oauth/authorize?') === 0) {
        location.replace(go);
    } else {
        document.getElementById('msg').textContent = 'This sign-in link is not for this Cairn.';
    }
})();
