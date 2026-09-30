#!/bin/sh
# Reload nginx every six hours so a certificate certbot has renewed is
# picked up without a restart.
#
# This lives in /docker-entrypoint.d/ rather than in the compose `command:`
# on purpose. The compose file used to wrap nginx in
#   sh -c "while :; do sleep 6h ...; nginx -s reload; done & nginx ..."
# and the official image only runs its entrypoint scripts - including the
# one that renders /etc/nginx/templates/*.template into conf.d - when the
# command is `nginx` itself. With the wrapper, the project's configuration
# was never rendered: the stock default server answered on port 80, nothing
# listened on 443, and there was no TLS, no frontend and no API proxy.
set -eu

(
    while :; do
        sleep 6h
        nginx -s reload
    done
) &
