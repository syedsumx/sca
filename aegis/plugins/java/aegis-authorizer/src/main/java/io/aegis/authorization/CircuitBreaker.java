package io.aegis.authorization;

import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.LongSupplier;

/**
 * Consecutive-failure circuit breaker protecting GMS request threads from a slow or dead PDP.
 *
 * <p>CLOSED → (failureThreshold consecutive failures) → OPEN for openSeconds → HALF_OPEN, where a
 * single probe request is let through; success closes the circuit, failure re-opens it.
 */
final class CircuitBreaker {

  enum State {
    CLOSED,
    OPEN,
    HALF_OPEN
  }

  private final int failureThreshold;
  private final long openNanos;
  private final LongSupplier clock;
  private final AtomicInteger consecutiveFailures = new AtomicInteger();
  private final AtomicLong openedAt = new AtomicLong(Long.MIN_VALUE);
  private final AtomicLong probeInFlightSince = new AtomicLong(Long.MIN_VALUE);

  CircuitBreaker(int failureThreshold, long openSeconds, LongSupplier nanoClock) {
    this.failureThreshold = failureThreshold;
    this.openNanos = openSeconds * 1_000_000_000L;
    this.clock = nanoClock;
  }

  State state() {
    long opened = openedAt.get();
    if (opened == Long.MIN_VALUE) {
      return State.CLOSED;
    }
    return clock.getAsLong() - opened >= openNanos ? State.HALF_OPEN : State.OPEN;
  }

  /** Returns true if a call to the PDP may be attempted now. */
  boolean allowRequest() {
    State s = state();
    if (s == State.CLOSED) {
      return true;
    }
    if (s == State.OPEN) {
      return false;
    }
    // HALF_OPEN: allow exactly one probe at a time (a stuck probe is released after openNanos).
    long now = clock.getAsLong();
    long since = probeInFlightSince.get();
    if (since != Long.MIN_VALUE && now - since < openNanos) {
      return false;
    }
    return probeInFlightSince.compareAndSet(since, now);
  }

  void recordSuccess() {
    consecutiveFailures.set(0);
    openedAt.set(Long.MIN_VALUE);
    probeInFlightSince.set(Long.MIN_VALUE);
  }

  void recordFailure() {
    probeInFlightSince.set(Long.MIN_VALUE);
    if (state() == State.HALF_OPEN) {
      openedAt.set(clock.getAsLong());
      return;
    }
    if (consecutiveFailures.incrementAndGet() >= failureThreshold) {
      openedAt.compareAndSet(Long.MIN_VALUE, clock.getAsLong());
    }
  }
}
