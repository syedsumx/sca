package io.aegis.authorization;

import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal JSON writer/parser used for the PDP wire protocol.
 *
 * <p>The plugin runs inside GMS's isolated plugin classloader. Bundling Jackson would risk version
 * clashes with the copy GMS ships, so the plugin carries this small, strict implementation instead.
 * It supports the full JSON grammar (objects, arrays, strings with escapes, numbers, booleans,
 * null); numbers are returned as {@link Double} or {@link Long}.
 */
final class MiniJson {

  private MiniJson() {}

  // ---------------------------------------------------------------- writing

  static String write(Object value) {
    StringBuilder sb = new StringBuilder(256);
    writeValue(sb, value);
    return sb.toString();
  }

  @SuppressWarnings("unchecked")
  private static void writeValue(StringBuilder sb, Object value) {
    if (value == null) {
      sb.append("null");
    } else if (value instanceof String) {
      writeString(sb, (String) value);
    } else if (value instanceof Boolean || value instanceof Integer || value instanceof Long) {
      sb.append(value);
    } else if (value instanceof Number) {
      double d = ((Number) value).doubleValue();
      if (Double.isNaN(d) || Double.isInfinite(d)) {
        sb.append("null");
      } else {
        sb.append(value);
      }
    } else if (value instanceof Map) {
      sb.append('{');
      boolean first = true;
      for (Map.Entry<Object, Object> e : ((Map<Object, Object>) value).entrySet()) {
        if (!first) {
          sb.append(',');
        }
        first = false;
        writeString(sb, String.valueOf(e.getKey()));
        sb.append(':');
        writeValue(sb, e.getValue());
      }
      sb.append('}');
    } else if (value instanceof Collection) {
      sb.append('[');
      boolean first = true;
      for (Object o : (Collection<Object>) value) {
        if (!first) {
          sb.append(',');
        }
        first = false;
        writeValue(sb, o);
      }
      sb.append(']');
    } else {
      writeString(sb, value.toString());
    }
  }

  private static void writeString(StringBuilder sb, String s) {
    sb.append('"');
    for (int i = 0; i < s.length(); i++) {
      char c = s.charAt(i);
      switch (c) {
        case '"':
          sb.append("\\\"");
          break;
        case '\\':
          sb.append("\\\\");
          break;
        case '\n':
          sb.append("\\n");
          break;
        case '\r':
          sb.append("\\r");
          break;
        case '\t':
          sb.append("\\t");
          break;
        case '\b':
          sb.append("\\b");
          break;
        case '\f':
          sb.append("\\f");
          break;
        default:
          if (c < 0x20) {
            sb.append(String.format("\\u%04x", (int) c));
          } else {
            sb.append(c);
          }
      }
    }
    sb.append('"');
  }

  // ---------------------------------------------------------------- parsing

  static Object parse(String json) {
    Parser p = new Parser(json);
    p.skipWs();
    Object v = p.readValue(0);
    p.skipWs();
    if (p.pos != json.length()) {
      throw new IllegalArgumentException("Trailing characters at " + p.pos);
    }
    return v;
  }

  private static final class Parser {
    private static final int MAX_DEPTH = 64;
    private final String s;
    private int pos;

    Parser(String s) {
      this.s = s;
    }

    void skipWs() {
      while (pos < s.length()) {
        char c = s.charAt(pos);
        if (c == ' ' || c == '\n' || c == '\r' || c == '\t') {
          pos++;
        } else {
          break;
        }
      }
    }

    Object readValue(int depth) {
      if (depth > MAX_DEPTH) {
        throw new IllegalArgumentException("JSON nested too deeply");
      }
      if (pos >= s.length()) {
        throw new IllegalArgumentException("Unexpected end of JSON");
      }
      char c = s.charAt(pos);
      switch (c) {
        case '{':
          return readObject(depth);
        case '[':
          return readArray(depth);
        case '"':
          return readString();
        case 't':
          expect("true");
          return Boolean.TRUE;
        case 'f':
          expect("false");
          return Boolean.FALSE;
        case 'n':
          expect("null");
          return null;
        default:
          if (c == '-' || (c >= '0' && c <= '9')) {
            return readNumber();
          }
          throw new IllegalArgumentException("Unexpected character '" + c + "' at " + pos);
      }
    }

    private void expect(String literal) {
      if (!s.startsWith(literal, pos)) {
        throw new IllegalArgumentException("Expected " + literal + " at " + pos);
      }
      pos += literal.length();
    }

    private Map<String, Object> readObject(int depth) {
      Map<String, Object> out = new LinkedHashMap<>();
      pos++; // {
      skipWs();
      if (peek() == '}') {
        pos++;
        return out;
      }
      while (true) {
        skipWs();
        if (peek() != '"') {
          throw new IllegalArgumentException("Expected object key at " + pos);
        }
        String key = readString();
        skipWs();
        if (peek() != ':') {
          throw new IllegalArgumentException("Expected ':' at " + pos);
        }
        pos++;
        skipWs();
        out.put(key, readValue(depth + 1));
        skipWs();
        char c = peek();
        pos++;
        if (c == '}') {
          return out;
        }
        if (c != ',') {
          throw new IllegalArgumentException("Expected ',' or '}' at " + (pos - 1));
        }
      }
    }

    private List<Object> readArray(int depth) {
      List<Object> out = new ArrayList<>();
      pos++; // [
      skipWs();
      if (peek() == ']') {
        pos++;
        return out;
      }
      while (true) {
        skipWs();
        out.add(readValue(depth + 1));
        skipWs();
        char c = peek();
        pos++;
        if (c == ']') {
          return out;
        }
        if (c != ',') {
          throw new IllegalArgumentException("Expected ',' or ']' at " + (pos - 1));
        }
      }
    }

    private char peek() {
      if (pos >= s.length()) {
        throw new IllegalArgumentException("Unexpected end of JSON");
      }
      return s.charAt(pos);
    }

    private String readString() {
      pos++; // opening quote
      StringBuilder sb = new StringBuilder();
      while (true) {
        char c = peek();
        pos++;
        if (c == '"') {
          return sb.toString();
        }
        if (c == '\\') {
          char e = peek();
          pos++;
          switch (e) {
            case '"':
            case '\\':
            case '/':
              sb.append(e);
              break;
            case 'n':
              sb.append('\n');
              break;
            case 'r':
              sb.append('\r');
              break;
            case 't':
              sb.append('\t');
              break;
            case 'b':
              sb.append('\b');
              break;
            case 'f':
              sb.append('\f');
              break;
            case 'u':
              if (pos + 4 > s.length()) {
                throw new IllegalArgumentException("Bad unicode escape at " + pos);
              }
              sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
              pos += 4;
              break;
            default:
              throw new IllegalArgumentException("Bad escape '\\" + e + "' at " + pos);
          }
        } else if (c < 0x20) {
          throw new IllegalArgumentException("Control character in string at " + (pos - 1));
        } else {
          sb.append(c);
        }
      }
    }

    private Object readNumber() {
      int start = pos;
      if (peek() == '-') {
        pos++;
      }
      boolean isFloat = false;
      while (pos < s.length()) {
        char c = s.charAt(pos);
        if (c >= '0' && c <= '9') {
          pos++;
        } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
          isFloat = true;
          pos++;
        } else {
          break;
        }
      }
      String num = s.substring(start, pos);
      if (num.isEmpty() || "-".equals(num)) {
        throw new IllegalArgumentException("Bad number at " + start);
      }
      return isFloat ? (Object) Double.parseDouble(num) : (Object) Long.parseLong(num);
    }
  }
}
