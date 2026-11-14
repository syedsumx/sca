# cairn-web only: the built-in root account "admin" (password CAIRN_ADMIN_PASSWORD).
# Replaces the engine's sample credentials file; nothing else can log in with a password unless
# extra accounts are listed in CAIRN_LOCAL_USERS_FILE (one "user:password" per line).
if [ -n "${CAIRN_WEB_USERS_FILE-}" ]; then
  if [ -z "${CAIRN_ADMIN_PASSWORD-}" ]; then
    echo "cairn: CAIRN_ADMIN_PASSWORD must be set" >&2
    exit 64
  fi
  (
    umask 077
    {
      printf 'admin:%s\n' "$CAIRN_ADMIN_PASSWORD"
      if [ -n "${CAIRN_LOCAL_USERS_FILE-}" ] && [ -f "$CAIRN_LOCAL_USERS_FILE" ]; then
        grep -v '^[[:space:]]*\(#\|$\)' "$CAIRN_LOCAL_USERS_FILE"
      fi
    } > "$CAIRN_WEB_USERS_FILE"
  )
fi
