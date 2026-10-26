package io.aegis.authorization;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/** Parsed response of {@code POST /governance/v1/authorize}. */
final class PdpDecision {

  enum Decision {
    ALLOW,
    DENY,
    NOT_APPLICABLE
  }

  final Decision decision;
  final String reason;
  final List<String> policyIds;
  final List<Object> obligations;
  /** Null when the PDP did not specify a TTL. */
  final Long ttlSeconds;

  PdpDecision(
      Decision decision,
      String reason,
      List<String> policyIds,
      List<Object> obligations,
      Long ttlSeconds) {
    this.decision = decision;
    this.reason = reason == null ? "" : reason;
    this.policyIds = policyIds == null ? Collections.emptyList() : policyIds;
    this.obligations = obligations == null ? Collections.emptyList() : obligations;
    this.ttlSeconds = ttlSeconds;
  }

  /**
   * Parses the PDP JSON body. Unknown decisions are treated as {@code DENY} (fail closed) rather
   * than an error so a newer PDP cannot accidentally widen access through an older plugin.
   */
  @SuppressWarnings("unchecked")
  static PdpDecision parse(String body) {
    Object parsed = MiniJson.parse(body);
    if (!(parsed instanceof Map)) {
      throw new IllegalArgumentException("PDP response is not a JSON object");
    }
    Map<String, Object> m = (Map<String, Object>) parsed;
    Object d = m.get("decision");
    if (!(d instanceof String)) {
      throw new IllegalArgumentException("PDP response missing 'decision'");
    }
    Decision decision;
    try {
      decision = Decision.valueOf(((String) d).trim().toUpperCase(Locale.ROOT));
    } catch (IllegalArgumentException e) {
      decision = Decision.DENY;
    }
    List<String> ids = new ArrayList<>();
    Object rawIds = m.get("policy_ids");
    if (rawIds instanceof List) {
      for (Object o : (List<Object>) rawIds) {
        if (o != null) {
          ids.add(o.toString());
        }
      }
    }
    Object obligations = m.get("obligations");
    Object ttl = m.get("ttl_seconds");
    Object reason = m.get("reason");
    return new PdpDecision(
        decision,
        reason == null ? null : reason.toString(),
        ids,
        obligations instanceof List ? (List<Object>) obligations : null,
        ttl instanceof Number ? ((Number) ttl).longValue() : null);
  }

  String describe() {
    StringBuilder sb = new StringBuilder("Aegis PDP ").append(decision);
    if (!reason.isEmpty()) {
      sb.append(": ").append(reason);
    }
    if (!policyIds.isEmpty()) {
      sb.append(" ").append(policyIds);
    }
    return sb.toString();
  }
}
