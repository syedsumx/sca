// A question Cairn's own palette, search bar or help panel hands to the assistant, to be sent at once. It is kept in
// memory, not in the address or history state (hash history drops state): a link from outside the app cannot set it,
// so a bare ``/assistant?q=…`` is only put in the box (the assistant acts as the signed-in person).
let pending: string | null = null;

export function handOffQuestion(question: string): void {
    pending = question;
}

/** True, once, when ``question`` is the one Cairn just handed over. */
export function takeHandedOff(question: string | undefined): boolean {
    const ok = !!question && pending === question;
    pending = null;
    return ok;
}
