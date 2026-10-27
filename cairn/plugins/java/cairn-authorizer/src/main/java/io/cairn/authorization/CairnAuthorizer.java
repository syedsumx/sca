package io.cairn.authorization;

import com.datahub.authorization.AuthorizationRequest;
import com.datahub.authorization.AuthorizationResult;
import com.datahub.authorization.AuthorizerContext;
import com.datahub.authorization.EntityFieldType;
import com.datahub.authorization.EntitySpec;
import com.datahub.authorization.EntitySpecResolver;
import com.datahub.authorization.ResolvedEntitySpec;
import com.datahub.plugins.auth.authorization.Authorizer;
import java.io.IOException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeSet;
import java.util.function.LongSupplier;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * Cairn Core {@link Authorizer} plugin that delegates decisions to the Cairn Policy Decision Point
 * (ABAC + RBAC, deny-overrides).
 *
 * <p>Chain semantics recap (see {@code AuthorizerChain}): custom authorizers run first and the
 * first ALLOW wins. Returning DENY therefore means "no grant from me" and lets the chain continue
 * to the native policy authorizer in {@code additive} mode. In {@code authoritative} mode Cairn Core is
 * started with {@code CAIRN_NATIVE_POLICIES_ENABLED=false}, so this plugin is the only authorizer and its
 * DENY is final.
 *
 * <p>Failure handling is fail-closed: if the PDP is unreachable, slow, or the circuit breaker is
 * open, the request is denied unless the privilege is listed in {@code failOpenPrivileges}.
 *
 * <p>Reads by a {@code trustedReaders} actor are allowed without asking the PDP: the PDP resolves
 * attributes by reading Cairn Core as that actor, so routing those reads back to the PDP would make
 * every decision wait on itself (and time out, fail-closed, in authoritative mode).
 */
public class CairnAuthorizer implements Authorizer {

  private static final Logger LOG = Logger.getLogger(CairnAuthorizer.class.getName());

  /** The core's built-in system actor. */
  static final String SYSTEM_ACTOR = "urn:li:corpuser:__datahub_system";

  private static final String CORP_USER = "corpuser";

  private volatile CairnAuthorizerConfig config;
  private volatile EntitySpecResolver resolver;
  private volatile PdpClient pdp;
  private volatile DecisionCache cache;
  private volatile CircuitBreaker breaker;
  private final LongSupplier clock;

  public CairnAuthorizer() {
    this(System::nanoTime);
  }

  CairnAuthorizer(LongSupplier nanoClock) {
    this.clock = nanoClock;
  }

  @Override
  public void init(Map<String, Object> authorizerConfig, AuthorizerContext ctx) {
    init(CairnAuthorizerConfig.from(authorizerConfig), ctx == null ? null : ctx.getEntitySpecResolver());
  }

  void init(CairnAuthorizerConfig cfg, EntitySpecResolver entitySpecResolver) {
    init(cfg, entitySpecResolver, new PdpClient(cfg.pdpUrl, cfg.serviceToken, cfg.connectTimeoutMs, cfg.requestTimeoutMs));
  }

  void init(CairnAuthorizerConfig cfg, EntitySpecResolver entitySpecResolver, PdpClient client) {
    this.config = cfg;
    this.resolver = entitySpecResolver;
    this.pdp = client;
    this.cache = new DecisionCache(cfg.cacheTtlSeconds, cfg.cacheMaxEntries, clock);
    this.breaker = new CircuitBreaker(cfg.circuitFailureThreshold, cfg.circuitOpenSeconds, clock);
    if (cfg.serviceToken.isEmpty()) {
      LOG.warning("CairnAuthorizer: no service token configured; PDP calls are unauthenticated");
    }
    LOG.info(
        String.format(
            "CairnAuthorizer initialised: pdp=%s mode=%s cacheTtl=%ss rootUsers=%d failOpen=%s",
            cfg.pdpUrl, cfg.mode, cfg.cacheTtlSeconds, cfg.rootUsers.size(), cfg.failOpenPrivileges));
  }

  @Override
  public AuthorizationResult authorize(AuthorizationRequest request) {
    CairnAuthorizerConfig cfg = this.config;
    if (cfg == null) {
      return deny(request, "CairnAuthorizer not initialised");
    }
    String actor = request.getActorUrn();
    if (SYSTEM_ACTOR.equals(actor)) {
      return allow(request, "Cairn: system actor");
    }
    if (actor != null && cfg.rootUsers.contains(actor)) {
      return allow(request, "Cairn: break-glass root user");
    }
    if (actor != null
        && cfg.trustedReaders.contains(actor)
        && request.getPrivilege() != null
        && cfg.readPrivileges.contains(request.getPrivilege())) {
      return allow(request, "Cairn: PDP attribute reader");
    }

    String key = cacheKey(request);
    PdpDecision cached = cache.get(key);
    if (cached != null) {
      return toResult(request, cached);
    }

    if (!breaker.allowRequest()) {
      return failure(request, cfg, "PDP circuit open");
    }

    PdpDecision decision;
    try {
      decision = pdp.authorize(buildPayload(request, cfg));
      breaker.recordSuccess();
    } catch (IOException | RuntimeException e) {
      breaker.recordFailure();
      LOG.log(Level.WARNING, "Cairn PDP call failed for privilege " + request.getPrivilege(), e);
      return failure(request, cfg, "PDP unavailable: " + e.getMessage());
    }
    cache.put(key, decision);
    return toResult(request, decision);
  }

