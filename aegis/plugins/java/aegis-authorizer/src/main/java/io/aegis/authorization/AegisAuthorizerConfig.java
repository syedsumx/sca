package io.aegis.authorization;

import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.function.Function;

/**
 * Typed view over the {@code configs} block of the plugin entry in
 * {@code /etc/datahub/plugins/auth/config.yml}.
 *
 * <p>Values may be given as YAML scalars or strings (GMS passes the YAML through as a {@code
 * Map<String, Object>}); string values of the form {@code ${ENV_VAR}} are resolved from the
 * process environment so secrets do not have to live in the config file.
 */
final class AegisAuthorizerConfig {

  enum Mode {
    /** Plugin only adds grants; the native DataHubAuthorizer remains in the chain. */
    ADDITIVE,
    /** Plugin is the only authorizer (AUTH_POLICIES_ENABLED=false); PDP is the source of truth. */
    AUTHORITATIVE
  }

  static final String DEFAULT_PDP_URL = "http://aegis-pdp:8810";

  final String pdpUrl;
  final String serviceToken;
  final long connectTimeoutMs;
  final long requestTimeoutMs;
  final long cacheTtlSeconds;
  final int cacheMaxEntries;
  final Mode mode;
  final Set<String> rootUsers;
  final Set<String> failOpenPrivileges;
  final int circuitFailureThreshold;
  final long circuitOpenSeconds;
  final boolean resolveResourceAttributes;

  private AegisAuthorizerConfig(Map<String, Object> raw, Function<String, String> env) {
    Map<String, Object> cfg = raw == null ? Collections.emptyMap() : raw;
    this.pdpUrl = stripTrailingSlash(str(cfg, "pdpUrl", DEFAULT_PDP_URL, env));

    String token = str(cfg, "serviceToken", null, env);
    String tokenEnv = str(cfg, "serviceTokenEnv", "AEGIS_SERVICE_TOKEN", env);
    if ((token == null || token.isEmpty()) && tokenEnv != null) {
      token = env.apply(tokenEnv);
    }
    this.serviceToken = token == null ? "" : token;

    this.connectTimeoutMs = lng(cfg, "connectTimeoutMs", 500, env);
    this.requestTimeoutMs = lng(cfg, "requestTimeoutMs", 1500, env);
    this.cacheTtlSeconds = lng(cfg, "cacheTtlSeconds", 5, env);
    this.cacheMaxEntries = (int) lng(cfg, "cacheMaxEntries", 50_000, env);
    this.mode =
        Mode.valueOf(
            str(cfg, "mode", "additive", env).trim().toUpperCase(Locale.ROOT).replace('-', '_'));
    this.rootUsers = set(cfg.get("rootUsers"), env);
    this.failOpenPrivileges = set(cfg.get("failOpenPrivileges"), env);
    this.resolveResourceAttributes =
        Boolean.parseBoolean(str(cfg, "resolveResourceAttributes", "true", env));

    Object cb = cfg.get("circuitBreaker");
    Map<?, ?> cbMap = cb instanceof Map ? (Map<?, ?>) cb : Collections.emptyMap();
    this.circuitFailureThreshold = (int) toLong(cbMap.get("failureThreshold"), 5, env);
    this.circuitOpenSeconds = toLong(cbMap.get("openSeconds"), 10, env);

    if (connectTimeoutMs <= 0 || requestTimeoutMs <= 0) {
      throw new IllegalArgumentException("Timeouts must be positive");
    }
    if (cacheMaxEntries < 0 || cacheTtlSeconds < 0) {
      throw new IllegalArgumentException("Cache settings must be non-negative");
    }
    if (circuitFailureThreshold <= 0 || circuitOpenSeconds <= 0) {
      throw new IllegalArgumentException("circuitBreaker settings must be positive");
    }
  }

  static AegisAuthorizerConfig from(Map<String, Object> raw) {
    return new AegisAuthorizerConfig(raw, System::getenv);
  }

  static AegisAuthorizerConfig from(Map<String, Object> raw, Function<String, String> env) {
    return new AegisAuthorizerConfig(raw, env);
  }

  // ---------------------------------------------------------------- helpers

  private static String stripTrailingSlash(String s) {
    String out = s;
    while (out.endsWith("/")) {
      out = out.substring(0, out.length() - 1);
    }
    return out;
  }

  private static String resolveEnv(String value, Function<String, String> env) {
    if (value != null && value.startsWith("${") && value.endsWith("}")) {
      String name = value.substring(2, value.length() - 1);
      String fallback = null;
      int idx = name.indexOf(":-");
      if (idx >= 0) {
        fallback = name.substring(idx + 2);
        name = name.substring(0, idx);
      }
      String resolved = env.apply(name);
      return resolved != null && !resolved.isEmpty() ? resolved : fallback;
    }
    return value;
  }

  private static String str(
      Map<String, Object> cfg, String key, String def, Function<String, String> env) {
    Object v = cfg.get(key);
    if (v == null) {
      return def;
    }
    String resolved = resolveEnv(v.toString(), env);
    return resolved == null ? def : resolved;
  }

  private static long lng(
      Map<String, Object> cfg, String key, long def, Function<String, String> env) {
    return toLong(cfg.get(key), def, env);
  }

  private static long toLong(Object v, long def, Function<String, String> env) {
    if (v == null) {
      return def;
    }
    if (v instanceof Number) {
      return ((Number) v).longValue();
    }
    String s = resolveEnv(v.toString().trim(), env);
    return s == null || s.isEmpty() ? def : Long.parseLong(s.trim());
  }

  private static Set<String> set(Object v, Function<String, String> env) {
    Set<String> out = new LinkedHashSet<>();
    if (v == null) {
      return Collections.unmodifiableSet(out);
    }
    if (v instanceof Collection) {
      for (Object o : (Collection<?>) v) {
        if (o != null) {
          addCsv(out, resolveEnv(o.toString(), env));
        }
      }
    } else {
      addCsv(out, resolveEnv(v.toString(), env));
    }
    return Collections.unmodifiableSet(out);
  }

  private static void addCsv(Set<String> out, String s) {
    if (s == null) {
      return;
    }
    for (String part : s.split(",")) {
      String t = part.trim();
      if (!t.isEmpty()) {
        out.add(t);
      }
    }
  }
}
