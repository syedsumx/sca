#!/usr/bin/env bash
# The vSwitch that connects the dedicated servers privately (Hetzner Robot API). Credentials: a Robot
# web service user (Robot -> Settings -> Web service and app settings), in ROBOT_USER / ROBOT_PASSWORD.
#
#   deploy/hetzner/robot.sh servers                       # your servers: number, IP, product, data centre
#   deploy/hetzner/robot.sh vswitch-create cairn 4000     # prints the new vSwitch's id
#   deploy/hetzner/robot.sh vswitch-attach <id> <server IP>...
#   deploy/hetzner/robot.sh vswitch-show <id>
#
# A vSwitch links servers in the same network zone (eu-central: FSN1 and NBG1; HEL1 is its own zone).
# Attaching takes a few minutes; vswitch-show says when each server is "ready".
set -euo pipefail
api=https://robot-ws.your-server.de
: "${ROBOT_USER:?set ROBOT_USER}" "${ROBOT_PASSWORD:?set ROBOT_PASSWORD}"
call() {   # method path [curl args]: the Robot user's password goes to curl on stdin, not the command line
    local method=$1 path=$2; shift 2
    printf 'user = "%s:%s"\n' "$ROBOT_USER" "$ROBOT_PASSWORD" |
        curl -sS --fail-with-body -K - -X "$method" "$api$path" "$@"
}
case "${1:-}" in
    servers)
        call GET /server | jq -r '.[].server | [.server_number, .server_ip, .product, .dc] | @tsv' ;;
    vswitch-create)
        name=${2:?name}; vlan=${3:?vlan 4000-4091}
        call POST /vswitch --data-urlencode "name=$name" --data-urlencode "vlan=$vlan" | jq -r '.vswitch.id // .id' ;;
    vswitch-attach)
        id=${2:?vswitch id}; shift 2; [ $# -gt 0 ] || { echo "give the servers' IPs" >&2; exit 64; }
        args=(); for ip in "$@"; do args+=(--data-urlencode "server[]=$ip"); done
        call POST "/vswitch/$id/server" "${args[@]}" >/dev/null
        echo "attaching $*: check with vswitch-show $id" >&2 ;;
    vswitch-show)
        call GET "/vswitch/${2:?vswitch id}" | jq -r '(.vswitch // .) | "vSwitch \(.id) \(.name) VLAN \(.vlan)", (.server[] | "  \(.server_ip) \(.status)")' ;;
    *) sed -n '2,10p' "$0"; exit 64 ;;
esac
