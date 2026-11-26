#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

bundle_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
application_dir=/opt/persermaps-github
service_user=persermaps-github
state_dir=/var/lib/persermaps-github
configuration=/etc/persermaps-github.env

if [[ $EUID -ne 0 ]]; then
    printf 'Запустите установку от root.\n' >&2
    exit 1
fi

copy_application_files() {
    local file
    install -d -m 0755 "$application_dir/web/assets"
    for file in app.py called.py companies.py sources.py yandex_chrome.py check-api.py requirements.txt; do
        install -m 0644 "$bundle_dir/$file" "$application_dir/$file"
    done
    install -m 0644 "$bundle_dir/web/index.html" "$application_dir/web/index.html"
    install -m 0644 "$bundle_dir/web/assets/"{styles.css,parser.js} "$application_dir/web/assets/"
}

configure_web_login() {
    if ! grep -q '^PARSER_WEB_PASSWORD=' "$configuration"; then
        (
            umask 077
            printf 'PARSER_WEB_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" >> "$configuration"
        )
    fi
}

restart_application() {
    systemctl restart "$service_user"
    for _ in {1..20}; do
        if curl -fsS --max-time 2 http://127.0.0.1:8765/health 2>/dev/null; then
            printf '\nСлужба парсера запущена.\n'
            python3 - "$configuration" <<'PY'
import base64
from pathlib import Path
import sys
from urllib.request import Request, urlopen

settings = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
password = next(line.partition("=")[2] for line in reversed(settings) if line.startswith("PARSER_WEB_PASSWORD="))
credentials = base64.b64encode(("parser:" + password).encode()).decode()
request = Request("http://127.0.0.1:8765/", headers={"Authorization": "Basic " + credentials})
with urlopen(request, timeout=5) as response:
    if b'id="search-form"' not in response.read():
        raise SystemExit("Форма поиска отсутствует на странице парсера.")
print("WEB OK: страница открывается после входа.")
PY
            return
        fi
        sleep 0.5
    done
    systemctl --no-pager status "$service_user" || true
    return 1
}

if [[ $# -eq 1 && $1 == --app-only ]]; then
    if [[ ! -x $application_dir/venv/bin/python || ! -f $configuration ]]; then
        printf 'Сначала запустите полную установку.\n' >&2
        exit 1
    fi
    copy_application_files
    configure_web_login
    restart_application
    exit 0
fi
if [[ $# -ne 0 ]]; then
    printf 'Использование: bash install-vps.sh [--app-only]\n' >&2
    exit 1
fi

if ! systemctl is-active --quiet "$service_user" && ss -H -ltn 'sport = :8765' | grep -q .; then
    printf 'Порт 8765 уже занят. Уточните, какой сервис на нём работает.\n' >&2
    exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl git gnupg python3-venv

chrome_binary=$(command -v google-chrome-stable || command -v google-chrome || command -v chromium || true)
if [[ -z $chrome_binary ]]; then
    if [[ $(dpkg --print-architecture) != amd64 ]]; then
        printf 'Для этой архитектуры установите совместимый Chromium.\n' >&2
        exit 1
    fi
    if ! apt-cache show google-chrome-stable >/dev/null 2>&1; then
        apt_files=$(mktemp -d /tmp/persermaps-chrome.XXXXXX)
        chrome_repo=/etc/apt/sources.list.d/persermaps-github-chrome.list
        chrome_key=/etc/apt/keyrings/persermaps-github-chrome.gpg
        if [[ -e $chrome_repo || -e $chrome_key ]]; then
            printf 'Найдены ранее созданные настройки репозитория Chrome.\n' >&2
            exit 1
        fi
        trap 'rm -f -- "$chrome_repo" "$chrome_key"; rm -rf -- "$apt_files"' EXIT
        curl -fsSL --max-time 30 https://dl.google.com/linux/linux_signing_key.pub -o "$apt_files/chrome.pub"
        gpg --homedir "$apt_files" --batch --import "$apt_files/chrome.pub"
        gpg --homedir "$apt_files" --batch --export EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796 > "$apt_files/chrome.gpg"
        test -s "$apt_files/chrome.gpg"
        install -d -m 0755 /etc/apt/keyrings
        install -m 0644 "$apt_files/chrome.gpg" "$chrome_key"
        printf 'deb [arch=amd64 signed-by=%s] https://dl.google.com/linux/chrome/deb/ stable main\n' "$chrome_key" > "$chrome_repo"
        apt-get update
    fi
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends google-chrome-stable
    chrome_binary=$(command -v google-chrome-stable)
fi

if ! id "$service_user" >/dev/null 2>&1; then
    useradd --system --user-group --create-home --home-dir "$state_dir" --shell /usr/sbin/nologin "$service_user"
fi
install -d -m 0755 "$application_dir" "$application_dir/sources"
install -d -m 0700 -o "$service_user" -g "$service_user" "$state_dir"

checkout_source() {
    local path=$1 repository=$2 revision=$3
    if [[ -e $path ]]; then
        if [[ $(git -C "$path" remote get-url origin) != "$repository" || $(git -C "$path" rev-parse HEAD) != "$revision" ]]; then
            printf 'В каталоге %s находится другой checkout.\n' "$path" >&2
            return 1
        fi
        return
    fi

    local staging
    staging=$(mktemp -d "$application_dir/sources/checkout.XXXXXX")
    if git -C "$staging" init -q && git -C "$staging" remote add origin "$repository" && \
        git -C "$staging" fetch -q --depth 1 origin "$revision" && git -C "$staging" checkout -q --detach FETCH_HEAD; then
        chmod 0755 "$staging"
        mv -T -- "$staging" "$path"
    else
        rm -rf -- "$staging"
        return 1
    fi
}

checkout_source "$application_dir/sources/parser-2gis" https://github.com/interlark/parser-2gis.git e12cc050ea811ed76e65a1be938214b6583f9072

copy_application_files

if [[ ! -x $application_dir/venv/bin/python ]]; then
    python3 -m venv "$application_dir/venv"
fi
"$application_dir/venv/bin/python" -m pip install -r "$application_dir/requirements.txt"
if [[ ! -x $application_dir/2gis-venv/bin/python ]]; then
    python3 -m venv "$application_dir/2gis-venv"
fi
"$application_dir/2gis-venv/bin/python" -m pip install "$application_dir/sources/parser-2gis"
"$application_dir/2gis-venv/bin/python" "$application_dir/sources/parser-2gis/parser-2gis.py" --version

driver_binary=$(command -v chromedriver || command -v chromium.chromedriver || true)
driver_args=()
if [[ -n $driver_binary && ${chrome_binary##*/} == chromium ]]; then
    driver_args=(--driver "$driver_binary")
fi
cd "$application_dir"
runuser -u "$service_user" -- "$application_dir/venv/bin/python" "$application_dir/yandex_chrome.py" \
    --browser "$chrome_binary" "${driver_args[@]}" --check-browser

if [[ ! -e $configuration ]]; then
    (
        umask 077
        {
            printf 'PARSER_API_KEY=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
            printf 'PARSER_DB_PATH=%s/called.sqlite3\n' "$state_dir"
            printf 'MAPS_CHROME_BINARY=%s\n' "$chrome_binary"
            if [[ ${#driver_args[@]} -gt 0 ]]; then
                printf 'MAPS_CHROMEDRIVER=%s\n' "$driver_binary"
            fi
        } > "$configuration"
    )
fi
configure_web_login

install -m 0644 "$bundle_dir/persermaps-github.service" /etc/systemd/system/persermaps-github.service
systemctl daemon-reload
systemctl enable "$service_user"
restart_application
