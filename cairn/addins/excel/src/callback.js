/* Cairn redirected back here with ?code=…&state=…: hand the address to the task pane, which checks the state and
   exchanges the code with its PKCE verifier (the code alone is useless). */
Office.onReady(function () {
    Office.context.ui.messageParent(location.href);
});
