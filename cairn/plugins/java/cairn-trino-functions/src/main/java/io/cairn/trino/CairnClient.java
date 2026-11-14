package io.cairn.trino;

import io.trino.spi.TrinoException;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static io.trino.spi.StandardErrorCode.EXCEEDED_FUNCTION_MEMORY_LIMIT;
import static io.trino.spi.StandardErrorCode.FUNCTION_IMPLEMENTATION_ERROR;
import static io.trino.spi.StandardErrorCode.GENERIC_INSUFFICIENT_RESOURCES;
import static io.trino.spi.StandardErrorCode.INVALID_FUNCTION_ARGUMENT;
import static io.trino.spi.StandardErrorCode.NOT_FOUND;
import static io.trino.spi.StandardErrorCode.PERMISSION_DENIED;

/**
 * Calls Cairn for Cairn's functions. Configured from Trino's environment, as Helm sets it: {@code CAIRN_API_URL}
 * (the Cairn API, e.g. {@code http://cairn-api:8000}) and Trino's access key for Cairn, {@code CAIRN_TRINO_ACCESS_KEY}
 * (or a file named by {@code CAIRN_TRINO_ACCESS_KEY_FILE}), the same key Trino's access control uses. Without them
 * every call fails: nothing is searched without Cairn's decision.
 */
final class CairnClient {
    private static final int MAX_RESPONSE = 32 * 1024 * 1024;
    private static volatile CairnClient instance;

    private final HttpClient http;
    private final String url;
    private final String key;
    private final Duration timeout;

    private CairnClient(String url, String key, Duration timeout) {
        this.url = url;
        this.key = key;
        this.timeout = timeout;
        this.http = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(Duration.ofSeconds(10)).proxy(HttpClient.Builder.NO_PROXY)
                .followRedirects(HttpClient.Redirect.NEVER).build();
    }

    static CairnClient get() {
        CairnClient c = instance;
        if (c == null) {
            synchronized (CairnClient.class) {
                c = instance;
                if (c == null) {
                    c = fromEnvironment();
                    instance = c;
                }
            }
        }
        return c;
    }

    private static CairnClient fromEnvironment() {
        String url = System.getenv("CAIRN_API_URL");
        String key = System.getenv("CAIRN_TRINO_ACCESS_KEY");
        String file = System.getenv("CAIRN_TRINO_ACCESS_KEY_FILE");
        if (file != null && !file.isBlank()) {
            try {
                key = Files.readString(Path.of(file), StandardCharsets.UTF_8).strip();
            }
            catch (IOException e) {
                throw new TrinoException(FUNCTION_IMPLEMENTATION_ERROR, "Cairn's functions: the access key file cannot be read");
            }
        }
        if (url == null || !(url.startsWith("http://") || url.startsWith("https://")) || key == null || key.length() < 24) {
            throw new TrinoException(FUNCTION_IMPLEMENTATION_ERROR,
                    "Cairn's functions are not set up: CAIRN_API_URL and CAIRN_TRINO_ACCESS_KEY are needed on Trino");
        }
        long seconds = 60;
        try {
            String t = System.getenv("CAIRN_FUNCTIONS_TIMEOUT_SECONDS");
            if (t != null && !t.isBlank()) {
                seconds = Math.max(1, Long.parseLong(t.strip()));
            }
        }
        catch (NumberFormatException ignored) {
            // the default
        }
        return new CairnClient(url.replaceAll("/+$", ""), key, Duration.ofSeconds(seconds));
    }

    /** The results of one search, as Cairn returns them ({@code [{key, chunk, text, score}]}). */
    List<Object> aiSearch(String user, String queryId, String index, String query, int k) {
        String body = "{\"user\":" + quote(user) + ",\"query_id\":" + quote(queryId) + ",\"index\":" + quote(index)
                + ",\"query\":" + quote(query) + ",\"k\":" + k + "}";
        Object doc = post("/v1/trino/functions/ai-search", body);
        if (doc instanceof Map<?, ?> m && m.get("results") instanceof List<?> results) {
            return new ArrayList<>(results);
        }
        throw malformed("no results");
    }

