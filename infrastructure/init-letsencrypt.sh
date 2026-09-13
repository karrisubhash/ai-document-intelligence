#!/usr/bin/env sh
#
# First-boot TLS bootstrap for the production stack.
#
#   NGINX_SERVER_NAME=docs.example.edu CERTBOT_EMAIL=ops@example.edu \
#     sh infrastructure/init-letsencrypt.sh
#
# WHY THIS SCRIPT HAS TO EXIST
#
# There is a chicken-and-egg problem in every webroot-validated TLS setup,
# and skipping it is why the production stack previously could not start at
# all: nginx refuses to load a server block whose certificate file is
# missing, but Let's Encrypt can only issue that certificate after it
# fetches a challenge file over HTTP - which needs nginx already running.
#
# So this does the standard three-step dance:
#
#   1. write a throwaway SELF-SIGNED certificate at the exact path nginx
#      expects, purely so nginx can start;
#   2. start nginx, which can now serve /.well-known/acme-challenge/;
#   3. ask certbot for the real certificate, which overwrites the
#      self-signed one in place, and reload nginx.
#
# After this, the `certbot` service in docker-compose.prod.yml renews on a
# timer and nginx reloads itself every 6 hours to pick up a renewal. This
# script is only for the first issuance.
#
# NOT YET EXERCISED AGAINST A REAL DOMAIN. The logic is the conventional
# webroot flow and the paths are checked against this repo's nginx config,
# but no certificate has been issued from this checkout - there is no
# public hostname pointing at it. Run it first with STAGING=1, which uses
# Let's Encrypt's staging CA and cannot burn the rate limit.

set -eu

DOMAIN="${NGINX_SERVER_NAME:-}"
EMAIL="${CERTBOT_EMAIL:-}"
STAGING="${STAGING:-0}"

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
LIVE="/etc/letsencrypt/live/$DOMAIN"

if [ -z "$DOMAIN" ] || [ "$DOMAIN" = "localhost" ]; then
  echo "NGINX_SERVER_NAME must be a real, public hostname (got '${DOMAIN}')." >&2
  echo "Let's Encrypt cannot issue for localhost." >&2
  exit 1
fi

if [ -z "$EMAIL" ]; then
  echo "CERTBOT_EMAIL must be set - it is where expiry warnings go." >&2
  exit 1
fi

echo "==> 1/3  Writing a temporary self-signed certificate for $DOMAIN"
echo "         (so nginx can start and serve the ACME challenge)"
$COMPOSE run --rm --entrypoint sh certbot -c "
  mkdir -p '$LIVE'
  if [ -f '$LIVE/fullchain.pem' ]; then
    echo '         a certificate already exists - leaving it alone'
    exit 0
  fi
  openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout '$LIVE/privkey.pem' \
    -out '$LIVE/fullchain.pem' \
    -subj '/CN=$DOMAIN'
"

echo "==> 2/3  Starting nginx"
$COMPOSE up -d nginx
# Give nginx a moment to bind before the CA is told to come and look.
$COMPOSE exec nginx sh -c 'until nginx -t >/dev/null 2>&1; do sleep 1; done'

echo "==> 3/3  Requesting the real certificate"
STAGING_FLAG=""
if [ "$STAGING" = "1" ]; then
  echo "         using the STAGING CA - the result will not be trusted by browsers"
  STAGING_FLAG="--staging"
fi

# --force-renewal because step 1 left a self-signed cert at this path and
# certbot would otherwise see a valid-looking certificate and decline.
$COMPOSE run --rm certbot certonly \
  --webroot --webroot-path=/var/www/certbot \
  --email "$EMAIL" \
  --agree-tos --no-eff-email \
  --force-renewal \
  $STAGING_FLAG \
  -d "$DOMAIN"

echo "==> Reloading nginx with the issued certificate"
$COMPOSE exec nginx nginx -s reload

echo
echo "Done. Renewal is handled by the certbot service; nginx reloads every"
echo "6 hours to pick one up. Verify with:"
echo "    curl -sSI https://$DOMAIN/health/"
