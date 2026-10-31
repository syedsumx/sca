// Command cairn-store is Cairn's S3-compatible object store (single node).
//
//	cairn-store serve   run the server (default)
//	cairn-store fsck    check the data directory and remove debris left by a crash, then exit
//	cairn-store healthcheck  exit 0 if the local server is ready (container health checks)
//	cairn-store version print the version
//
// Configuration comes from flags or environment variables:
//
//	CAIRN_STORE_ADDR              listen address (:8333)
//	CAIRN_STORE_DATA              data directory (/data)
//	CAIRN_STORE_ACCESS_KEY        admin access key ...
//	CAIRN_STORE_SECRET_KEY        ... and its secret (or CAIRN_STORE_SECRET_KEY_FILE)
//	CAIRN_STORE_CREDENTIALS_FILE  JSON list of further keys: access_key, secret_key, buckets, read_only, admin
//	CAIRN_STORE_REGION            region reported to clients (us-east-1)
//	CAIRN_STORE_DOMAIN            enables virtual-hosted-style requests on <bucket>.<domain>
//	CAIRN_STORE_BUCKETS           comma-separated buckets to create at start-up
//	CAIRN_STORE_TLS_CERT/_KEY     serve HTTPS
//	CAIRN_STORE_FSYNC             false disables fsync (tests only: a crash can lose acknowledged writes)
//	CAIRN_STORE_LOG_LEVEL         debug, info, warn, error
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/syedsumx/sca/cairn/store/internal/engine"
	"github.com/syedsumx/sca/cairn/store/internal/s3"
)

var version = "0.1.0"

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func main() {
	cmd := "serve"
	args := os.Args[1:]
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		cmd, args = args[0], args[1:]
	}
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	addr := fs.String("addr", env("CAIRN_STORE_ADDR", ":8333"), "listen address")
	data := fs.String("data", env("CAIRN_STORE_DATA", "/data"), "data directory")
	fs.Parse(args)

	level := slog.LevelInfo
	_ = level.UnmarshalText([]byte(env("CAIRN_STORE_LOG_LEVEL", "info")))
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level}))

	switch cmd {
	case "version":
		fmt.Println("cairn-store", version)
	case "fsck":
		eng, err := engine.Open(*data, engine.Options{Fsync: true})
		if err != nil {
			fatal(log, "open store", err)
		}
		rep, err := eng.Recover()
		eng.Close()
		if err != nil {
			fatal(log, "fsck", err)
		}
		out, _ := json.MarshalIndent(rep, "", "  ")
		fmt.Println(string(out))
		if rep.MissingBlobs > 0 {
			os.Exit(2)
		}
	case "serve":
		serve(log, *addr, *data)
	case "healthcheck":
		host := *addr
		if strings.HasPrefix(host, ":") {
			host = "127.0.0.1" + host
		}
		c := http.Client{Timeout: 3 * time.Second}
		resp, err := c.Get("http://" + host + "/-/ready")
		if err != nil || resp.StatusCode != http.StatusOK {
			os.Exit(1)
		}
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q (serve, fsck, version)\n", cmd)
		os.Exit(2)
	}
}

func fatal(log *slog.Logger, msg string, err error) {
	log.Error(msg, "err", err)
	os.Exit(1)
}

func credentials() ([]s3.Credential, error) {
	var out []s3.Credential
	secret := os.Getenv("CAIRN_STORE_SECRET_KEY")
	if f := os.Getenv("CAIRN_STORE_SECRET_KEY_FILE"); f != "" {
		b, err := os.ReadFile(f)
		if err != nil {
			return nil, err
		}
		secret = strings.TrimSpace(string(b))
	}
	if ak := os.Getenv("CAIRN_STORE_ACCESS_KEY"); ak != "" {
		out = append(out, s3.Credential{AccessKey: ak, SecretKey: secret, Admin: true})
	}
	if f := os.Getenv("CAIRN_STORE_CREDENTIALS_FILE"); f != "" {
		b, err := os.ReadFile(f)
		if err != nil {
			return nil, err
		}
		var more []s3.Credential
		if err := json.Unmarshal(b, &more); err != nil {
			return nil, fmt.Errorf("%s: %w", f, err)
		}
		out = append(out, more...)
	}
	if len(out) == 0 {
		return nil, errors.New("set CAIRN_STORE_ACCESS_KEY and CAIRN_STORE_SECRET_KEY, or CAIRN_STORE_CREDENTIALS_FILE")
	}
	return out, nil
}

func serve(log *slog.Logger, addr, data string) {
	creds, err := credentials()
	if err != nil {
		fatal(log, "credentials", err)
	}
	fsync := env("CAIRN_STORE_FSYNC", "true") != "false"
	eng, err := engine.Open(data, engine.Options{Fsync: fsync})
	if err != nil {
		fatal(log, "open store", err)
	}
	defer eng.Close()
	if !fsync {
		log.Warn("fsync is off: a crash or power loss can lose writes that were acknowledged")
	}
	for _, b := range strings.Split(env("CAIRN_STORE_BUCKETS", ""), ",") {
		if b = strings.TrimSpace(b); b != "" {
			if err := eng.CreateBucket(b); err != nil && !errors.Is(err, engine.ErrBucketExists) {
				fatal(log, "create bucket "+b, err)
			}
		}
	}
	srv, err := s3.New(eng, s3.Config{Region: env("CAIRN_STORE_REGION", "us-east-1"), Domain: os.Getenv("CAIRN_STORE_DOMAIN"),
		Credentials: creds, Logger: log})
	if err != nil {
		fatal(log, "configure", err)
	}
	hs := &http.Server{Addr: addr, Handler: srv, ReadHeaderTimeout: 30 * time.Second, IdleTimeout: 120 * time.Second,
		MaxHeaderBytes: 64 << 10}
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-stop
		log.Info("shutting down")
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		hs.Shutdown(ctx)
	}()
	log.Info("cairn-store listening", "addr", addr, "data", data, "version", version, "keys", len(creds), "fsync", fsync)
	cert, key := os.Getenv("CAIRN_STORE_TLS_CERT"), os.Getenv("CAIRN_STORE_TLS_KEY")
	if cert != "" {
		err = hs.ListenAndServeTLS(cert, key)
	} else {
		err = hs.ListenAndServe()
	}
	if err != nil && !errors.Is(err, http.ErrServerClosed) {
		fatal(log, "serve", err)
	}
}
