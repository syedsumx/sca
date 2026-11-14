package io.cairn.store;

import java.io.IOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Random;

import org.apache.hadoop.conf.Configuration;
import org.apache.hadoop.fs.FSDataInputStream;
import org.apache.hadoop.fs.FSDataOutputStream;
import org.apache.hadoop.fs.FileStatus;
import org.apache.hadoop.fs.FileSystem;
import org.apache.hadoop.fs.LocatedFileStatus;
import org.apache.hadoop.fs.Path;
import org.apache.hadoop.fs.RemoteIterator;

/**
 * Exercises Hadoop's S3A file system against Cairn Store, the way Hadoop, Spark and Hive use it.
 *
 * <pre>java ... io.cairn.store.HadoopCheck &lt;endpoint&gt; &lt;access key&gt; &lt;secret key&gt; &lt;bucket&gt;</pre>
 *
 * Prints one line per check and exits non-zero on the first failure.
 */
public final class HadoopCheck {
    private static int checks;

    private static void check(boolean ok, String what) {
        if (!ok) {
            throw new AssertionError("FAILED: " + what);
        }
        checks++;
        System.out.println("ok " + what);
    }

    private static byte[] read(FileSystem fs, Path p) throws IOException {
        try (FSDataInputStream in = fs.open(p)) {
            return in.readAllBytes();
        }
    }

    private static String sha256(byte[] b) throws Exception {
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(b));
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 4) {
            System.err.println("usage: HadoopCheck <endpoint> <access key> <secret key> <bucket>");
            System.exit(2);
        }
        Configuration conf = new Configuration(false);
        conf.set("fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem");
        conf.set("fs.s3a.endpoint", args[0]);
        conf.set("fs.s3a.endpoint.region", "us-east-1");
        conf.setBoolean("fs.s3a.path.style.access", true);
        conf.setBoolean("fs.s3a.connection.ssl.enabled", args[0].startsWith("https"));
        conf.set("fs.s3a.access.key", args[1]);
        conf.set("fs.s3a.secret.key", args[2]);
        conf.set("fs.s3a.aws.credentials.provider", "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider");
        conf.set("fs.s3a.multipart.size", "5M");
        conf.set("fs.s3a.multipart.threshold", "5M");
        conf.set("fs.s3a.fast.upload.buffer", "bytebuffer");
        conf.setInt("fs.s3a.attempts.maximum", 1);
        conf.setInt("fs.s3a.retry.limit", 1);

        try (FileSystem fs = FileSystem.newInstance(new URI("s3a://" + args[3] + "/"), conf)) {
            Path root = new Path("/hadoop-check");
            Path dir = new Path(root, "dir");
            check(fs.mkdirs(dir), "mkdirs");
            check(fs.getFileStatus(dir).isDirectory(), "an empty directory is a directory");

            Path small = new Path(dir, "a.txt");
            try (FSDataOutputStream out = fs.create(small, false)) {
                out.write("hello hadoop\n".getBytes(StandardCharsets.UTF_8));
            }
            FileStatus st = fs.getFileStatus(small);
            check(st.isFile() && st.getLen() == 13, "create and stat a small file");
            check(new String(read(fs, small), StandardCharsets.UTF_8).equals("hello hadoop\n"), "read it back");
            boolean refused = false;
            try (FSDataOutputStream out = fs.create(small, false)) {
                out.write(1);
            } catch (IOException e) {
                refused = true; // FileAlreadyExistsException
            }
            check(refused, "create without overwrite refuses an existing file");

            byte[] big = new byte[12 * 1024 * 1024 + 4321];
            new Random(42).nextBytes(big);
            Path large = new Path(dir, "large.bin");
            try (FSDataOutputStream out = fs.create(large, true)) {
                for (int off = 0; off < big.length; off += 1 << 20) {
                    out.write(big, off, Math.min(1 << 20, big.length - off));
                }
            }
            check(fs.getFileStatus(large).getLen() == big.length, "multipart upload of a 12 MiB file");
            check(sha256(read(fs, large)).equals(sha256(big)), "the large file reads back byte for byte");
            try (FSDataInputStream in = fs.open(large)) {
                byte[] part = new byte[4096];
                in.readFully(7_000_000L, part);
                boolean same = true;
                for (int i = 0; i < part.length; i++) {
                    same &= part[i] == big[7_000_000 + i];
                }
                check(same, "positioned read in the middle");
                in.seek(11_000_000L);
                check(in.read() == (big[11_000_000] & 0xff), "seek and read");
            }

            List<String> names = new ArrayList<>();
            for (FileStatus s : fs.listStatus(dir)) {
                names.add(s.getPath().getName());
            }
            names.sort(null);
            check(names.equals(List.of("a.txt", "large.bin")), "listStatus " + names);
            int deep = 0;
            RemoteIterator<LocatedFileStatus> it = fs.listFiles(root, true);
            while (it.hasNext()) {
                it.next();
                deep++;
            }
            check(deep == 2, "recursive listFiles");

            Path renamed = new Path(dir, "b.txt");
            check(fs.rename(small, renamed), "rename a file");
            check(!fs.exists(small) && fs.exists(renamed), "the file moved");

            Path moved = new Path(root, "moved");
            check(fs.rename(dir, moved), "rename a directory");
            check(!fs.exists(dir) && fs.getFileStatus(new Path(moved, "large.bin")).getLen() == big.length, "the directory moved");

            check(fs.getContentSummary(root).getLength() == big.length + 13, "content summary");
            check(fs.delete(root, true), "recursive delete");
            check(!fs.exists(root), "nothing left");
        }
        System.out.println("PASSED " + checks + " checks");
    }
}
