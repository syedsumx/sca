package io.cairn.authorization;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.datahub.authorization.AuthorizationRequest;
import com.datahub.authorization.AuthorizationResult;
import com.datahub.authorization.EntityFieldType;
import com.datahub.authorization.EntitySpec;
import com.datahub.authorization.EntitySpecResolver;
import com.datahub.authorization.FieldResolver;
import com.datahub.authorization.ResolvedEntitySpec;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

class CairnAuthorizerTest {

  private static final String DATASET =
      "urn:li:dataset:(urn:li:dataPlatform:snowflake,finance.revenue,PROD)";
  private static final String ALICE = "urn:li:corpuser:alice";

  private HttpServer server;
  private final List<Map<String, Object>> received = new CopyOnWriteArrayList<>();
  private final List<String> authHeaders = new CopyOnWriteArrayList<>();
  private final AtomicReference<String> responseBody =
      new AtomicReference<>("{\"decision\":\"ALLOW\",\"reason\":\"ok\",\"policy_ids\":[\"p1\"]}");
  private final AtomicInteger responseStatus = new AtomicInteger(200);
  private final AtomicLong now = new AtomicLong(1_000_000_000L);
  private String pdpUrl;

  @BeforeEach
  @SuppressWarnings("unchecked")
  void startFakePdp() throws IOException {
    server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
    server.createContext(
        "/governance/v1/authorize",
        exchange -> {
          byte[] body = exchange.getRequestBody().readAllBytes();
          received.add((Map<String, Object>) MiniJson.parse(new String(body, StandardCharsets.UTF_8)));
          String auth = exchange.getRequestHeaders().getFirst("Authorization");
          authHeaders.add(auth == null ? "" : auth);
          byte[] out = responseBody.get().getBytes(StandardCharsets.UTF_8);
          exchange.getResponseHeaders().add("Content-Type", "application/json");
          exchange.sendResponseHeaders(responseStatus.get(), out.length);
          try (OutputStream os = exchange.getResponseBody()) {
            os.write(out);
          }
        });
    server.start();
    pdpUrl = "http://127.0.0.1:" + server.getAddress().getPort();
  }

  @AfterEach
  void stop() {
    server.stop(0);
  }

  private CairnAuthorizer authorizer(Map<String, Object> extra) {
    Map<String, Object> cfg = new HashMap<>();
    cfg.put("pdpUrl", pdpUrl + "/");
    cfg.put("serviceToken", "s3cret");
    cfg.put("cacheTtlSeconds", 5);
    cfg.put("rootUsers", List.of("urn:li:corpuser:admin"));
    cfg.put("failOpenPrivileges", "VIEW_ENTITY_PAGE");
    Map<String, Object> cb = new HashMap<>();
    cb.put("failureThreshold", 2);
    cb.put("openSeconds", 10);
    cfg.put("circuitBreaker", cb);
    cfg.putAll(extra);
    CairnAuthorizer a = new CairnAuthorizer(now::get);
    a.init(CairnAuthorizerConfig.from(cfg, k -> null), resolver());
    return a;
  }

  private static EntitySpecResolver resolver() {
    return spec -> {
      Map<EntityFieldType, FieldResolver> fields = new HashMap<>();
      if ("corpuser".equals(spec.getType())) {
        fields.put(
            EntityFieldType.GROUP_MEMBERSHIP,
            FieldResolver.getResolverFromValues(Set.of("urn:li:corpGroup:finance")));
      } else {
        fields.put(
            EntityFieldType.OWNER, FieldResolver.getResolverFromValues(Set.of(ALICE)));
        fields.put(
            EntityFieldType.TAG,
            FieldResolver.getResolverFromValues(Set.of("urn:li:tag:PII", "urn:li:tag:Gold")));
        fields.put(
            EntityFieldType.DOMAIN,
            FieldResolver.getResolverFromValues(Set.of("urn:li:domain:finance")));
      }
      return new ResolvedEntitySpec(spec, fields);
    };
  }

  private static AuthorizationRequest req(String actor, String privilege) {
    return new AuthorizationRequest(
        actor,
        privilege,
        Optional.of(new EntitySpec("dataset", DATASET)),
        Collections.emptyList());
  }

  @Test
  void allowFromPdpIsAllow() {
    CairnAuthorizer a = authorizer(Map.of());
    AuthorizationResult r = a.authorize(req(ALICE, "EDIT_ENTITY_TAGS"));
    assertEquals(AuthorizationResult.Type.ALLOW, r.getType());
    assertTrue(r.getMessage().contains("p1"));
    assertEquals("Bearer s3cret", authHeaders.get(0));
  }

