package io.cairn.client;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Checks of the Java client. The JSON codec always; the live part when CAIRN_TEST_URL is set (as
 * tests/test_sdk_clients.py does): CAIRN_TEST_TOKEN is ana's personal access token, CAIRN_TEST_AGENT_KEY an agent key acting for her, CAIRN_TEST_PERSON_HEADER a
 * header that makes the caller ana herself (dev mode only). Exits non-zero on the first failure.
 */
public final class ClientCheck {
    private static int checks;

    private static void check(boolean ok, String what) {
        checks++;
        if (!ok) {
            throw new AssertionError(what);
        }
    }

    private static CairnException fails(Runnable r) {
        try {
            r.run();
        } catch (CairnException e) {
            return e;
        }
        throw new AssertionError("expected a CairnException");
    }

    @SuppressWarnings("unchecked")
    public static void main(String[] args) {
        // The JSON codec.
        Object v = Json.parse("{\"a\":[1,-2.5,true,null,\"x\\n\\u00e9\\\"\"],\"b\":{},\"big\":123456789012345678901234567890}");
        Map<String, Object> m = (Map<String, Object>) v;
        List<Object> a = (List<Object>) m.get("a");
        check(a.size() == 5 && a.get(0).equals(1L) && a.get(1).equals(-2.5) && a.get(3) == null, "values: " + a);
        check(a.get(4).equals("x\n\u00e9\""), "escapes");
        check(m.get("big").toString().equals("123456789012345678901234567890"), "big integers");
        check(Json.parse(Json.write(m)).equals(m), "round trip");
        check(Json.write(Map.of("k", "\u0001")).equals("{\"k\":\"\\u0001\"}"), "control characters escaped");
        for (String bad : new String[] {"", "{", "[1,]", "{\"a\" 1}", "tru", "\"\\x\"", "1 2", "[" .repeat(600)}) {
            try {
                Json.parse(bad);
                throw new AssertionError("accepted " + bad);
            } catch (IllegalArgumentException expected) {
                checks++;
            }
        }
        try {
            CairnClient.builder("http://cairn.example.com").build();
            throw new AssertionError("plain http accepted");
        } catch (IllegalArgumentException expected) {
            checks++;
        }

        String url = System.getenv("CAIRN_TEST_URL");
        if (url == null || url.isEmpty()) {
            System.out.println("ok " + checks + " checks (CAIRN_TEST_URL not set: live checks skipped)");
            return;
        }
        String ds = System.getenv("CAIRN_TEST_DATASET");
        List<CairnClient.Deprecation> seen = new ArrayList<>();
        CairnClient c = CairnClient.builder(url).token(System.getenv("CAIRN_TEST_TOKEN")).onDeprecation(seen::add).build();
        check("urn:li:corpuser:ana".equals(c.me().get("urn")), "me");
        check(c.datasetHealth(ds) != null, "health");
        Map<String, Object> out = c.runSql("wh", "SELECT count(*) AS n FROM main.orders", 10);
        check(out.get("rows").equals(List.of(List.of(3L))), "sql rows: " + out.get("rows"));
        Map<String, Object> masked = c.runSql("wh", "SELECT email FROM main.users ORDER BY id", 10);
        for (Object row : (List<Object>) masked.get("rows")) {
            String email = String.valueOf(((List<Object>) row).get(0));
            check(email.contains("\u2022") && !email.contains("kim") && !email.contains("lee"), "email masked: " + row);
        }
        check(fails(() -> c.runSql("wh", "SELECT * FROM main.secrets", 10)).status() == 403, "a denied table");
        CairnClient agent = CairnClient.builder(url).token(System.getenv("CAIRN_TEST_AGENT_KEY")).build();
        check("urn:li:corpuser:ana".equals(agent.me().get("urn")), "agent acts for ana");
        check(fails(agent::customAgents).status() == 403, "agents do not start agents");
        check(fails(() -> CairnClient.builder(url).token("not-a-token").build().me()).status() == 401, "bad token");

        c.savedQueries();
        c.savedQueries();
        check(seen.size() == 1 && seen.get(0).path().equals("/sql/saved") && "2099-01-01".equals(seen.get(0).sunset())
                && "/v2/sql/saved".equals(seen.get(0).successor()), "deprecation reported once: " + seen);
        CairnException gone = fails(() -> c.request("GET", "/v1/datasets/changes", null, null));
        check(gone.status() == 410 && gone.detail().contains("retired"), "retired route: " + gone.detail());
        check(((Number) c.deprecations().get("min_notice_days")).intValue() == 180, "deprecations listed");

        String[] header = System.getenv("CAIRN_TEST_PERSON_HEADER").split(":", 2);
        CairnClient person = CairnClient.builder(url).header(header[0], header[1]).build();
        List<Object> agents = person.customAgents();
        check(agents.size() == 1 && "steward".equals(((Map<String, Object>) agents.get(0)).get("name")), "agents: " + agents);
        Map<String, Object> run = person.runAgent("steward", "how are orders?");
        check("ok".equals(run.get("status")) && "fine".equals(run.get("answer")), "agent run: " + run);
        System.out.println("ok " + checks + " checks (live included)");
    }
}
