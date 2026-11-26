#!/usr/bin/env bash
set -Eeuo pipefail

parser_host=${1:-}
expected_ipv4=${2:-}
if [[ $EUID -ne 0 || $# -ne 2 || ! $parser_host =~ ^[a-z0-9-]+(\.[a-z0-9-]+)+$ || ! $expected_ipv4 =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then
    printf 'Запустите от root: bash publish-web.sh parser.example.com IPv4_сервера\n' >&2
    exit 1
fi

resolved_ipv4=$(getent ahostsv4 "$parser_host" | awk '{print $1}' | sort -u) || true
if [[ $resolved_ipv4 != "$expected_ipv4" ]]; then
    printf 'DNS для %s: %s. Нужна A-запись на %s.\n' \
        "$parser_host" "${resolved_ipv4:-нет IPv4-адреса}" "$expected_ipv4" >&2
    exit 1
fi

local_health=$(curl -fsS --max-time 5 http://127.0.0.1:8765/health)
other_listeners=$(ss -H -ltnp '( sport = :80 or sport = :443 )' | awk 'NF && $0 !~ /"caddy",/')
if [[ -n $other_listeners ]]; then
    printf 'Порты 80 или 443 заняты другим процессом:\n%s\n' "$other_listeners" >&2
    exit 1
fi

caddy_config=/etc/caddy/Caddyfile
preserve_config=false
if [[ -e $caddy_config ]]; then
    preserve_config=true
fi
if ! command -v caddy >/dev/null; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends caddy
fi
caddy_config=$(realpath -e -- "$caddy_config")

marker="# PerserMAPS $parser_host"
backup=
if ! grep -Fxq -- "$marker" "$caddy_config"; then
    candidate=$(mktemp "$(dirname -- "$caddy_config")/.persermaps-config.XXXXXX")
    trap 'rm -f -- "$candidate"' EXIT
    backup=$(mktemp /root/persermaps-caddy-before.XXXXXX)
    cp -- "$caddy_config" "$backup"
    if [[ $preserve_config == true ]]; then
        cat "$backup" > "$candidate"
    fi
    printf '\n%s\n%s {\n    reverse_proxy 127.0.0.1:8765\n}\n' "$marker" "$parser_host" >> "$candidate"
    caddy validate --config "$candidate" --adapter caddyfile
    if ! cmp -s -- "$caddy_config" "$backup"; then
        printf 'Конфигурация Caddy изменилась во время проверки. Повторите запуск.\n' >&2
        exit 1
    fi
    chmod --reference="$caddy_config" "$candidate"
    chown --reference="$caddy_config" "$candidate"
    mv -T -- "$candidate" "$caddy_config"
fi

activate_caddy() {
    if systemctl is-active --quiet caddy; then
        if [[ -n $backup ]]; then
            systemctl reload caddy
        fi
    else
        systemctl enable --now caddy
    fi
}

if ! activate_caddy; then
    if [[ -n $backup ]]; then
        cp -- "$backup" "$caddy_config"
    fi
    journalctl -u caddy -n 20 --no-pager
    exit 1
fi

for _ in {1..12}; do
    if public_health=$(curl -fsS --max-time 3 "https://$parser_host/health" 2>/dev/null); then
        if [[ $public_health != "$local_health" ]]; then
            printf 'Ответ HTTPS для /health отличается от ответа локального парсера.\n' >&2
            exit 1
        fi
        page_status=$(curl -sS --max-time 3 -o /dev/null -w '%{http_code}' "https://$parser_host/")
        if [[ $page_status == 401 ]]; then
            printf '%s\nHTTPS OK: https://%s/ запрашивает вход.\n' "$public_health" "$parser_host"
            exit 0
        fi
        printf 'HTTPS открылся, но страница вернула HTTP %s вместо запроса входа.\n' "$page_status" >&2
        exit 1
    fi
    sleep 2
done

printf 'HTTPS пока недоступен для %s. Журнал Caddy приведён ниже.\n' "$parser_host" >&2
journalctl -u caddy -n 25 --no-pager
exit 1