  @Test
  @SuppressWarnings("unchecked")
  void payloadCarriesResolvedAttributesAndGroups() {
    CairnAuthorizer a = authorizer(Map.of());
    a.authorize(req(ALICE, "EDIT_ENTITY_TAGS"));
    Map<String, Object> p = received.get(0);
    assertEquals(ALICE, p.get("actor"));
    assertEquals("EDIT_ENTITY_TAGS", p.get("privilege"));
    Map<String, Object> resource = (Map<String, Object>) p.get("resource");
    assertEquals("dataset", resource.get("type"));
    assertEquals(DATASET, resource.get("urn"));
    Map<String, Object> attrs = (Map<String, Object>) p.get("resource_attributes");
    assertEquals(List.of("urn:li:tag:Gold", "urn:li:tag:PII"), attrs.get("tags"));
    assertEquals(List.of(ALICE), attrs.get("owners"));
    assertEquals(List.of("urn:li:domain:finance"), attrs.get("domains"));
    assertEquals(List.of(), attrs.get("glossary_terms"));
    assertEquals(List.of("urn:li:corpGroup:finance"), p.get("actor_groups"));
    assertEquals("core", ((Map<String, Object>) p.get("environment")).get("channel"));
  }

  @Test
  void denyAndNotApplicableAreDeny() {
    CairnAuthorizer a = authorizer(Map.of("cacheTtlSeconds", 0));
    responseBody.set("{\"decision\":\"DENY\",\"reason\":\"pii clearance\"}");
    assertEquals(AuthorizationResult.Type.DENY, a.authorize(req(ALICE, "VIEW_DATASET_PROFILE")).getType());
    responseBody.set("{\"decision\":\"NOT_APPLICABLE\"}");
    assertEquals(AuthorizationResult.Type.DENY, a.authorize(req(ALICE, "VIEW_DATASET_PROFILE")).getType());
    responseBody.set("{\"decision\":\"MAYBE\"}");
    assertEquals(AuthorizationResult.Type.DENY, a.authorize(req(ALICE, "VIEW_DATASET_PROFILE")).getType());
  }

  @Test
  void systemActorAndRootUsersBypassPdp() {
    CairnAuthorizer a = authorizer(Map.of());
    assertEquals(
        AuthorizationResult.Type.ALLOW,
        a.authorize(req(CairnAuthorizer.SYSTEM_ACTOR, "MANAGE_POLICIES")).getType());
    assertEquals(
        AuthorizationResult.Type.ALLOW,
        a.authorize(req("urn:li:corpuser:admin", "MANAGE_POLICIES")).getType());
    assertTrue(received.isEmpty());
  }

  @Test
  void pdpAttributeReaderReadsWithoutCallingPdp() {
    CairnAuthorizer a = authorizer(Map.of());
    String reader = CairnAuthorizerConfig.DEFAULT_TRUSTED_READER;
    responseBody.set("{\"decision\":\"DENY\"}");
    assertEquals(AuthorizationResult.Type.ALLOW, a.authorize(req(reader, "VIEW_ENTITY_PAGE")).getType());
    assertTrue(received.isEmpty());
    // Anything but a read still goes to the PDP.
    assertEquals(AuthorizationResult.Type.DENY, a.authorize(req(reader, "EDIT_ENTITY_TAGS")).getType());
    assertEquals(1, received.size());
    // Other actors' reads are decided by the PDP.
    assertEquals(AuthorizationResult.Type.DENY, a.authorize(req(ALICE, "VIEW_ENTITY_PAGE")).getType());
    assertEquals(2, received.size());
    // An empty list turns the shortcut off.
    CairnAuthorizer none = authorizer(Map.of("trustedReaders", ""));
    assertEquals(AuthorizationResult.Type.DENY, none.authorize(req(reader, "VIEW_ENTITY_PAGE")).getType());
  }

  @Test
  void decisionsAreCachedUntilTtlExpires() {
    CairnAuthorizer a = authorizer(Map.of());
    a.authorize(req(ALICE, "EDIT_ENTITY_TAGS"));
    a.authorize(req(ALICE, "EDIT_ENTITY_TAGS"));
    assertEquals(1, received.size());
    // Different privilege → different key.
    a.authorize(req(ALICE, "EDIT_ENTITY_OWNERS"));
    assertEquals(2, received.size());
    now.addAndGet(6_000_000_000L);
    a.authorize(req(ALICE, "EDIT_ENTITY_TAGS"));
    assertEquals(3, received.size());
  }

  @Test
  void pdpTtlZeroDisablesCachingForThatDecision() {
    CairnAuthorizer a = authorizer(Map.of());
    responseBody.set("{\"decision\":\"ALLOW\",\"ttl_seconds\":0}");
    a.authorize(req(ALICE, "EDIT_ENTITY_TAGS"));
    a.authorize(req(ALICE, "EDIT_ENTITY_TAGS"));
    assertEquals(2, received.size());
  }

