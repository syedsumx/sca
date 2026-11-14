/* eslint-disable */
// For the unit tests: Node loads antd (and rc-select) itself, outside the bundlers, so the ARIA correction of
// rc-select-a11y-loader.js is applied here as the module is required, exactly as the MFE and demo builds apply it.
const fs = require('fs');
const Module = require('module');
const fix = require('./rc-select-a11y-loader.js');

const RC_SELECT_INPUT = /rc-select[\\/](es|lib)[\\/]Selector[\\/]Input\.js$/;
if (!Module._extensions['.js'].__cairnA11y) {
    const original = Module._extensions['.js'];
    const patched = function (module, filename) {
        if (RC_SELECT_INPUT.test(filename)) {
            module._compile(fix.call({ resourcePath: filename }, fs.readFileSync(filename, 'utf8')), filename);
            return;
        }
        original(module, filename);
    };
    patched.__cairnA11y = true;
    Module._extensions['.js'] = patched;
}
