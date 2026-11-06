package io.cairn.client;

/** Cairn answered with an error status. */
public class CairnException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    private final int status;
    private final String detail;
    private final transient Object body;

    public CairnException(int status, String detail, Object body) {
        super("Cairn answered " + status + ": " + detail);
        this.status = status;
        this.detail = detail;
        this.body = body;
    }

    /** The HTTP status, e.g. 403 when the token's holder may not do this. */
    public int status() {
        return status;
    }

    /** Cairn's explanation. */
    public String detail() {
        return detail;
    }

    /** The parsed answer, if there was one. */
    public Object body() {
        return body;
    }
}