  @Test
  void subResourcesChangeCacheKey() {
    EntitySpec f1 = new EntitySpec("schemaField", "urn:li:schemaField:(" + DATASET + ",a)");
    EntitySpec f2 = new EntitySpec("schemaField", "urn:li:schemaField:(" + DATASET + ",b)");
    AuthorizationRequest r1 =
        new AuthorizationRequest(ALICE, "EDIT_ENTITY_TAGS", Optional.of(new EntitySpec("dataset", DATASET)), List.of(f1));
    AuthorizationRequest r2 =
        new AuthorizationRequest(ALICE, "EDIT_ENTITY_TAGS", Optional.of(new EntitySpec("dataset", DATASET)), List.of(f2));
    AuthorizationRequest r1b =
        new AuthorizationRequest(ALICE, "EDIT_ENTITY_TAGS", Optional.of(new EntitySpec("dataset", DATASET)), new ArrayList<>(List.of(f1)));
    assertTrue(!CairnAuthorizer.cacheKey(r1).equals(CairnAuthorizer.cacheKey(r2)));
    assertEquals(CairnAuthorizer.cacheKey(r1), CairnAuthorizer.cacheKey(r1b));
  }

  @Test
  void failsClosedOnPdpErrorExceptFailOpenPrivileges() {
    CairnAuthorizer a = authorizer(Map.of());
    responseStatus.set(500);
    AuthorizationResult edit = a.authorize(req(ALICE, "EDIT_ENTITY_TAGS"));
    assertEquals(AuthorizationResult.Type.DENY, edit.getType());
    assertTrue(edit.getMessage().contains("fail-closed"));
    AuthorizationResult view = a.authorize(req(ALICE, "VIEW_ENTITY_PAGE"));
    assertEquals(AuthorizationResult.Type.ALLOW, view.getType());
    assertTrue(view.getMessage().contains("fail-open"));
  }

  @Test
  void malformedResponseFailsClosed() {
    CairnAuthorizer a = authorizer(Map.of());
    responseBody.set("not json");
    assertEquals(AuthorizationResult.Type.DENY, a.authorize(req(ALICE, "EDIT_ENTITY_TAGS")).getType());
  }

  @Test
  void circuitOpensAfterThresholdAndRecovers() {
    CairnAuthorizer a = authorizer(Map.of("cacheTtlSeconds", 0));
    responseStatus.set(503);
    a.authorize(req(ALICE, "EDIT_ENTITY_TAGS"));
    a.authorize(req(ALICE, "EDIT_ENTITY_TAGS"));
    assertEquals(CircuitBreaker.State.OPEN, a.circuitState());
    int callsWhenOpened = received.size();
    AuthorizationResult r = a.authorize(req(ALICE, "EDIT_ENTITY_TAGS"));
    assertEquals(AuthorizationResult.Type.DENY, r.getType());
    assertTrue(r.getMessage().contains("circuit open"));
    assertEquals(callsWhenOpened, received.size(), "no PDP call while circuit is open");

    now.addAndGet(11_000_000_000L);
    responseStatus.set(200);
    assertEquals(AuthorizationResult.Type.ALLOW, a.authorize(req(ALICE, "EDIT_ENTITY_TAGS")).getType());
    assertEquals(CircuitBreaker.State.CLOSED, a.circuitState());
  }

  @Test
  void unreachablePdpFailsClosed() {
    Map<String, Object> cfg = new HashMap<>();
    cfg.put("pdpUrl", "http://127.0.0.1:1");
    cfg.put("connectTimeoutMs", 200);
    cfg.put("requestTimeoutMs", 300);
    CairnAuthorizer a = new CairnAuthorizer(now::get);
    a.init(CairnAuthorizerConfig.from(cfg, k -> null), resolver());
    assertEquals(AuthorizationResult.Type.DENY, a.authorize(req(ALICE, "EDIT_ENTITY_TAGS")).getType());
  }

  @Test
  void platformPrivilegeHasNullResource() {
    CairnAuthorizer a = authorizer(Map.of());
    a.authorize(new AuthorizationRequest(ALICE, "MANAGE_POLICIES", Optional.empty(), List.of()));
    assertTrue(received.get(0).containsKey("resource"));
    assertEquals(null, received.get(0).get("resource"));
  }

  @Test
  void configResolvesEnvPlaceholders() {
    Map<String, String> env = Map.of("CAIRN_SERVICE_TOKEN", "from-env", "PDP", "http://pdp:9");
    CairnAuthorizerConfig c =
        CairnAuthorizerConfig.from(
            Map.of("pdpUrl", "${PDP}", "mode", "authoritative", "rootUsers", "a, b"), env::get);
    assertEquals("http://pdp:9", c.pdpUrl);
    assertEquals("from-env", c.serviceToken);
    assertEquals(CairnAuthorizerConfig.Mode.AUTHORITATIVE, c.mode);
    assertEquals(Set.of("a", "b"), c.rootUsers);
    CairnAuthorizerConfig d =
        CairnAuthorizerConfig.from(Map.of("pdpUrl", "${MISSING:-http://fallback:1}"), env::get);
    assertEquals("http://fallback:1", d.pdpUrl);
  }
}
