package io.cairn.client;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpHeaders;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Consumer;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Client for the main Cairn APIs (Java 17+, no dependencies). Every call is made as the holder of the token,
 * so it sees and does only what they may.
 *
 * <pre>{@code
 * CairnClient cairn = CairnClient.builder("https://cairn.example.com/cairn/api").token(token).build();
 * Map<String, Object> me = cairn.me();
 * }</pre>
 *
 * <p>Answers are JSON as {@code Map<String, Object>} / {@code List<Object>} (see {@link Json}). Errors raise
 * {@link CairnException}. A call to a deprecated route is reported once to the deprecation listener.
 */
public final class CairnClient {
    /** A deprecated route this client called. */
    public record Deprecation(String method, String path, String deprecation, String sunset, String successor) {
    }

    private static final Set<Integer> RETRY_STATUSES = Set.of(429, 503);
    private static final long MAX_WAIT_MS = 30_000;
    private static final Pattern SUCCESSOR = Pattern.compile("<([^>]+)>\\s*;\\s*rel=\"?successor-version\"?");
    private static final System.Logger LOG = System.getLogger(CairnClient.class.getName());

    private final String baseUrl;
    private final String token;
    private final HttpClient http;
    private final int retries;
    private final Duration timeout;
    private final Map<String, String> headers;
    private final Consumer<Deprecation> onDeprecation;
    private final Set<String> warned = ConcurrentHashMap.newKeySet();

