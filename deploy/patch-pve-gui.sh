#!/bin/sh
# Keep the UPS status row in the PVE node summary.
#
# pve-manager upgrades replace index.html.tpl and drop unowned files in
# /usr/share/pve-manager/js, so this script is idempotent and runs from the
# APT post-invoke hook (see 99pve-ups-gui) as well as install.sh.
set -eu

WIDGET_SRC=/usr/local/lib/ups-pve/pve-ups-status.js
WIDGET_DST=/usr/share/pve-manager/js/pve-ups-status.js
TEMPLATE=/usr/share/pve-manager/index.html.tpl
VER=6
TAG="    <script type=\"text/javascript\" src=\"/pve2/js/pve-ups-status.js?v=$VER\"></script>"

changed=0

if [ -d /usr/share/pve-manager/js ] && [ -f "$WIDGET_SRC" ]; then
    if ! cmp -s "$WIDGET_SRC" "$WIDGET_DST"; then
        install -m 644 "$WIDGET_SRC" "$WIDGET_DST"
        changed=1
    fi
fi

# Drop stale tags (older ?v=) and insert the current one after pvemanagerlib.js.
if [ -f "$TEMPLATE" ] && ! grep -q "pve-ups-status\.js?v=$VER" "$TEMPLATE"; then
    tmp=$(mktemp)
    grep -v 'pve-ups-status\.js' "$TEMPLATE" |
        awk -v tag="$TAG" '{ print } /pvemanagerlib\.js/ && !done { print tag; done = 1 }' \
            >"$tmp"
    if grep -q "pve-ups-status\.js?v=$VER" "$tmp"; then
        cat "$tmp" >"$TEMPLATE"
        changed=1
    fi
    rm -f "$tmp"
fi

if [ "$changed" -eq 1 ]; then
    # index.html.tpl is cached by pveproxy; restart it so the tag is served.
    systemctl try-restart pveproxy.service 2>/dev/null || true
fi

exit 0
