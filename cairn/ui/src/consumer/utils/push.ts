/**
 * Web push for this device: the browser's push subscription, kept by Cairn for the signed-in person.
 * Works where the workspace runs as the installable app (its service worker, sw.js, is registered); the push
 * carries nothing, and the service worker fetches the newest unread notification from Cairn when it arrives.
 */
import type { ConsumerClient } from '../api/client';

export function pushSupported(): boolean {
    return typeof window !== 'undefined' && 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

function keyBytes(b64url: string): Uint8Array {
    const pad = '='.repeat((4 - (b64url.length % 4)) % 4);
    const raw = atob((b64url + pad).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

/** The service worker's registration, when the app registered one (null in the catalog shell or the demo). */
export async function registration(): Promise<ServiceWorkerRegistration | null> {
    if (!pushSupported()) return null;
    return (await navigator.serviceWorker.getRegistration()) ?? null;
}

export async function deviceSubscribed(): Promise<boolean> {
    const reg = await registration();
    return !!(reg && (await reg.pushManager.getSubscription()));
}

export async function enablePush(client: ConsumerClient, publicKey: string): Promise<void> {
    const reg = await registration();
    if (!reg) throw new Error('Open Cairn as an app (install it, or open /cairn-ui/app.html) to get notifications on this device.');
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') throw new Error('Notifications are blocked for Cairn in this browser.');
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(publicKey) });
    const json = sub.toJSON();
    await client.subscribePush({ endpoint: sub.endpoint, keys: (json.keys ?? {}) as Record<string, string> });
}

export async function disablePush(client: ConsumerClient): Promise<void> {
    const reg = await registration();
    const sub = reg ? await reg.pushManager.getSubscription() : null;
    if (!sub) return;
    await client.unsubscribePush(sub.endpoint);
    await sub.unsubscribe();
}
