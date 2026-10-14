#!/bin/sh
# Store setup, run by the WP-CLI container every time the pod starts.
#
# Every step checks before it acts, so running it again is safe: a fresh volume
# gets a full install, an existing one gets only what is missing. The pod is
# marked Ready when this script finishes (the readiness probe looks for the
# marker file), so the storefront is never exposed half-configured.
set -eu

cd /var/www/html
MARKER=/tmp/setup-complete
rm -f "$MARKER"
ADMIN="--user=$WP_ADMIN_USER"

log() { echo "[setup] $*"; }

# True when version $1 is older than version $2.
older_than() {
  php -r 'exit(version_compare($argv[1], $argv[2], "<") ? 0 : 1);' "$1" "$2"
}

db_ready() {
  php -r '
    mysqli_report(MYSQLI_REPORT_OFF);
    $h = explode(":", getenv("WORDPRESS_DB_HOST"));
    $c = @mysqli_connect($h[0], getenv("WORDPRESS_DB_USER"), getenv("WORDPRESS_DB_PASSWORD"),
                         getenv("WORDPRESS_DB_NAME"), (int) ($h[1] ?? 3306));
    exit($c ? 0 : 1);'
}

# The WordPress container copies core into the volume and then writes
# wp-config.php, so the config file appearing means the copy is complete.
log "waiting for WordPress files"
until [ -f wp-config.php ] && [ -f wp-includes/version.php ]; do sleep 2; done

log "waiting for the database"
until db_ready; do sleep 3; done

if ! wp core is-installed 2>/dev/null; then
  log "installing WordPress"
  wp core install \
    --url="$WP_URL" \
    --title="$WP_TITLE" \
    --admin_user="$WP_ADMIN_USER" \
    --admin_password="$WP_ADMIN_PASSWORD" \
    --admin_email="$WP_ADMIN_EMAIL" \
    --skip-email
fi

# Upgrades are driven by Helm values. Core lives on the volume, so a newer
# image alone does not upgrade an existing store; this does.
core_now=$(wp core version)
if [ -n "${WP_CORE_VERSION:-}" ] && [ "$core_now" != "$WP_CORE_VERSION" ]; then
  if older_than "$core_now" "$WP_CORE_VERSION"; then
    log "upgrading WordPress core $core_now -> $WP_CORE_VERSION"
    wp core update --version="$WP_CORE_VERSION"
    wp core update-db
  else
    log "WordPress core $core_now is newer than the chart's $WP_CORE_VERSION, leaving it alone"
  fi
fi

if ! wp plugin is-installed woocommerce; then
  log "installing WooCommerce $WC_VERSION"
  wp plugin install woocommerce --version="$WC_VERSION" --activate
else
  wp plugin is-active woocommerce || wp plugin activate woocommerce
  wc_now=$(wp plugin get woocommerce --field=version)
  if [ "$wc_now" != "$WC_VERSION" ] && older_than "$wc_now" "$WC_VERSION"; then
    log "upgrading WooCommerce $wc_now -> $WC_VERSION"
    wp plugin update woocommerce --version="$WC_VERSION"
    wp wc update $ADMIN
  fi
fi

# Store configuration is applied once per chart revision of this script and its
# inputs, so normal pod restarts skip straight past it.
if [ "$(wp option get urumi_setup_rev 2>/dev/null || true)" != "$SETUP_REV" ]; then
  log "configuring the store"
  wp option update blogname "$WP_TITLE"
  wp option update timezone_string "$STORE_TIMEZONE"
  # Pretty permalinks are what makes /wp-json/ (the REST API) available.
  wp rewrite structure '/%postname%/'

  # New WooCommerce stores start hidden behind a "coming soon" page.
  wp option update woocommerce_coming_soon no
  wp option update woocommerce_currency "$STORE_CURRENCY"
  wp option update woocommerce_default_country "$STORE_COUNTRY"
  wp option update woocommerce_allow_tracking no
  wp option update woocommerce_show_marketplace_suggestions no
  wp option update woocommerce_onboarding_profile '{"skipped":true}' --format=json

  # Shop, cart, checkout and account pages.
  wp wc tool run install_pages $ADMIN

  # Cash on delivery: a real checkout with no payment provider to configure.
  wp wc payment_gateway update cod --enabled=true $ADMIN

  # Checkout refuses physical products when no shipping method matches, so the
  # catch-all zone gets free shipping.
  if [ "$(wp wc shipping_zone_method list 0 $ADMIN --format=count)" = "0" ]; then
    wp wc shipping_zone_method create 0 --method_id=free_shipping $ADMIN
  fi

  if [ "${SEED_SAMPLE_PRODUCTS:-false}" = "true" ]; then
    if [ -z "$(wp wc product list --sku=URUMI-TEE --field=id $ADMIN)" ]; then
      wp wc product create $ADMIN --name="Urumi Classic Tee" --type=simple --sku=URUMI-TEE \
        --regular_price=19.99 --short_description="Soft cotton tee. Sample product." --porcelain
    fi
    if [ -z "$(wp wc product list --sku=URUMI-MUG --field=id $ADMIN)" ]; then
      wp wc product create $ADMIN --name="Urumi Coffee Mug" --type=simple --sku=URUMI-MUG \
        --regular_price=12.50 --short_description="Ceramic mug, 350 ml. Sample product." --porcelain
    fi
  fi

  wp option update urumi_setup_rev "$SETUP_REV"
fi

# Runs on every start so the REST key in WooCommerce always matches the
# Kubernetes Secret, including after the Secret is rotated.
wp eval-file /scripts/ensure-api-key.php

touch "$MARKER"
log "store is ready at $WP_URL"

# Stay running: Kubernetes restarts containers that exit, and an idle WP-CLI is
# useful for `kubectl exec ... -- wp ...`.
trap 'exit 0' TERM INT
while true; do
  sleep 3600 &
  wait $!
done