    private Object post(String path, String body) {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url + path)).timeout(timeout)
                .header("content-type", "application/json").header("x-cairn-trino-key", key)
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8)).build();
        HttpResponse<byte[]> resp;
        try {
            resp = http.send(req, HttpResponse.BodyHandlers.ofByteArray());
        }
        catch (IOException e) {
            throw new TrinoException(FUNCTION_IMPLEMENTATION_ERROR, "Cairn is not reachable: " + e.getMessage());
        }
        catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new TrinoException(FUNCTION_IMPLEMENTATION_ERROR, "interrupted while Cairn searched");
        }
        if (resp.body().length > MAX_RESPONSE) {
            throw new TrinoException(EXCEEDED_FUNCTION_MEMORY_LIMIT, "Cairn's answer is too large");
        }
        String text = new String(resp.body(), StandardCharsets.UTF_8);
        Object doc;
        try {
            doc = new Json(text).parse();
        }
        catch (IllegalArgumentException e) {
            doc = null;
        }
        int status = resp.statusCode();
        if (status == 200) {
            if (doc == null) {
                throw malformed("not JSON");
            }
            return doc;
        }
        String reason = doc instanceof Map<?, ?> m && m.get("detail") instanceof String d ? d : ("status " + status);
        throw new TrinoException(switch (status) {
            case 403 -> PERMISSION_DENIED;
            case 404 -> NOT_FOUND;
            case 400, 409, 422 -> INVALID_FUNCTION_ARGUMENT;
            case 429 -> GENERIC_INSUFFICIENT_RESOURCES;
            default -> FUNCTION_IMPLEMENTATION_ERROR;
        }, "Cairn: " + reason);
    }

    static TrinoException malformed(String why) {
        return new TrinoException(FUNCTION_IMPLEMENTATION_ERROR, "Cairn's answer is malformed: " + why);
    }

    static String quote(String s) {
        StringBuilder b = new StringBuilder(s.length() + 2).append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> b.append("\\\"");
                case '\\' -> b.append("\\\\");
                case '\n' -> b.append("\\n");
                case '\r' -> b.append("\\r");
                case '\t' -> b.append("\\t");
                default -> {
                    if (c < 0x20) {
                        b.append(String.format("\\u%04x", (int) c));
                    }
                    else {
                        b.append(c);
                    }
                }
            }
        }
        return b.append('"').toString();
    }

    /** A small JSON reader: objects (as maps), arrays (lists), strings, numbers (doubles), booleans and null. */
    static final class Json {
        private final String s;
        private int i;

        Json(String s) {
            this.s = s;
        }

        Object parse() {
            Object v = value(0);
            ws();
            if (i != s.length()) {
                throw new IllegalArgumentException("trailing data");
            }
            return v;
        }

        private void ws() {
            while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
                i++;
            }
        }

        private Object value(int depth) {
            if (depth > 64) {
                throw new IllegalArgumentException("too deep");
            }
            ws();
            if (i >= s.length()) {
                throw new IllegalArgumentException("end of input");
            }
            char c = s.charAt(i);
            if (c == '{') {
                i++;
                Map<String, Object> m = new LinkedHashMap<>();
                ws();
                if (peek('}')) {
                    return m;
                }
                do {
                    ws();
                    String k = string();
                    ws();
                    expect(':');
                    m.put(k, value(depth + 1));
                    ws();
                }
                while (peek(','));
                expect('}');
                return m;
            }
            if (c == '[') {
                i++;
                List<Object> l = new ArrayList<>();
                ws();
                if (peek(']')) {
                    return l;
                }
                do {
                    l.add(value(depth + 1));
                    ws();
                }
                while (peek(','));
                expect(']');
                return l;
            }
            if (c == '"') {
                return string();
            }
            if (s.startsWith("true", i)) {
                i += 4;
                return Boolean.TRUE;
            }
            if (s.startsWith("false", i)) {
                i += 5;
                return Boolean.FALSE;
            }
            if (s.startsWith("null", i)) {
                i += 4;
                return null;
            }
            int start = i;
            while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) {
                i++;
            }
            if (start == i) {
                throw new IllegalArgumentException("unexpected " + c);
            }
            return Double.parseDouble(s.substring(start, i));
        }

        private boolean peek(char c) {
            if (i < s.length() && s.charAt(i) == c) {
                i++;
                return true;
            }
            return false;
        }

        private void expect(char c) {
            if (!peek(c)) {
                throw new IllegalArgumentException("expected " + c);
            }
        }

        private String string() {
            expect('"');
            StringBuilder b = new StringBuilder();
            while (true) {
                if (i >= s.length()) {
                    throw new IllegalArgumentException("unterminated string");
                }
                char c = s.charAt(i++);
                if (c == '"') {
                    return b.toString();
                }
                if (c != '\\') {
                    b.append(c);
                    continue;
                }
                if (i >= s.length()) {
                    throw new IllegalArgumentException("bad escape");
                }
                char e = s.charAt(i++);
                switch (e) {
                    case '"', '\\', '/' -> b.append(e);
                    case 'b' -> b.append('\b');
                    case 'f' -> b.append('\f');
                    case 'n' -> b.append('\n');
                    case 'r' -> b.append('\r');
                    case 't' -> b.append('\t');
                    case 'u' -> {
                        if (i + 4 > s.length()) {
                            throw new IllegalArgumentException("bad escape");
                        }
                        b.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                        i += 4;
                    }
                    default -> throw new IllegalArgumentException("bad escape");
                }
            }
        }
    }
}
