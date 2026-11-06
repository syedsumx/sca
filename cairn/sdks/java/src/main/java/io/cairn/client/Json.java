package io.cairn.client;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * A small JSON codec so the client has no dependencies. Objects are {@code Map<String, Object>} (in order),
 * arrays {@code List<Object>}, numbers {@code Long} or {@code Double} (or {@code BigInteger}/{@code BigDecimal}
 * when they do not fit), plus {@code String}, {@code Boolean} and {@code null}.
 */
public final class Json {
    private static final int MAX_DEPTH = 512;

    private final String s;
    private int i;

    private Json(String s) {
        this.s = s;
    }

    /** Parse a JSON document. */
    public static Object parse(String text) {
        Json p = new Json(text);
        p.ws();
        Object v = p.value(0);
        p.ws();
        if (p.i != p.s.length()) {
            throw p.error("unexpected text after the value");
        }
        return v;
    }

    /** Write a value as JSON. */
    public static String write(Object value) {
        StringBuilder out = new StringBuilder();
        write(value, out);
        return out.toString();
    }

    private static void write(Object v, StringBuilder out) {
        if (v == null) {
            out.append("null");
        } else if (v instanceof String str) {
            quote(str, out);
        } else if (v instanceof Boolean || v instanceof Integer || v instanceof Long || v instanceof Short
                || v instanceof Byte || v instanceof BigInteger || v instanceof BigDecimal) {
            out.append(v);
        } else if (v instanceof Double || v instanceof Float) {
            double d = ((Number) v).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw new IllegalArgumentException("JSON has no NaN or infinity");
            }
            out.append(v);
        } else if (v instanceof Map<?, ?> m) {
            out.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) {
                    out.append(',');
                }
                first = false;
                quote(String.valueOf(e.getKey()), out);
                out.append(':');
                write(e.getValue(), out);
            }
            out.append('}');
        } else if (v instanceof Iterable<?> it) {
            out.append('[');
            Iterator<?> iter = it.iterator();
            while (iter.hasNext()) {
                write(iter.next(), out);
                if (iter.hasNext()) {
                    out.append(',');
                }
            }
            out.append(']');
        } else if (v instanceof Object[] arr) {
            write(List.of(arr), out);
        } else {
            quote(v.toString(), out);
        }
    }

    private static void quote(String str, StringBuilder out) {
        out.append('"');
        for (int k = 0; k < str.length(); k++) {
            char c = str.charAt(k);
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                case '\b' -> out.append("\\b");
                case '\f' -> out.append("\\f");
                default -> {
                    if (c < 0x20 || c == ' ' || c == ' ') {
                        out.append(String.format("\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
                }
            }
        }
        out.append('"');
    }

    private IllegalArgumentException error(String what) {
        return new IllegalArgumentException("invalid JSON at " + i + ": " + what);
    }

    private void ws() {
        while (i < s.length()) {
            char c = s.charAt(i);
            if (c != ' ' && c != '\n' && c != '\r' && c != '\t') {
                return;
            }
            i++;
        }
    }

    private Object value(int depth) {
        if (depth > MAX_DEPTH) {
            throw error("nested too deeply");
        }
        if (i >= s.length()) {
            throw error("unexpected end");
        }
        char c = s.charAt(i);
        return switch (c) {
            case '{' -> object(depth);
            case '[' -> array(depth);
            case '"' -> string();
            case 't' -> literal("true", Boolean.TRUE);
            case 'f' -> literal("false", Boolean.FALSE);
            case 'n' -> literal("null", null);
            default -> number();
        };
    }

    private Object literal(String word, Object v) {
        if (!s.startsWith(word, i)) {
            throw error("expected " + word);
        }
        i += word.length();
        return v;
    }

    private Map<String, Object> object(int depth) {
        Map<String, Object> m = new LinkedHashMap<>();
        i++;
        ws();
        if (i < s.length() && s.charAt(i) == '}') {
            i++;
            return m;
        }
        while (true) {
            ws();
            if (i >= s.length() || s.charAt(i) != '"') {
                throw error("expected a key");
            }
            String key = string();
            ws();
            if (i >= s.length() || s.charAt(i) != ':') {
                throw error("expected :");
            }
            i++;
            ws();
            m.put(key, value(depth + 1));
            ws();
            if (i >= s.length()) {
                throw error("unexpected end");
            }
            char c = s.charAt(i++);
            if (c == '}') {
                return m;
            }
            if (c != ',') {
                throw error("expected , or }");
            }
        }
    }

    private List<Object> array(int depth) {
        List<Object> list = new ArrayList<>();
        i++;
        ws();
        if (i < s.length() && s.charAt(i) == ']') {
            i++;
            return list;
        }
        while (true) {
            ws();
            list.add(value(depth + 1));
            ws();
            if (i >= s.length()) {
                throw error("unexpected end");
            }
            char c = s.charAt(i++);
            if (c == ']') {
                return list;
            }
            if (c != ',') {
                throw error("expected , or ]");
            }
        }
    }

    private String string() {
        StringBuilder out = new StringBuilder();
        i++;
        while (true) {
            if (i >= s.length()) {
                throw error("unterminated string");
            }
            char c = s.charAt(i++);
            if (c == '"') {
                return out.toString();
            }
            if (c < 0x20) {
                throw error("control character in string");
            }
            if (c != '\\') {
                out.append(c);
                continue;
            }
            if (i >= s.length()) {
                throw error("unterminated escape");
            }
            char e = s.charAt(i++);
            switch (e) {
                case '"', '\\', '/' -> out.append(e);
                case 'b' -> out.append('\b');
                case 'f' -> out.append('\f');
                case 'n' -> out.append('\n');
                case 'r' -> out.append('\r');
                case 't' -> out.append('\t');
                case 'u' -> {
                    if (i + 4 > s.length()) {
                        throw error("short \\u escape");
                    }
                    try {
                        out.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                    } catch (NumberFormatException ex) {
                        throw error("bad \\u escape");
                    }
                    i += 4;
                }
                default -> throw error("bad escape");
            }
        }
    }

    private Object number() {
        int start = i;
        if (i < s.length() && s.charAt(i) == '-') {
            i++;
        }
        boolean fraction = false;
        while (i < s.length()) {
            char c = s.charAt(i);
            if (c >= '0' && c <= '9') {
                i++;
            } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                fraction = true;
                i++;
            } else {
                break;
            }
        }
        String t = s.substring(start, i);
        if (t.isEmpty() || t.equals("-")) {
            throw error("expected a value");
        }
        try {
            if (!fraction) {
                BigInteger big = new BigInteger(t);
                return big.bitLength() < 64 ? (Object) big.longValue() : big;
            }
            BigDecimal dec = new BigDecimal(t);
            double d = dec.doubleValue();
            return Double.isInfinite(d) ? dec : (Object) d;
        } catch (NumberFormatException ex) {
            throw error("bad number");
        }
    }
}