  /** Invalidates every cached decision (exposed for tests and future admin hooks). */
  public void invalidateCache() {
    DecisionCache c = this.cache;
    if (c != null) {
      c.invalidateAll();
    }
  }

  // ---------------------------------------------------------------- request building

  Map<String, Object> buildPayload(AuthorizationRequest request, CairnAuthorizerConfig cfg) {
    Map<String, Object> payload = new LinkedHashMap<>();
    payload.put("actor", request.getActorUrn());
    payload.put("privilege", request.getPrivilege());

    Optional<EntitySpec> resourceSpec =
        request.getResourceSpec() == null ? Optional.empty() : request.getResourceSpec();
    if (resourceSpec.isPresent()) {
      payload.put("resource", spec(resourceSpec.get()));
    } else {
      payload.put("resource", null);
    }

    List<Object> subs = new ArrayList<>();
    if (request.getSubResources() != null) {
      for (EntitySpec s : request.getSubResources()) {
        subs.add(spec(s));
      }
    }
    payload.put("sub_resources", subs);

    payload.put(
        "resource_attributes",
        resourceSpec.isPresent() && cfg.resolveResourceAttributes
            ? resolveAttributes(resourceSpec.get())
            : emptyAttributes());
    payload.put("actor_groups", actorGroups(request));

    Map<String, Object> env = new LinkedHashMap<>();
    env.put("channel", "core");
    env.put("mode", cfg.mode.name().toLowerCase());
    payload.put("environment", env);
    return payload;
  }

  private static Map<String, Object> spec(EntitySpec s) {
    Map<String, Object> m = new LinkedHashMap<>();
    m.put("type", s.getType());
    m.put("urn", s.getEntity());
    return m;
  }

  private static Map<String, Object> emptyAttributes() {
    Map<String, Object> m = new LinkedHashMap<>();
    for (String k :
        new String[] {
          "owners", "domains", "tags", "glossary_terms", "containers", "platform_instances",
          "structured_properties"
        }) {
      m.put(k, Collections.emptyList());
    }
    return m;
  }

  /**
   * Resolves the attributes Cairn Core already knows how to compute cheaply. Each field is resolved
   * independently so one failing resolver (e.g. a missing aspect) does not hide the others.
   */
  private Map<String, Object> resolveAttributes(EntitySpec resource) {
    Map<String, Object> attrs = emptyAttributes();
    EntitySpecResolver r = this.resolver;
    if (r == null || resource.getEntity() == null || resource.getEntity().isEmpty()) {
      return attrs;
    }
    ResolvedEntitySpec resolved;
    try {
      resolved = r.resolve(resource);
    } catch (RuntimeException e) {
      LOG.log(Level.FINE, "Could not resolve resource " + resource.getEntity(), e);
      return attrs;
    }
    if (resolved == null) {
      return attrs;
    }
    attrs.put("owners", sorted(safeField(resolved, EntityFieldType.OWNER)));
    attrs.put("domains", sorted(safeField(resolved, EntityFieldType.DOMAIN)));
    attrs.put("tags", sorted(safeField(resolved, EntityFieldType.TAG)));
    attrs.put("glossary_terms", sorted(safeField(resolved, EntityFieldType.GLOSSARY)));
    attrs.put("containers", sorted(safeField(resolved, EntityFieldType.CONTAINER)));
    attrs.put(
        "platform_instances", sorted(safeField(resolved, EntityFieldType.DATA_PLATFORM_INSTANCE)));
    List<String> props = new ArrayList<>();
    try {
      Map<String, Set<String>> sp = structuredPropertyValues(resolved);
      if (sp != null) {
        for (Map.Entry<String, Set<String>> e : new java.util.TreeMap<>(sp).entrySet()) {
          for (String v : new TreeSet<>(e.getValue())) {
            props.add(e.getKey() + "=" + v);
          }
        }
      }
    } catch (RuntimeException e) {
      LOG.log(Level.FINE, "Structured property resolution failed", e);
    }
    attrs.put("structured_properties", props);
    return attrs;
  }

  private static Set<String> safeField(ResolvedEntitySpec resolved, EntityFieldType type) {
    try {
      Set<String> v = resolved.getFieldValues(type);
      return v == null ? Collections.emptySet() : v;
    } catch (RuntimeException e) {
      LOG.log(Level.FINE, "Field resolution failed for " + type, e);
      return Collections.emptySet();
    }
  }

