package io.cairn.authorization;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class MiniJsonTest {

  @Test
  void roundTrip() {
    Map<String, Object> m = new LinkedHashMap<>();
    m.put("s", "quote\" back\\ nl\n tab\t ctl\u0001 é");
    m.put("n", 42L);
    m.put("d", 1.5);
    m.put("b", true);
    m.put("z", null);
    m.put("l", Arrays.asList("a", 1L, null));
    String json = MiniJson.write(m);
    assertEquals(m, MiniJson.parse(json));
  }

  @Test
  void parsesWhitespaceAndUnicode() {
    Object v = MiniJson.parse(" { \"a\" : [ 1 , -2.5e1 , \"\\u00e9\\/\" ] } ");
    assertEquals(Map.of("a", List.of(1L, -25.0, "é/")), v);
  }

  @Test
  void rejectsMalformed() {
    assertThrows(IllegalArgumentException.class, () -> MiniJson.parse("{\"a\":1,}"));
    assertThrows(IllegalArgumentException.class, () -> MiniJson.parse("[1 2]"));
    assertThrows(IllegalArgumentException.class, () -> MiniJson.parse("{\"a\":1} x"));
    assertThrows(IllegalArgumentException.class, () -> MiniJson.parse("\"unterminated"));
    StringBuilder deep = new StringBuilder();
    for (int i = 0; i < 100; i++) {
      deep.append('[');
    }
    assertThrows(IllegalArgumentException.class, () -> MiniJson.parse(deep.toString()));
  }
}
