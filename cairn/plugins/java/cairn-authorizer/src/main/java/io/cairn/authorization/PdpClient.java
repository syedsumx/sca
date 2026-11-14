package io.cairn.authorization;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Map;

/** Thin HTTP client for the Cairn Policy Decision Point. */
class PdpClient {

  static final String AUTHORIZE_PATH = "/governance/v1/authorize";

  private final HttpClient http;
  private final URI authorizeUri;
  private final String serviceToken;
  private final Duration requestTimeout;

  PdpClient(String pdpUrl, String serviceToken, long connectTimeoutMs, long requestTimeoutMs) {
    this.http =
        HttpClient.newBuilder()
            .connectTimeout(Duration.ofMillis(connectTimeoutMs))
            .version(HttpClient.Version.HTTP_1_1)
            .followRedirects(HttpClient.Redirect.NEVER)
            .build();
    this.authorizeUri = URI.create(pdpUrl + AUTHORIZE_PATH);
    this.serviceToken = serviceToken;
    this.requestTimeout = Duration.ofMillis(requestTimeoutMs);
  }

  /**
   * Sends the authorization request. Any non-2xx status, transport error or malformed body is
   * surfaced as an {@link IOException} so the caller can apply its fail-closed policy.
   */
  PdpDecision authorize(Map<String, Object> payload) throws IOException {
    HttpRequest.Builder req =
        HttpRequest.newBuilder(authorizeUri)
            .timeout(requestTimeout)
            .header("Content-Type", "application/json")
            .header("Accept", "application/json")
            .header("User-Agent", "cairn-authorizer/0.1")
            .POST(HttpRequest.BodyPublishers.ofString(MiniJson.write(payload), StandardCharsets.UTF_8));
    if (serviceToken != null && !serviceToken.isEmpty()) {
      req.header("Authorization", "Bearer " + serviceToken);
    }
    HttpResponse<String> resp;
    try {
      resp = http.send(req.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      throw new IOException("Interrupted while calling PDP", e);
    }
    if (resp.statusCode() / 100 != 2) {
      throw new IOException("PDP returned HTTP " + resp.statusCode());
    }
    try {
      return PdpDecision.parse(resp.body());
    } catch (IllegalArgumentException e) {
      throw new IOException("Malformed PDP response: " + e.getMessage(), e);
    }
  }
}