  // ---------------------------------------------------------------- cross-version SPI access
  //
  // ResolvedEntitySpec#getStructuredPropertyValues and AuthorizationRequest#getActorGroupMembership
  // were added in Cairn Core 1.6/1.7. They are looked up reflectively so one plugin jar (compiled
  // against the Java 17 auth-api of 1.5.x) runs unchanged on Cairn Core 1.5 through 1.8+.

  private static final Method STRUCTURED_PROPS =
      findMethod(ResolvedEntitySpec.class, "getStructuredPropertyValues");
  private static final Method SESSION_GROUPS =
      findMethod(AuthorizationRequest.class, "getActorGroupMembership");

  private static Method findMethod(Class<?> type, String name) {
    try {
      return type.getMethod(name);
    } catch (NoSuchMethodException e) {
      return null;
    }
  }

  @SuppressWarnings("unchecked")
  private static Map<String, Set<String>> structuredPropertyValues(ResolvedEntitySpec resolved) {
    if (STRUCTURED_PROPS == null) {
      return null;
    }
    try {
      return (Map<String, Set<String>>) STRUCTURED_PROPS.invoke(resolved);
    } catch (ReflectiveOperationException e) {
      throw new IllegalStateException(e);
    }
  }

  private static Collection<?> sessionGroupMembership(AuthorizationRequest request) {
    if (SESSION_GROUPS == null) {
      return null;
    }
    try {
      return (Collection<?>) SESSION_GROUPS.invoke(request);
    } catch (ReflectiveOperationException e) {
      return null;
    }
  }

  private static List<String> sorted(Collection<String> values) {
    return new ArrayList<>(new TreeSet<>(values));
  }

  /**
   * Group membership: prefer the session-scoped membership Cairn Core attaches to the request; otherwise
   * resolve the actor through the entity-spec resolver.
   */
  private List<String> actorGroups(AuthorizationRequest request) {
    Set<String> groups = new TreeSet<>();
    Collection<?> sessionGroups = sessionGroupMembership(request);
    if (sessionGroups != null) {
      for (Object u : sessionGroups) {
        groups.add(u.toString());
      }
      return new ArrayList<>(groups);
    }
    EntitySpecResolver r = this.resolver;
    if (r != null && request.getActorUrn() != null) {
      try {
        ResolvedEntitySpec actor = r.resolve(new EntitySpec(CORP_USER, request.getActorUrn()));
        if (actor != null) {
          groups.addAll(actor.getGroupMembership());
        }
      } catch (RuntimeException e) {
        LOG.log(Level.FINE, "Could not resolve groups for " + request.getActorUrn(), e);
      }
    }
    return new ArrayList<>(groups);
  }

  // ---------------------------------------------------------------- results

  static String cacheKey(AuthorizationRequest request) {
    StringBuilder sb = new StringBuilder();
    sb.append(request.getActorUrn()).append('\u0001').append(request.getPrivilege()).append('\u0001');
    Optional<EntitySpec> rs = request.getResourceSpec();
    if (rs != null && rs.isPresent()) {
      sb.append(rs.get().getType()).append(':').append(rs.get().getEntity());
    }
    sb.append('\u0001');
    Collection<EntitySpec> subs = request.getSubResources();
    if (subs != null && !subs.isEmpty()) {
      TreeSet<String> parts = new TreeSet<>();
      for (EntitySpec s : subs) {
        parts.add(s.getType() + ":" + s.getEntity());
      }
      sb.append(sha256(String.join("\u0002", parts)));
    }
    return sb.toString();
  }

  private static String sha256(String s) {
    try {
      byte[] d = MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8));
      StringBuilder hex = new StringBuilder(d.length * 2);
      for (byte b : d) {
        hex.append(String.format("%02x", b));
      }
      return hex.toString();
    } catch (NoSuchAlgorithmException e) {
      throw new IllegalStateException(e);
    }
  }

  private static AuthorizationResult toResult(AuthorizationRequest request, PdpDecision d) {
    if (d.decision == PdpDecision.Decision.ALLOW) {
      return allow(request, d.describe());
    }
    // DENY and NOT_APPLICABLE both map to DENY: in additive mode the chain continues to the native
    // authorizer; in authoritative mode there is nothing after us.
    return deny(request, d.describe());
  }

  private static AuthorizationResult failure(
      AuthorizationRequest request, CairnAuthorizerConfig cfg, String why) {
    if (request.getPrivilege() != null && cfg.failOpenPrivileges.contains(request.getPrivilege())) {
      return allow(request, "Cairn fail-open (" + why + ")");
    }
    return deny(request, "Cairn fail-closed (" + why + ")");
  }

  private static AuthorizationResult allow(AuthorizationRequest request, String msg) {
    return new AuthorizationResult(request, AuthorizationResult.Type.ALLOW, msg);
  }

  private static AuthorizationResult deny(AuthorizationRequest request, String msg) {
    return new AuthorizationResult(request, AuthorizationResult.Type.DENY, msg);
  }

  // Visible for tests.
  CircuitBreaker.State circuitState() {
    return breaker.state();
  }

  int cacheSize() {
    return cache.size();
  }
}
