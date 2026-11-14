/* The offline page: try the app again, and go back to it as soon as the network returns. */
(function () {
    'use strict';
    function retry() { location.replace('app.html' + (location.hash || '#/discover')); }
    document.getElementById('retry').addEventListener('click', retry);
    window.addEventListener('online', retry);
})();
