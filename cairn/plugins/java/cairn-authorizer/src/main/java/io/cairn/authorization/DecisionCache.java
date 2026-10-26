package io.cairn.authorization;

import java.util.Iterator;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.LongSupplier;

/**
 * Bounded, thread-safe TTL cache for PDP decisions.
 *
 * <p>Deliberately simple: when the size bound is hit, expired entries are purged first and, if
 * still full, an arbitrary ~10% of entries are evicted. Authorization traffic is dominated by a
 * small hot set (the same actor browsing the same assets), so approximate eviction is fine and
 * avoids pulling Caffeine into the plugin classloader.
 */
final class DecisionCache {

  static final class Entry {
    final PdpDecision decision;
    final long expiresAtNanos;

    Entry(PdpDecision decision, long expiresAtNanos) {
      this.decision = decision;
      this.expiresAtNanos = expiresAtNanos;
    }
  }

  private final ConcurrentHashMap<String, Entry> map = new ConcurrentHashMap<>();
  private final long defaultTtlNanos;
  private final int maxEntries;
  private final LongSupplier clock;

  DecisionCache(long ttlSeconds, int maxEntries, LongSupplier nanoClock) {
    this.defaultTtlNanos = ttlSeconds * 1_000_000_000L;
    this.maxEntries = maxEntries;
    this.clock = nanoClock;
  }

  boolean enabled() {
    return defaultTtlNanos > 0 && maxEntries > 0;
  }

  PdpDecision get(String key) {
    if (!enabled()) {
      return null;
    }
    Entry e = map.get(key);
    if (e == null) {
      return null;
    }
    if (clock.getAsLong() - e.expiresAtNanos >= 0) {
      map.remove(key, e);
      return null;
    }
    return e.decision;
  }

  /**
   * Stores a decision. The PDP may shorten (never lengthen) the TTL via {@code ttl_seconds}; a TTL
   * of 0 from the PDP means "do not cache" (e.g. time-of-day dependent rules).
   */
  void put(String key, PdpDecision decision) {
    if (!enabled()) {
      return;
    }
    long ttl = defaultTtlNanos;
    if (decision.ttlSeconds != null) {
      if (decision.ttlSeconds <= 0) {
        return;
      }
      ttl = Math.min(ttl, decision.ttlSeconds * 1_000_000_000L);
    }
    if (map.size() >= maxEntries) {
      evict();
    }
    map.put(key, new Entry(decision, clock.getAsLong() + ttl));
  }

  void invalidateAll() {
    map.clear();
  }

  int size() {
    return map.size();
  }

  private void evict() {
    long now = clock.getAsLong();
    map.entrySet().removeIf(e -> now - e.getValue().expiresAtNanos >= 0);
    if (map.size() < maxEntries) {
      return;
    }
    int toRemove = Math.max(1, maxEntries / 10);
    Iterator<Map.Entry<String, Entry>> it = map.entrySet().iterator();
    while (toRemove-- > 0 && it.hasNext()) {
      it.next();
      it.remove();
    }
  }
}
