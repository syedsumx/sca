package io.cairn.trino;

import io.airlift.slice.Slice;
import io.airlift.slice.Slices;
import io.trino.spi.TrinoException;
import io.trino.spi.block.Block;
import io.trino.spi.block.BlockBuilder;
import io.trino.spi.block.RowBlock;
import io.trino.spi.connector.ConnectorSession;
import io.trino.spi.function.Description;
import io.trino.spi.function.ScalarFunction;
import io.trino.spi.function.SqlType;
import io.trino.spi.type.StandardTypes;

import java.util.List;
import java.util.Map;

import static io.trino.spi.StandardErrorCode.INVALID_FUNCTION_ARGUMENT;
import static io.trino.spi.type.DoubleType.DOUBLE;
import static io.trino.spi.type.IntegerType.INTEGER;
import static io.trino.spi.type.VarcharType.VARCHAR;

/**
 * {@code ai_search(index, query[, k])}: the {@code k} chunks of a Cairn vector index closest to {@code query}, as
 * {@code array(row(key, chunk, text, score))}, closest first. Cairn searches as the session's user, with the same
 * checks as {@code GET /v1/vectors/<index>/search}; a refusal fails the query with Cairn's reason. Use it with
 * {@code UNNEST}: {@code SELECT r.* FROM UNNEST(ai_search('help', 'refunds', 5)) AS r(key, chunk, text, score)}.
 */
public final class AiSearchFunction {
    static final String RESULT = "array(row(key varchar,chunk integer,text varchar,score double))";
    static final int DEFAULT_K = 10;
    static final int MAX_K = 100;

    private AiSearchFunction() {}

    @ScalarFunction(value = "ai_search", deterministic = false)
    @Description("The closest chunks of a Cairn vector index to a query, searched as the current user")
    @SqlType(RESULT)
    public static Block aiSearch(ConnectorSession session, @SqlType(StandardTypes.VARCHAR) Slice index,
                                 @SqlType(StandardTypes.VARCHAR) Slice query) {
        return search(session, index, query, DEFAULT_K);
    }

    @ScalarFunction(value = "ai_search", deterministic = false)
    @Description("The k closest chunks of a Cairn vector index to a query, searched as the current user")
    @SqlType(RESULT)
    public static Block aiSearch(ConnectorSession session, @SqlType(StandardTypes.VARCHAR) Slice index,
                                 @SqlType(StandardTypes.VARCHAR) Slice query, @SqlType(StandardTypes.BIGINT) long k) {
        return search(session, index, query, k);
    }

    private static Block search(ConnectorSession session, Slice index, Slice query, long k) {
        if (k < 1 || k > MAX_K) {
            throw new TrinoException(INVALID_FUNCTION_ARGUMENT, "ai_search: k is 1 to " + MAX_K);
        }
        List<Object> results = CairnClient.get().aiSearch(session.getUser(), session.getQueryId(),
                index.toStringUtf8(), query.toStringUtf8(), (int) k);
        int n = results.size();
        BlockBuilder keys = VARCHAR.createBlockBuilder(null, n);
        BlockBuilder chunks = INTEGER.createBlockBuilder(null, n);
        BlockBuilder texts = VARCHAR.createBlockBuilder(null, n);
        BlockBuilder scores = DOUBLE.createBlockBuilder(null, n);
        for (Object o : results) {
            if (!(o instanceof Map<?, ?> r)) {
                throw CairnClient.malformed("a result is not an object");
            }
            Object key = r.get("key");
            if (key == null) {
                keys.appendNull();
            }
            else {
                VARCHAR.writeSlice(keys, Slices.utf8Slice(key instanceof Double d && d == Math.rint(d)
                        ? Long.toString(d.longValue()) : String.valueOf(key)));
            }
            if (r.get("chunk") instanceof Double c) {
                INTEGER.writeLong(chunks, c.longValue());
            }
            else {
                chunks.appendNull();
            }
            if (r.get("text") instanceof String t) {
                VARCHAR.writeSlice(texts, Slices.utf8Slice(t));
            }
            else {
                texts.appendNull();
            }
            if (r.get("score") instanceof Double s) {
                DOUBLE.writeDouble(scores, s);
            }
            else {
                scores.appendNull();
            }
        }
        return RowBlock.fromFieldBlocks(n, new Block[] {keys.build(), chunks.build(), texts.build(), scores.build()});
    }
}
