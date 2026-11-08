package io.cairn.trino;

import io.trino.spi.Plugin;

import java.util.Set;

/**
 * Cairn's functions in Trino SQL. {@code ai_search(index, query[, k])} returns the closest chunks of a Cairn
 * vector index, searched by Cairn as the person running the query.
 */
public final class CairnFunctionsPlugin implements Plugin {
    @Override
    public Set<Class<?>> getFunctions() {
        return Set.of(AiSearchFunction.class);
    }
}
