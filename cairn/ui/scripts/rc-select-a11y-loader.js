/* eslint-disable */
// rc-select 14.1 (antd 4's Select) gives its combobox input aria-expanded only once it has been opened, and points
// aria-owns / aria-controls / aria-activedescendant at a listbox that exists only while open: two WCAG failures
// on every closed Select (axe: aria-required-attr, aria-valid-attr-value). This bundle carries its own antd, so
// the input is corrected here at build time. Each replacement must match, so an rc-select upgrade that changes
// this code fails the build instead of quietly dropping the fix.
const FIXES = [
    ["'aria-expanded': open,", "'aria-expanded': !!open,"],
    ["'aria-owns': \"\".concat(id, \"_list\"),", "'aria-owns': open ? \"\".concat(id, \"_list\") : undefined,"],
    ["'aria-controls': \"\".concat(id, \"_list\"),", "'aria-controls': open ? \"\".concat(id, \"_list\") : undefined,"],
    ["'aria-activedescendant': activeDescendantId", "'aria-activedescendant': open ? activeDescendantId : undefined"],
];

module.exports = function rcSelectA11y(source) {
    let out = source;
    for (const [from, to] of FIXES) {
        if (!out.includes(from)) {
            throw new Error(`rc-select-a11y-loader: ${this.resourcePath} no longer contains ${from}; review the fix`);
        }
        out = out.replace(from, to);
    }
    return out;
};
module.exports.FIXES = FIXES;