    private CairnClient(Builder b) {
        URI uri = URI.create(b.baseUrl);
        String host = uri.getHost() == null ? "" : uri.getHost();
        if (!"https".equals(uri.getScheme()) && !Set.of("localhost", "127.0.0.1", "[::1]", "::1").contains(host)) {
            throw new IllegalArgumentException("baseUrl must use https (plain http only to localhost)");
        }
        this.baseUrl = b.baseUrl.replaceAll("/+$", "");
        this.token = b.token;
        // Over plain HTTP (localhost only) the JDK would try an h2c upgrade, which many servers refuse: use HTTP/1.1.
        this.http = b.http != null ? b.http : HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10))
                .version("https".equals(uri.getScheme()) ? HttpClient.Version.HTTP_2 : HttpClient.Version.HTTP_1_1)
                .build();
        this.retries = b.retries;
        this.timeout = b.timeout;
        this.headers = Map.copyOf(b.headers);
        this.onDeprecation = b.onDeprecation != null ? b.onDeprecation : d -> LOG.log(System.Logger.Level.WARNING,
                "Cairn: {0} {1} is deprecated{2}{3}", d.method(), d.path(),
                d.sunset() != null ? " and stops answering on " + d.sunset() : "",
                d.successor() != null ? "; use " + d.successor() : "");
    }

    /** @param baseUrl Cairn's API, e.g. {@code https://cairn.example.com/cairn/api} */
    public static Builder builder(String baseUrl) {
        return new Builder(baseUrl);
    }

    public static final class Builder {
        private final String baseUrl;
        private String token;
        private HttpClient http;
        private int retries = 2;
        private Duration timeout = Duration.ofSeconds(60);
        private final Map<String, String> headers = new LinkedHashMap<>();
        private Consumer<Deprecation> onDeprecation;

        private Builder(String baseUrl) {
            this.baseUrl = Objects.requireNonNull(baseUrl, "baseUrl");
        }

        /** A Cairn access token or agent key, sent as a bearer token. */
        public Builder token(String token) {
            this.token = token;
            return this;
        }

        public Builder httpClient(HttpClient http) {
            this.http = http;
            return this;
        }

        /** How many times a 429/503 that carries Retry-After is retried (default 2). */
        public Builder retries(int retries) {
            this.retries = Math.max(0, retries);
            return this;
        }

        public Builder timeout(Duration timeout) {
            this.timeout = Objects.requireNonNull(timeout);
            return this;
        }

        public Builder header(String name, String value) {
            this.headers.put(name, value);
            return this;
        }

        /** Told once per route when Cairn says a route is deprecated (default: a warning in the log). */
        public Builder onDeprecation(Consumer<Deprecation> listener) {
            this.onDeprecation = listener;
            return this;
        }

        public CairnClient build() {
            return new CairnClient(this);
        }
    }

    // ---- any call ------------------------------------------------------------------------------------

    /** Any Cairn API call: the parsed JSON answer, or null when there is none. */
    public Object request(String method, String path, Map<String, ?> query, Object body) {
        StringBuilder url = new StringBuilder(baseUrl).append(path.startsWith("/") ? path : "/" + path);
        if (query != null) {
            String sep = "?";
            for (Map.Entry<String, ?> e : query.entrySet()) {
                if (e.getValue() == null) {
                    continue;
                }
                url.append(sep).append(enc(e.getKey())).append('=').append(enc(String.valueOf(e.getValue())));
                sep = "&";
            }
        }
        HttpRequest.Builder rb = HttpRequest.newBuilder(URI.create(url.toString())).timeout(timeout)
                .header("Accept", "application/json");
        headers.forEach(rb::header);
        if (token != null) {
            rb.header("Authorization", "Bearer " + token);
        }
        if (body != null) {
            rb.header("Content-Type", "application/json")
                    .method(method, HttpRequest.BodyPublishers.ofString(Json.write(body), StandardCharsets.UTF_8));
        } else {
            rb.method(method, HttpRequest.BodyPublishers.noBody());
        }
        HttpRequest req = rb.build();
        for (int attempt = 0; ; attempt++) {
            HttpResponse<String> res;
            try {
                res = http.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            } catch (IOException e) {
                throw new CairnException(0, "could not reach Cairn: " + e.getMessage(), null);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new CairnException(0, "interrupted", null);
            }
            noticeDeprecation(method, path, res.headers());
            Optional<Long> wait = retryAfterMs(res.headers().firstValue("retry-after").orElse(null));
            if (RETRY_STATUSES.contains(res.statusCode()) && wait.isPresent() && attempt < retries) {
                try {
                    Thread.sleep(Math.min(wait.get(), MAX_WAIT_MS));
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    throw new CairnException(res.statusCode(), "interrupted while waiting to retry", null);
                }
                continue;
            }
            Object parsed = null;
            String text = res.body();
            if (text != null && !text.isEmpty()) {
                try {
                    parsed = Json.parse(text);
                } catch (IllegalArgumentException e) {
                    parsed = text;
                }
            }
            if (res.statusCode() >= 400) {
                throw new CairnException(res.statusCode(), detailOf(parsed, res.statusCode()), parsed);
            }
            return parsed;
        }
    }

    private void noticeDeprecation(String method, String path, HttpHeaders h) {
        Optional<String> deprecation = h.firstValue("deprecation");
        if (deprecation.isEmpty()) {
            return;
        }
        String key = method + " " + path.split("\\?", 2)[0];
        if (!warned.add(key)) {
            return;
        }
        String successor = null;
        for (String link : h.allValues("link")) {
            Matcher m = SUCCESSOR.matcher(link);
            if (m.find()) {
                successor = m.group(1);
            }
        }
        String sunset = h.firstValue("sunset").map(CairnClient::isoDate).orElse(null);
        onDeprecation.accept(new Deprecation(method, path, deprecation.get(), sunset, successor));
    }

    private static String isoDate(String httpDate) {
        try {
            return ZonedDateTime.parse(httpDate, DateTimeFormatter.RFC_1123_DATE_TIME).toLocalDate().toString();
        } catch (DateTimeParseException e) {
            return httpDate;
        }
    }

    private static Optional<Long> retryAfterMs(String value) {
        if (value == null || value.isBlank()) {
            return Optional.empty();
        }
        try {
            return Optional.of(Math.max(0, Long.parseLong(value.trim()) * 1000));
        } catch (NumberFormatException e) {
            try {
                long at = ZonedDateTime.parse(value.trim(), DateTimeFormatter.RFC_1123_DATE_TIME).toInstant().toEpochMilli();
                return Optional.of(Math.max(0, at - System.currentTimeMillis()));
            } catch (DateTimeParseException e2) {
                return Optional.empty();
            }
        }
    }

    private static String detailOf(Object body, int status) {
        if (body instanceof Map<?, ?> m && m.containsKey("detail")) {
            Object d = m.get("detail");
            return d instanceof String s ? s : Json.write(d);
        }
        if (body instanceof String s && !s.isEmpty()) {
            return s.length() > 500 ? s.substring(0, 500) : s;
        }
        return "HTTP " + status;
    }

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8).replace("+", "%20");
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> obj(Object o) {
        return (Map<String, Object>) o;
    }

    @SuppressWarnings("unchecked")
    private List<Object> list(Object o) {
        return (List<Object>) o;
    }

    private static Map<String, Object> body(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int k = 0; k < kv.length; k += 2) {
            m.put((String) kv[k], kv[k + 1]);
        }
        return m;
    }

    // ---- the main APIs -------------------------------------------------------------------------------

    /** Who the token belongs to. */
    public Map<String, Object> me() {
        return obj(request("GET", "/me", null, null));
    }

    /** Routes being retired, with their sunset dates. */
    public Map<String, Object> deprecations() {
        return obj(request("GET", "/deprecations", null, null));
    }

    /** A dataset's quality health. */
    public Map<String, Object> datasetHealth(String datasetUrn) {
        return obj(request("GET", "/quality/datasets/" + enc(datasetUrn) + "/health", null, null));
    }

    /** A dataset's urn from its name (catalog.schema.table). */
    public Map<String, Object> resolveDataset(String name) {
        return obj(request("GET", "/v1/datasets/resolve", Map.of("name", name), null));
    }

    /** Search the catalog by meaning. */
    public Map<String, Object> semanticSearch(String query, int limit) {
        return obj(request("POST", "/ai/search/semantic", null, body("query", query, "limit", limit)));
    }

    /** Ask a question about the catalog. */
    public Map<String, Object> ask(String question) {
        return obj(request("POST", "/ai/ask", null, body("question", question)));
    }

    /** A turn with the assistant; pass the conversation id it returned (or null) to continue. */
    public Map<String, Object> chat(String message, String conversationId) {
        return obj(request("POST", "/ai/chat", null, body("message", message, "conversation_id", conversationId)));
    }

    /** Run SQL on a connection, governed: row filters and masks apply as for the token's holder. */
    public Map<String, Object> runSql(String connection, String sql, int limit) {
        return obj(request("POST", "/sql/run", null, body("connection", connection, "sql", sql, "limit", limit)));
    }

    public List<Object> savedQueries() {
        return list(request("GET", "/sql/saved", null, null));
    }

    /** Data products in the marketplace matching {@code q} (null for all). */
    public Object products(String q) {
        Map<String, Object> query = new LinkedHashMap<>();
        query.put("q", q);
        return request("GET", "/marketplace/products", query, null);
    }

    public Map<String, Object> product(String urn) {
        return obj(request("GET", "/marketplace/products/" + enc(urn), null, null));
    }

    /** Ask for access to a data product, for a purpose. */
    public Map<String, Object> requestAccess(String productUrn, String purpose, String justification, Integer durationDays) {
        Map<String, Object> b = body("product_urn", productUrn, "purpose", purpose, "justification",
                justification == null ? "" : justification);
        if (durationDays != null) {
            b.put("duration_days", durationDays);
        }
        return obj(request("POST", "/marketplace/access-requests", null, b));
    }

    public Object accessRequests() {
        return request("GET", "/marketplace/access-requests", null, null);
    }

    public Map<String, Object> accessRequest(String id) {
        return obj(request("GET", "/marketplace/access-requests/" + enc(id), null, null));
    }

    /** Custom agents the token's holder may run. */
    public List<Object> customAgents() {
        return list(request("GET", "/v1/custom-agents", null, null));
    }

    /** Run a deployed custom agent as the token's holder. */
    public Map<String, Object> runAgent(String name, String input) {
        return obj(request("POST", "/v1/custom-agents/" + enc(name) + "/run", null, body("input", input)));
    }
}
